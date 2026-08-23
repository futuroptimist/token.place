import importlib.util
import json
import sys
import types
from pathlib import Path

SCRIPT = (
    Path(__file__).parents[2] / "desktop-tauri/src-tauri/python/headless_admission.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("headless_admission_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mock_runtime_fails_closed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("USE_MOCK_LLM", "1")
    assert load_module().run(tmp_path / "model.gguf", "8k-fast") != 0
    assert json.loads(capsys.readouterr().out) == {
        "authoritative_evidence": "failed",
        "schema_version": 1,
        "warm_load": "failed",
    }


def test_success_requires_production_authoritative_evidence(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("USE_MOCK_LLM", raising=False)
    monkeypatch.setenv("TOKENPLACE_RUNTIME_ID", "bundled-test")

    class Manager:
        model_profile = {}

        def get_llm_instance(self):
            return object()

        def close(self):
            self.closed = True

    manager = Manager()
    bridge = types.ModuleType("compute_node_bridge")
    bridge.ensure_desktop_python_dependencies = lambda: {"ok": "true"}
    bridge._ensure_desktop_llama_runtime_for_context = lambda mode, tier: {
        "selected_backend": "cpu"
    }
    bridge._load_context_profile_helpers = lambda: (
        lambda manager, tier: None,
        lambda tier: tier,
    )
    runtime = types.ModuleType("utils.compute_node_runtime")
    runtime.ComputeNodeRuntimeConfig = lambda **kwargs: kwargs
    runtime.ComputeNodeRuntime = lambda config: types.SimpleNamespace(
        model_manager=manager
    )
    runtime.apply_compute_mode = lambda manager, mode: None
    network = types.ModuleType("utils.networking.relay_client")

    class RelayClient:
        _api_v1_render_and_tokenize_chat_prompt = staticmethod(
            lambda *args, **kwargs: 9
        )

        @staticmethod
        def _api_v1_record_benchmark_tokenizer_observation(llm, messages, **kwargs):
            fixture = messages[0]["content"].encode()
            Path(
                __import__("os").environ[
                    "TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_EVIDENCE"
                ]
            ).write_text(
                json.dumps(
                    {
                        "method": "packaged_admission_render_and_tokenize_chat",
                        "runtime_identity": "bundled-test",
                        "fixture_sha256": __import__("hashlib")
                        .sha256(fixture)
                        .hexdigest(),
                        "total_prompt_tokens": 9,
                        "target_offsets_tokens": {"boundary": 5},
                    }
                )
            )

    network.RelayClient = RelayClient
    monkeypatch.setitem(sys.modules, "compute_node_bridge", bridge)
    monkeypatch.setitem(sys.modules, "utils.compute_node_runtime", runtime)
    monkeypatch.setitem(sys.modules, "utils.networking.relay_client", network)
    assert load_module().run(tmp_path / "model.gguf", "8k-fast") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["warm_load"] == "ready"
    assert result["authoritative_evidence"] == "passed"
    assert "model" not in result and "fixture" not in result
