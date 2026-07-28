"""Fail-closed contract tests for the installed Windows RTX release gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = ROOT / "desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py"
UI_PATH = ROOT / "desktop-tauri/scripts/test_desktop_operator_ui_e2e.py"
SPEC = importlib.util.spec_from_file_location("windows_nvidia_gpu_smoke_test", SMOKE_PATH)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def _profile(model: Path, *, size: int | None = None, digest: str | None = None):
    import hashlib

    return {
        "filename": model.name,
        "artifact_size_bytes": model.stat().st_size if size is None else size,
        "artifact_sha256": hashlib.sha256(model.read_bytes()).hexdigest() if digest is None else digest,
    }


def test_canonical_model_requires_exact_name_size_and_hash(monkeypatch, tmp_path):
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"canonical")
    monkeypatch.setattr(smoke, "_canonical_model_contract", lambda: _profile(model))
    assert smoke.validate_canonical_model(model) == model.resolve()

    for bad_model, profile in (
        (tmp_path / "arbitrary.gguf", _profile(model)),
        (model, _profile(model, size=999)),
        (model, _profile(model, digest="0" * 64)),
    ):
        if not bad_model.exists():
            bad_model.write_bytes(b"canonical")
        monkeypatch.setattr(smoke, "_canonical_model_contract", lambda p=profile: p)
        with pytest.raises(RuntimeError):
            smoke.validate_canonical_model(bad_model)


def test_gate_materializes_once_and_runs_installed_ui_harness(monkeypatch, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")
    calls: list[list[str]] = []
    monkeypatch.setattr(smoke, "validate_canonical_model", lambda path: path.resolve())
    monkeypatch.setattr(smoke, "materialize_nsis", lambda _installer, root: root / "token-place.exe")
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **_kwargs: calls.append([str(v) for v in command]))

    smoke.run_installed_hardware_gate(installer, model, "64k-full")

    assert len(calls) == 1
    command = calls[0]
    assert "test_desktop_operator_ui_e2e.py" in " ".join(command)
    assert "--packaged-windows-nvidia-hardware" in command
    assert "--context-tier" in command and "64k-full" in command
    source = SMOKE_PATH.read_text(encoding="utf-8")
    assert "compute_node_bridge.py" not in source
    assert "_run_bridge_oneshot" not in source
    assert "os.environ.copy" not in source


def test_ui_hardware_mode_is_fail_closed_and_uses_rust_lifecycle():
    source = UI_PATH.read_text(encoding="utf-8")
    for required in (
        "Start operator",
        "Stop operator",
        "CryptoClient",
        "physical_device_missing",
        "offloaded_layers=0",
        "kv_cache_device=cpu",
        "wait_for_operator_log_stop_markers",
        "desktop.compute_node_bridge.unregister.succeeded",
        "desktop.compute_node.bridge_process_exited",
    ):
        assert required in source
    hardware_branch = source.split("if hardware_mode:", 1)[1]
    assert 'env.pop(key, None)' in hardware_branch
    assert 'env["USE_MOCK_LLM"] = "1"' not in hardware_branch.split("else:", 1)[0]


class _Element:
    def __init__(self, text: str):
        self.text = text


class _Driver:
    def __init__(self, values: dict[str, str], page: str):
        self.values = values
        self.page_source = page

    def find_element(self, _by, xpath):
        label = next(label for label in self.values if f"'{label}:'" in xpath)
        return _Element(self.values[label])


def _valid_status_driver():
    import tempfile

    operator_log = Path(tempfile.gettempdir()) / "token-place-hardware-contract.log"
    operator_log.write_text(
        "warm load offloaded_layers=40 kv_cache_device=cuda physical_device=rtx4090",
        encoding="utf-8",
    )
    return _Driver(
        {
            "Requested mode": "gpu",
            "Backend available": "cuda",
            "Backend selected": "cuda",
            "Backend used": "cuda",
            "Context tier": "8k-fast",
            "Worker state": "ready",
            "Worker alive": "yes",
            "Runtime ID": "bundled-cpython-3.11-win-x86_64-cu124",
            "Launcher source": "bundled_runtime",
            "Interpreter": "python.exe",
            "Operator debug log": str(operator_log),
        },
        "installed UI status",
    )


def test_hardware_status_rejects_missing_device_cpu_and_fake_fields():
    spec = importlib.util.spec_from_file_location("desktop_ui_e2e_contract", UI_PATH)
    assert spec and spec.loader
    # Avoid importing selenium in this focused unit test; execute just the helper source.
    source = UI_PATH.read_text(encoding="utf-8")
    start = source.index("def _status_value")
    end = source.index("\ndef main(", start)
    namespace = {
        "webdriver": SimpleNamespace(Remote=object),
        "By": SimpleNamespace(XPATH="xpath"),
        "Path": Path,
    }
    exec(source[start:end], namespace)  # noqa: S102 - isolated repository helper
    validator = namespace["assert_packaged_windows_nvidia_status"]
    validator(_valid_status_driver(), "8k-fast")
    for marker in ("physical_device_missing", "offloaded_layers=0", "kv_cache_device=cpu"):
        driver = _valid_status_driver()
        driver.page_source += " warm load offloaded_layers=40 kv_cache_device=cuda physical_device=rtx " + marker
        with pytest.raises(AssertionError):
            validator(driver, "8k-fast")


def test_workflow_runs_both_installed_hardware_tiers_without_enabling_runner():
    workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(encoding="utf-8")
    job = workflow.split("windows-nvidia-release-gate:", 1)[1].split("\n  publish:", 1)[0]
    assert "if: ${{ false }}" in job
    assert job.count("windows_nvidia_gpu_smoke_test.py") == 2
    assert "--context-tier 8k-fast" in job
    assert "--context-tier 64k-full" in job
    assert "--installer $setup.FullName" in job
