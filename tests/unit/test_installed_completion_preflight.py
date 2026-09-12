from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path("desktop-tauri/src-tauri/python/installed_completion_preflight.py")
PYTHON_DIR = SCRIPT.parent.resolve()
sys.path.insert(0, str(PYTHON_DIR))
SPEC = importlib.util.spec_from_file_location("installed_completion_preflight", SCRIPT)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class Manager:
    llm = None

    def __init__(self, *, backend="cuda", attempts=1, smoke="passed"):
        self.last_compute_diagnostics = {
            "backend_used": backend,
            "offloaded_layers": 33,
            "kv_cache_device": backend,
            "api_v1_readiness_completion_smoke_result": smoke,
            "api_v1_readiness_completion_smoke_plain_completion_attempt_count": attempts,
        }


class Runtime:
    def __init__(self, *, backend="cuda", attempts=1, smoke="passed", ready=True,
                 error=None, delay=0, cleanup=True):
        self.model_manager = Manager(backend=backend, attempts=attempts, smoke=smoke)
        self.ready, self.error, self.delay, self.cleanup = ready, error, delay, cleanup

    def ensure_api_v1_runtime_ready(self):
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.ready

    def stop(self, **_kwargs):
        if not self.cleanup:
            raise RuntimeError("secret prompt must never escape")


@pytest.fixture(autouse=True)
def installed_identity(monkeypatch):
    for name, value in {
        "TOKENPLACE_APP_VERSION": "0.1.18", "TOKENPLACE_BUILD_ID": "build",
        "TOKENPLACE_TARGET_TRIPLE": "x86_64-pc-windows-msvc",
        "TOKENPLACE_BUNDLED_RUNTIME_ID": "runtime", "TOKENPLACE_RUNTIME_ID": "runtime",
        "TOKENPLACE_LAUNCHER_SOURCE": "bundled",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def args(tmp_path):
    from utils.llm.model_profiles import get_default_model_profile
    model = tmp_path / get_default_model_profile()["filename"]
    model.write_bytes(b"controlled fixture; not model weights")
    return argparse.Namespace(model=str(model), mode="gpu", context_tier="8k-fast")


def run(args, runtime, *, setup=None):
    return preflight.run_preflight(
        args, runtime_factory=lambda: runtime,
        setup_runtime=setup or (lambda _mode: {"selected_backend": "cuda"}),
    )


def test_success_is_one_gpu_completion_with_cleanup_and_no_side_effects(args):
    code, evidence = run(args, Runtime())
    assert code == 0 and evidence["accepted"] is True
    assert evidence["completion_count"] == 1
    assert evidence["cleanup"] == {"requested": True, "verified": True}
    assert evidence["side_effects"] == {"relay_contacts": 0, "registrations": 0, "benchmark_attempts": 0}


def test_packaged_identity_and_model_mismatch_fail_closed(args, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENPLACE_RUNTIME_ID", "system-python")
    assert run(args, Runtime())[1]["failure_code"] == "packaged_identity_mismatch"
    monkeypatch.setenv("TOKENPLACE_RUNTIME_ID", "runtime")
    args.model = str(tmp_path / "wrong.gguf")
    Path(args.model).write_bytes(b"x")
    assert run(args, Runtime())[1]["failure_code"] == "model_identity_mismatch"


def test_cpu_fallback_and_unobserved_gpu_are_rejected(args):
    assert run(args, Runtime(), setup=lambda _mode: {"selected_backend": "cpu"})[1]["failure_code"] == "cpu_fallback_rejected"
    assert run(args, Runtime(backend="cpu"))[1]["failure_code"] == "gpu_execution_unverified"


def test_duplicate_or_failed_completion_is_rejected(args):
    assert run(args, Runtime(attempts=2))[1]["failure_code"] == "completion_count_invalid"
    assert run(args, Runtime(ready=False, smoke="failed"))[1]["failure_code"] == "completion_failed"


def test_worker_exit_and_deadline_are_bounded(args, monkeypatch):
    assert run(args, Runtime(error=RuntimeError("generated secret")))[1]["failure_code"] == "internal_failure"
    monkeypatch.setattr(preflight, "MODEL_LOAD_DEADLINE_SECONDS", 0.001)
    monkeypatch.setattr(preflight, "GENERATION_DEADLINE_SECONDS", 0.001)
    assert run(args, Runtime(delay=0.03))[1]["failure_code"] == "completion_deadline_exceeded"


def test_cleanup_failure_cannot_be_accepted_and_evidence_is_private(args):
    code, evidence = run(args, Runtime(cleanup=False, error=RuntimeError("prompt=TOP_SECRET ciphertext=ABC")))
    encoded = json.dumps(evidence)
    assert code == 10 and evidence["accepted"] is False
    assert evidence["failure_code"] == "cleanup_failed"
    assert "TOP_SECRET" not in encoded and "ciphertext" not in encoded


def test_evidence_has_strict_versioned_allowlist(args):
    _, evidence = run(args, Runtime())
    assert set(evidence) == {"schema", "accepted", "failure_code", "identity", "configuration",
                             "deadlines_ms", "phases", "timings_ms", "observed_execution",
                             "completion_count", "cleanup", "side_effects"}
    assert evidence["schema"] == "token.place/installed-gpu-completion-preflight/v1"
