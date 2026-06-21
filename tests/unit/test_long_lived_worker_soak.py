"""Deterministic long-lived llama.cpp worker soak and fault-injection coverage."""
import json
import sys
import threading
from pathlib import Path

import pytest

from utils.llm import model_manager as model_manager_module
from utils.llm.model_manager import ModelManager

PROMPT_SENTINEL = "SOAK_SECRET_PROMPT_SENTINEL_7"
OUTPUT_SENTINEL = "SOAK_SECRET_OUTPUT_SENTINEL_7"


class _Config:
    is_production = False

    def __init__(self, models_dir: Path):
        self.models_dir = str(models_dir)

    def get(self, key, default=None):
        values = {
            "model.filename": "fake.gguf",
            "model.url": "https://invalid.local/fake.gguf",
            "paths.models_dir": self.models_dir,
            "model.use_mock": False,
            "model.context_size": 256,
            "model.chat_format": "llama-3",
            "model.max_tokens": 32,
            "model.temperature": 0.0,
            "model.top_p": 1.0,
            "model.stop_tokens": [],
            "model.n_gpu_layers": 0,
            "model.enforce_gpu_memory_headroom": False,
        }
        return values.get(key, default)


@pytest.fixture
def fake_llama_site(tmp_path, monkeypatch):
    site = tmp_path / "fake-site-packages"
    package = site / "llama_cpp"
    package.mkdir(parents=True)
    state_file = tmp_path / "fake_llama_state.json"
    state_file.write_text(json.dumps({"created": 0, "closed": 0}), encoding="utf-8")
    package.joinpath("__init__.py").write_text(
        r'''
import json
import os
import sys
from pathlib import Path

GGML_USE_CUDA = False
GGML_USE_METAL = False

def llama_supports_gpu_offload():
    return False

STATE = Path(os.environ["TOKEN_PLACE_FAKE_LLAMA_STATE"])

def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"created": 0, "closed": 0}

def _save(data):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(STATE)

class Llama:
    def __init__(self, *args, **kwargs):
        if os.environ.get("TOKEN_PLACE_FAKE_LLAMA_INIT_FAIL") == "1":
            raise RuntimeError("fake init failure")
        data = _load()
        data["created"] = int(data.get("created", 0)) + 1
        data["active_generation"] = data["created"] - 1
        data.setdefault("requests", [])
        _save(data)
        self.generation = data["active_generation"]

    def create_chat_completion(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        data = _load()
        data.setdefault("requests", []).append({"generation": self.generation})
        _save(data)
        if "RAISE_ONCE" in prompt and not data.get("raised_once"):
            data["raised_once"] = True
            _save(data)
            raise RuntimeError("fake request scoped failure")
        if "EXIT_ABRUPT" in prompt:
            os._exit(17)
        return {"choices": [{"message": {"role": "assistant", "content": os.environ.get("TOKEN_PLACE_FAKE_OUTPUT_SENTINEL", "fake output") + " gen=" + str(self.generation)}}]}
''',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setenv("TOKEN_PLACE_FAKE_LLAMA_STATE", str(state_file))
    monkeypatch.setenv("TOKEN_PLACE_FAKE_OUTPUT_SENTINEL", OUTPUT_SENTINEL)
    monkeypatch.setenv("TOKEN_PLACE_LLAMA_CPP_SUBPROCESS_INFERENCE_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(model_manager_module, "_signal_guard_available", lambda: False)
    monkeypatch.setattr(
        model_manager_module,
        "_find_llama_cpp_spec_in_subprocess",
        lambda **_: {"module_path": str(package / "__init__.py"), "interpreter": sys.executable},
    )
    monkeypatch.setattr(
        model_manager_module,
        "_probe_llama_cpp_capabilities_in_subprocess",
        lambda **_: {
            "backend": "cpu",
            "gpu_offload_supported": False,
            "detected_device": "cpu",
            "interpreter": sys.executable,
            "prefix": sys.prefix,
            "llama_module_path": str(package / "__init__.py"),
            "error": None,
        },
    )
    yield state_file


def _manager(tmp_path):
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    (models / "fake.gguf").write_bytes(b"not a real gguf")
    return ModelManager(_Config(models))


def _complete(manager: ModelManager, request_id: str, content: str):
    return manager.create_chat_completion_with_recovery(
        messages=[{"role": "user", "content": f"{PROMPT_SENTINEL} {request_id} {content}"}],
        stream=False,
    )


def _state(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_bounded(manager: ModelManager, state_file: Path, *, created_max: int, restarts: int):
    status = manager.worker_lifecycle_status()
    state = _state(state_file)
    assert state["created"] <= created_max
    assert status["worker_restart_count"] == restarts
    assert status["worker_generation"] <= created_max
    assert threading.active_count() < 80


def test_long_lived_worker_soak_and_fault_injection(tmp_path, fake_llama_site, caplog, capsys, monkeypatch):
    manager = _manager(tmp_path)
    responses_by_request = {}

    # 1. One warmed worker generation serves 100 sequential API v1-style requests.
    first = _complete(manager, "warm-0", "healthy")
    assert OUTPUT_SENTINEL in first["choices"][0]["message"]["content"]
    for index in range(1, 101):
        request_id = f"healthy-{index}"
        response = _complete(manager, request_id, "healthy")
        assert request_id not in responses_by_request
        responses_by_request[request_id] = response
    assert _state(fake_llama_site)["created"] == 1
    assert manager.worker_lifecycle_status()["worker_generation"] == 0
    _assert_bounded(manager, fake_llama_site, created_max=1, restarts=0)

    # 2. A request-scoped exception does not poison or replace the worker.
    with pytest.raises(model_manager_module.LlamaCppInferenceRequestError):
        _complete(manager, "request-error", "RAISE_ONCE")
    after_error = _complete(manager, "after-request-error", "healthy")
    assert "gen=0" in after_error["choices"][0]["message"]["content"]
    _assert_bounded(manager, fake_llama_site, created_max=1, restarts=0)

    # 3. A killed worker is evicted and replaced once.
    manager.llm._process.kill()
    recovered = _complete(manager, "dead-worker", "healthy-after-kill")
    assert "gen=1" in recovered["choices"][0]["message"]["content"]
    _assert_bounded(manager, fake_llama_site, created_max=2, restarts=1)

    # 4. Two local relay fixtures share one model manager; inference is serialized.
    barrier = threading.Barrier(3)
    relay_results = []

    def relay_fixture(name):
        barrier.wait(timeout=5)
        relay_results.append(_complete(manager, f"relay-{name}", "healthy"))

    threads = [threading.Thread(target=relay_fixture, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
    assert len(relay_results) == 2
    assert _state(fake_llama_site)["created"] == 2
    _assert_bounded(manager, fake_llama_site, created_max=2, restarts=1)

    # 5. Persistent fatal replacement failure leaves the node failed/not-ready with no cached worker.
    monkeypatch.setenv("TOKEN_PLACE_FAKE_LLAMA_INIT_FAIL", "1")
    manager.llm._process.kill()
    with pytest.raises(RuntimeError, match="replacement failed"):
        _complete(manager, "fatal-replacement", "healthy-after-fatal-kill")
    status = manager.worker_lifecycle_status()
    assert status["worker_state"] == "failed"
    assert status["worker_alive"] is False
    assert manager.llm is None
    assert status["worker_restart_count"] == 2

    # 6. Stop while idle/recovering, then Start again and complete another request.
    manager._close_llm_proxy(manager.llm)
    manager.llm = None
    manager.worker_state = "stopped"
    monkeypatch.delenv("TOKEN_PLACE_FAKE_LLAMA_INIT_FAIL", raising=False)
    restarted = _complete(manager, "start-again", "healthy")
    assert "gen=2" in restarted["choices"][0]["message"]["content"]
    final_status = manager.worker_lifecycle_status()
    assert final_status["worker_state"] == "ready"
    assert final_status["worker_restart_count"] == 2
    _assert_bounded(manager, fake_llama_site, created_max=3, restarts=2)

    # Privacy: plaintext sentinels may be in caller-owned fake results, never in logs/diagnostics/CI output.
    captured = capsys.readouterr()
    diagnostics = json.dumps(final_status, sort_keys=True)
    unsafe_surfaces = "\n".join([caplog.text, captured.out, captured.err, diagnostics])
    assert PROMPT_SENTINEL not in unsafe_surfaces
    assert OUTPUT_SENTINEL not in unsafe_surfaces
    assert len(responses_by_request) == len(set(responses_by_request))
