#!/usr/bin/env python3
"""Deterministic desktop worker soak and fault-injection coverage.

This local-only harness exercises the long-lived llama.cpp worker lifecycle used by
packaged desktop/operator parity flows without a real GGUF, GPU, external relay,
or network.  The fake runtime implements the same non-streaming subprocess facade
surface consumed by ModelManager: ``is_alive()``, ``close()``, and
``create_chat_completion()`` with restartable/request-scoped failures.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.llm import model_manager as model_manager_module  # noqa: E402

LOG_DIR = REPO_ROOT / ".desktop-e2e-logs"
REQUEST_COUNT = 100
PROMPT_SENTINEL = "SOAK_PLAINTEXT_PROMPT_SENTINEL_DO_NOT_LOG"
OUTPUT_SENTINEL = "SOAK_PLAINTEXT_OUTPUT_SENTINEL_DO_NOT_LOG"


class _Config:
    is_production = False

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, key: str, default: Any = None) -> Any:
        values = {
            "model.filename": "soak-model.gguf",
            "model.url": "https://example.invalid/soak-model.gguf",
            "model.download_chunk_size_mb": 1,
            "model.download_timeout": 1,
            "paths.models_dir": str(self.root),
            "model.use_mock": False,
            "model.context_size": 128,
            "model.chat_format": "llama-3",
            "model.max_tokens": 32,
            "model.temperature": 0.0,
            "model.n_gpu_layers": 0,
            "model.hybrid_n_gpu_layers": 1,
            "model.gpu_memory_headroom_percent": 0.1,
            "model.enforce_gpu_memory_headroom": False,
        }
        return values.get(key, default)


class _FakeWorker:
    def __init__(self, name: str, plan: list[str], log: list[str]) -> None:
        self.name = name
        self.plan = plan
        self.log = log
        self.closed = False
        self.calls = 0
        self.pid = 10_000 + int(name.split("-")[-1])

    def is_alive(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True

    def create_chat_completion(self, *, messages: list[dict[str, str]], request_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.closed:
            raise model_manager_module.LlamaCppWorkerDeadError("fake worker liveness failed")
        action = self.plan.pop(0) if self.plan else "ok"
        self.log.append(f"worker={self.name} action={action} request_id={request_id}")
        if action == "request_error":
            raise model_manager_module.LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={"code": "fake_request_error", "method": "create_chat_completion"},
            )
        if action == "eof":
            self.closed = True
            raise model_manager_module.LlamaCppWorkerEOFError("fake worker exited before response")
        if action == "dead":
            self.closed = True
            raise model_manager_module.LlamaCppWorkerDeadError("fake worker liveness failed")
        prompt = messages[0]["content"] if messages else ""
        return {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": f"{OUTPUT_SENTINEL}:{prompt}"}}],
        }


class _FakeRuntimeFactory:
    def __init__(self, plans: list[list[str]]) -> None:
        self.plans = plans
        self.created: list[_FakeWorker] = []
        self.log: list[str] = []

    def import_runtime(self, **_kwargs: Any):
        factory = self

        class _Runtime:
            __file__ = "/fake/site-packages/llama_cpp/__init__.py"

            class Llama:
                def __init__(self, **_llama_kwargs: Any) -> None:
                    if not factory.plans:
                        raise RuntimeError("fake worker replacement unavailable")
                    generation = len(factory.created)
                    worker = _FakeWorker(f"generation-{generation}", factory.plans.pop(0), factory.log)
                    factory.created.append(worker)
                    self._worker = worker
                    self._process = type("_Process", (), {"poll": lambda _self: None if worker.is_alive() else 9})()

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._worker, name)

        return _Runtime


class _LocalRelay:
    def __init__(self, name: str, manager: model_manager_module.ModelManager) -> None:
        self.name = name
        self.manager = manager
        self.ready = False
        self.registered = False
        self.restart_counter_seen = 0
        self.state: dict[str, Any] = {"pending": [], "responses": {}}
        self.logs: list[str] = []

    def start(self) -> None:
        status = self.manager.worker_lifecycle_status()
        self.ready = status["worker_state"] in {"ready", "stopped"}
        self.registered = self.ready
        self.restart_counter_seen = int(status["worker_restart_count"])
        self.logs.append(f"{self.name}.start ready={self.ready} restarts={self.restart_counter_seen}")

    def stop(self) -> None:
        self.ready = False
        self.registered = False
        self.logs.append(f"{self.name}.stop")

    def submit(self, request_id: str, plaintext: str) -> dict[str, Any]:
        encrypted_prompt = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        self.state["pending"].append({"request_id": request_id, "ciphertext": encrypted_prompt})
        try:
            completion = self.manager.create_chat_completion_with_recovery(
                messages=[{"role": "user", "content": plaintext}], request_id=request_id, stream=False
            )
        except Exception as exc:
            status = self.manager.worker_lifecycle_status()
            self.ready = False
            self.registered = False
            self.restart_counter_seen = int(status["worker_restart_count"])
            self.logs.append(f"{self.name}.failure request_id={request_id} error={type(exc).__name__}")
            raise
        status = self.manager.worker_lifecycle_status()
        self.ready = status["worker_state"] == "ready" and bool(status["worker_alive"])
        self.registered = self.ready
        self.restart_counter_seen = int(status["worker_restart_count"])
        encrypted_response = base64.b64encode(
            completion["choices"][0]["message"]["content"].encode("utf-8")
        ).decode("ascii")
        if request_id in self.state["responses"]:
            raise AssertionError(f"duplicate encrypted response for {request_id}")
        self.state["responses"][request_id] = {"ciphertext": encrypted_response}
        self.logs.append(f"{self.name}.success request_id={request_id} restarts={self.restart_counter_seen}")
        return completion

    def diagnostics(self) -> dict[str, Any]:
        status = self.manager.worker_lifecycle_status()
        return {
            "relay": self.name,
            "registered": self.registered,
            "ready": self.ready,
            "worker_state": status["worker_state"],
            "worker_generation": status["worker_generation"],
            "worker_restart_count": status["worker_restart_count"],
            "worker_alive": status["worker_alive"],
            "last_worker_error_code": status["last_worker_error_code"],
        }


def _manager(tmp_root: Path, factory: _FakeRuntimeFactory) -> model_manager_module.ModelManager:
    (tmp_root / "soak-model.gguf").write_bytes(b"fake")
    manager = model_manager_module.ModelManager(_Config(tmp_root))
    manager.requested_compute_mode = "cpu"
    return manager


def _assert_no_plaintext(*objects: Any) -> None:
    combined = "\n".join(str(obj) for obj in objects)
    assert PROMPT_SENTINEL not in combined, "prompt sentinel leaked into diagnostics/log/state"
    assert OUTPUT_SENTINEL not in combined, "output sentinel leaked into diagnostics/log/state"


def _assert_bounds(factory: _FakeRuntimeFactory, *, max_workers: int, baseline_threads: int) -> None:
    assert len(factory.created) <= max_workers, f"created duplicate workers: {len(factory.created)} > {max_workers}"
    assert threading.active_count() <= baseline_threads + 6, "unexpected thread growth in soak harness"


def _run_with_factory(plans: list[list[str]], scenario) -> None:
    baseline_threads = threading.active_count()
    with tempfile.TemporaryDirectory(prefix="token-place-worker-soak-") as tmpdir:
        tmp_root = Path(tmpdir)
        factory = _FakeRuntimeFactory([list(plan) for plan in plans])
        manager = _manager(tmp_root, factory)
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logging.getLogger("model_manager").addHandler(handler)
        try:
            with patch.object(model_manager_module, "_import_llama_cpp_runtime", factory.import_runtime):
                with patch.object(manager, "_resolve_compute_plan", lambda: {
                    "requested_mode": "cpu", "effective_mode": "cpu", "backend_available": "cpu",
                    "backend_selected": "cpu", "backend_used": "cpu", "n_gpu_layers": 0,
                    "fallback_reason": None,
                }):
                    scenario(manager, factory, baseline_threads, log_stream)
        finally:
            logging.getLogger("model_manager").removeHandler(handler)
            with contextlib.suppress(Exception):
                if manager.llm is not None:
                    manager.llm.close()


def _healthy_soak(manager, factory, baseline_threads, log_stream) -> None:
    relay = _LocalRelay("relay-a", manager)
    relay.start()
    for idx in range(REQUEST_COUNT):
        relay.submit(f"healthy-{idx}", f"{PROMPT_SENTINEL}-healthy-{idx}")
        assert len(relay.state["responses"]) == idx + 1
    status = manager.worker_lifecycle_status()
    assert status["worker_generation"] == 0
    assert status["worker_restart_count"] == 0
    assert factory.created[0].calls == REQUEST_COUNT
    _assert_bounds(factory, max_workers=1, baseline_threads=baseline_threads)
    _assert_no_plaintext(relay.state, relay.logs, relay.diagnostics(), factory.log, log_stream.getvalue())


def _request_error_same_generation(manager, factory, baseline_threads, log_stream) -> None:
    relay = _LocalRelay("relay-a", manager)
    relay.start()
    try:
        relay.submit("request-error", f"{PROMPT_SENTINEL}-request-error")
    except model_manager_module.LlamaCppInferenceRequestError:
        pass
    else:  # pragma: no cover
        raise AssertionError("request-scoped exception did not propagate")
    status_after_error = manager.worker_lifecycle_status()
    assert status_after_error["worker_generation"] == 0
    assert status_after_error["worker_restart_count"] == 0
    relay.start()
    relay.submit("after-request-error", f"{PROMPT_SENTINEL}-after-request-error")
    status = manager.worker_lifecycle_status()
    assert status["worker_generation"] == 0
    assert status["worker_restart_count"] == 0
    assert status["last_worker_error_code"] is None
    assert factory.created[0].calls == 2
    _assert_bounds(factory, max_workers=1, baseline_threads=baseline_threads)
    _assert_no_plaintext(relay.state, relay.logs, relay.diagnostics(), factory.log, log_stream.getvalue())


def _dead_worker_replacement(manager, factory, baseline_threads, log_stream) -> None:
    relay = _LocalRelay("relay-a", manager)
    relay.start()
    relay.submit("warm", f"{PROMPT_SENTINEL}-warm")
    factory.created[0].close()
    relay.submit("after-dead", f"{PROMPT_SENTINEL}-after-dead")
    status = manager.worker_lifecycle_status()
    assert status["worker_generation"] == 1
    assert status["worker_restart_count"] == 1
    assert [worker.name for worker in factory.created] == ["generation-0", "generation-1"]
    _assert_bounds(factory, max_workers=2, baseline_threads=baseline_threads)
    _assert_no_plaintext(relay.state, relay.logs, relay.diagnostics(), factory.log, log_stream.getvalue())


def _two_relays_serialized(manager, factory, baseline_threads, log_stream) -> None:
    relays = [_LocalRelay("relay-a", manager), _LocalRelay("relay-b", manager)]
    for relay in relays:
        relay.start()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def call(relay: _LocalRelay, idx: int) -> None:
        barrier.wait(timeout=5)
        relay.submit(f"shared-{idx}", f"{PROMPT_SENTINEL}-shared-{idx}")
        results.append(relay.name)

    threads = [threading.Thread(target=call, args=(relay, idx)) for idx, relay in enumerate(relays)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), "concurrent relay request hung"
    assert sorted(results) == ["relay-a", "relay-b"]
    assert len(factory.created) == 1
    assert factory.created[0].calls == 2
    statuses = [relay.diagnostics() for relay in relays]
    assert all(status["worker_restart_count"] == 0 for status in statuses)
    _assert_bounds(factory, max_workers=1, baseline_threads=baseline_threads)
    _assert_no_plaintext([relay.state for relay in relays], [relay.logs for relay in relays], statuses, factory.log, log_stream.getvalue())


def _persistent_failure_unregisters(manager, factory, baseline_threads, log_stream) -> None:
    relays = [_LocalRelay("relay-a", manager), _LocalRelay("relay-b", manager)]
    for relay in relays:
        relay.start()
    try:
        relays[0].submit("fatal", f"{PROMPT_SENTINEL}-fatal")
    except RuntimeError as exc:
        assert "one restart attempt" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("persistent replacement failure did not fail closed")
    for relay in relays:
        relay.stop()
    statuses = [relay.diagnostics() for relay in relays]
    assert all(not status["registered"] and not status["ready"] for status in statuses)
    assert all(status["worker_state"] == "failed" for status in statuses)
    assert manager.llm is None
    assert len(factory.created) == 2
    assert manager.worker_lifecycle_status()["worker_restart_count"] == 2
    _assert_bounds(factory, max_workers=2, baseline_threads=baseline_threads)
    _assert_no_plaintext([relay.state for relay in relays], [relay.logs for relay in relays], statuses, factory.log, log_stream.getvalue())


def _stop_start_idle_and_recovery(manager, factory, baseline_threads, log_stream) -> None:
    relay = _LocalRelay("relay-a", manager)
    relay.start()
    relay.stop()  # stop during idle polling
    relay.start()
    relay.submit("before-recovery", f"{PROMPT_SENTINEL}-before-recovery")
    factory.created[0].close()
    relay.stop()  # stop while a recovery would be required
    relay.start()
    relay.submit("after-recovery-stop-start", f"{PROMPT_SENTINEL}-after-recovery-stop-start")
    status = manager.worker_lifecycle_status()
    assert status["worker_state"] == "ready"
    assert status["worker_restart_count"] == 1
    assert relay.ready and relay.registered
    _assert_bounds(factory, max_workers=2, baseline_threads=baseline_threads)
    _assert_no_plaintext(relay.state, relay.logs, relay.diagnostics(), factory.log, log_stream.getvalue())


def main() -> int:
    start = time.monotonic()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    scenarios = [
        ("healthy_soak", [["ok"] * REQUEST_COUNT], _healthy_soak),
        ("request_error_same_generation", [["request_error", "ok"]], _request_error_same_generation),
        ("dead_worker_replacement", [["ok"], ["ok"]], _dead_worker_replacement),
        ("two_relays_serialized", [["ok", "ok"]], _two_relays_serialized),
        ("persistent_failure_unregisters", [["dead"], ["eof"], ["ok"]], _persistent_failure_unregisters),
        ("stop_start_idle_and_recovery", [["ok"], ["ok"]], _stop_start_idle_and_recovery),
    ]
    output_lines = []
    try:
        for name, plans, scenario in scenarios:
            _run_with_factory(plans, scenario)
            output_lines.append(f"PASS {name}")
    finally:
        elapsed = time.monotonic() - start
        log_text = "\n".join(output_lines + [f"elapsed_seconds={elapsed:.3f}"])
        assert PROMPT_SENTINEL not in log_text
        assert OUTPUT_SENTINEL not in log_text
        (LOG_DIR / "desktop-worker-soak-fault-e2e.log").write_text(log_text + "\n", encoding="utf-8")
    assert elapsed < 60, f"soak harness exceeded CI budget: {elapsed:.3f}s"
    print("desktop worker soak/fault e2e passed")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("USE_MOCK_LLM", "0")
    raise SystemExit(main())
