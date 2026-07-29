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


def test_canonical_model_contract_raises_when_profile_missing(monkeypatch):
    import utils.llm.model_profiles as model_profiles

    monkeypatch.setattr(model_profiles, "get_model_profile", lambda _profile_id: None)
    with pytest.raises(RuntimeError, match="canonical Qwen3 model profile is missing"):
        smoke._canonical_model_contract()


def test_canonical_model_contract_returns_repository_profile():
    profile = smoke._canonical_model_contract()
    assert profile["filename"].endswith(".gguf")
    assert isinstance(profile["artifact_size_bytes"], int)
    assert isinstance(profile["artifact_sha256"], str)


def test_materialize_nsis_requires_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    with pytest.raises(RuntimeError, match="requires Windows"):
        smoke.materialize_nsis(installer, tmp_path / "install")


def test_materialize_nsis_requires_exe_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    bad_suffix = tmp_path / "setup.msi"
    bad_suffix.write_bytes(b"nsis")
    with pytest.raises(RuntimeError, match="must be the built NSIS setup executable"):
        smoke.materialize_nsis(bad_suffix, tmp_path / "install")

    missing = tmp_path / "missing.exe"
    with pytest.raises(RuntimeError, match="must be the built NSIS setup executable"):
        smoke.materialize_nsis(missing, tmp_path / "install")


def test_materialize_nsis_runs_installer_and_returns_single_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    install_root = tmp_path / "install"
    install_root.mkdir()
    calls = []
    monkeypatch.setattr(
        smoke.subprocess, "run", lambda command, **kwargs: calls.append(command) or None
    )

    def _create_install_tree():
        (install_root / "token-place.exe").write_bytes(b"app")
        (install_root / "Uninstall token.place.exe").write_bytes(b"uninstall")
        runtime_dir = install_root / "python-runtime"
        runtime_dir.mkdir()
        (runtime_dir / "python.exe").write_bytes(b"python")

    _create_install_tree()

    result = smoke.materialize_nsis(installer, install_root)

    assert result == (install_root / "token-place.exe").resolve()
    assert len(calls) == 1
    assert calls[0][0] == str(installer.resolve())
    assert "/S" in calls[0]
    assert any(str(arg).startswith("/D=") for arg in calls[0])


def test_materialize_nsis_rejects_ambiguous_exe_count(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    install_root = tmp_path / "install"
    install_root.mkdir()
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **kwargs: None)

    with pytest.raises(RuntimeError, match="exactly one Tauri executable"):
        smoke.materialize_nsis(installer, install_root)

    (install_root / "token-place.exe").write_bytes(b"app")
    (install_root / "second.exe").write_bytes(b"app2")
    with pytest.raises(RuntimeError, match="exactly one Tauri executable"):
        smoke.materialize_nsis(installer, install_root)


def test_run_installed_hardware_gate_runs_uninstaller_when_present(monkeypatch, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")
    calls: list[list[str]] = []
    captured_root: dict[str, Path] = {}

    def fake_materialize(_installer, root):
        captured_root["root"] = root
        (root / "unins000.exe").write_bytes(b"uninstall")
        return root / "token-place.exe"

    monkeypatch.setattr(smoke, "validate_canonical_model", lambda path: path.resolve())
    monkeypatch.setattr(smoke, "materialize_nsis", fake_materialize)
    monkeypatch.setattr(
        smoke.subprocess, "run", lambda command, **kwargs: calls.append([str(v) for v in command])
    )

    smoke.run_installed_hardware_gate(installer, model, "8k-fast")

    assert len(calls) == 2
    assert "test_desktop_operator_ui_e2e.py" in " ".join(calls[0])
    assert calls[1] == [str((captured_root["root"] / "unins000.exe")), "/S"]


def test_run_installed_hardware_gate_recognizes_uninstall_exe_naming(monkeypatch, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")
    calls: list[list[str]] = []
    captured_root: dict[str, Path] = {}

    def fake_materialize(_installer, root):
        captured_root["root"] = root
        # Real NSIS packages commonly emit this exact name, not only unins*.exe.
        (root / "uninstall.exe").write_bytes(b"uninstall")
        return root / "token-place.exe"

    monkeypatch.setattr(smoke, "validate_canonical_model", lambda path: path.resolve())
    monkeypatch.setattr(smoke, "materialize_nsis", fake_materialize)
    monkeypatch.setattr(
        smoke.subprocess, "run", lambda command, **kwargs: calls.append([str(v) for v in command])
    )

    smoke.run_installed_hardware_gate(installer, model, "8k-fast")

    assert len(calls) == 2
    assert calls[1] == [str((captured_root["root"] / "uninstall.exe")), "/S"]


def test_run_installed_hardware_gate_uninstalls_exactly_once_on_harness_failure(monkeypatch, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")
    calls: list[list[str]] = []
    captured_root: dict[str, Path] = {}

    def fake_materialize(_installer, root):
        captured_root["root"] = root
        (root / "unins000.exe").write_bytes(b"uninstall")
        return root / "token-place.exe"

    def fake_run(command, **kwargs):
        calls.append([str(v) for v in command])
        if "test_desktop_operator_ui_e2e.py" in " ".join(str(v) for v in command):
            raise smoke.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(smoke, "validate_canonical_model", lambda path: path.resolve())
    monkeypatch.setattr(smoke, "materialize_nsis", fake_materialize)
    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    with pytest.raises(smoke.subprocess.CalledProcessError):
        smoke.run_installed_hardware_gate(installer, model, "8k-fast")

    assert len(calls) == 2
    assert calls[1] == [str((captured_root["root"] / "unins000.exe")), "/S"]


def test_run_installed_hardware_gate_preserves_primary_exception_when_cleanup_also_fails(
    monkeypatch, tmp_path
):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")

    def fake_materialize(_installer, root):
        (root / "unins000.exe").write_bytes(b"uninstall")
        return root / "token-place.exe"

    def fake_run(command, **kwargs):
        joined = " ".join(str(v) for v in command)
        if "test_desktop_operator_ui_e2e.py" in joined:
            raise smoke.subprocess.CalledProcessError(1, command, output="hardware harness failed")
        raise smoke.subprocess.CalledProcessError(1, command, output="uninstall also failed")

    monkeypatch.setattr(smoke, "validate_canonical_model", lambda path: path.resolve())
    monkeypatch.setattr(smoke, "materialize_nsis", fake_materialize)
    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    with pytest.raises(smoke.subprocess.CalledProcessError) as raised:
        smoke.run_installed_hardware_gate(installer, model, "8k-fast")

    assert raised.value.output == "hardware harness failed"


def test_run_installed_hardware_gate_no_uninstaller_present_is_a_noop(monkeypatch, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")
    calls: list[list[str]] = []

    monkeypatch.setattr(smoke, "validate_canonical_model", lambda path: path.resolve())
    monkeypatch.setattr(smoke, "materialize_nsis", lambda _installer, root: root / "token-place.exe")
    monkeypatch.setattr(
        smoke.subprocess, "run", lambda command, **kwargs: calls.append([str(v) for v in command])
    )

    smoke.run_installed_hardware_gate(installer, model, "8k-fast")

    assert len(calls) == 1


def test_main_success_prints_passed_json(monkeypatch, tmp_path, capsys):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")
    monkeypatch.setattr(smoke, "run_installed_hardware_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        smoke.sys,
        "argv",
        [
            "windows_nvidia_gpu_smoke_test.py",
            "--installer", str(installer),
            "--model", str(model),
            "--context-tier", "8k-fast",
        ],
    )

    assert smoke.main() == 0
    payload = smoke.json.loads(capsys.readouterr().out)
    assert payload == {"result": "passed", "context_tier": "8k-fast"}


def test_main_failure_prints_failed_json_to_stderr(monkeypatch, tmp_path, capsys):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"nsis")
    model = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    model.write_bytes(b"model")

    def raise_error(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(smoke, "run_installed_hardware_gate", raise_error)
    monkeypatch.setattr(
        smoke.sys,
        "argv",
        [
            "windows_nvidia_gpu_smoke_test.py",
            "--installer", str(installer),
            "--model", str(model),
            "--context-tier", "64k-full",
        ],
    )

    assert smoke.main() == 1
    payload = smoke.json.loads(capsys.readouterr().err)
    assert payload == {"result": "failed", "error": "boom"}


def test_ui_hardware_mode_is_fail_closed_and_uses_rust_lifecycle():
    source = UI_PATH.read_text(encoding="utf-8")
    for required in (
        "Start operator",
        "Stop operator",
        "CryptoClient",
        "Operator session ID",
        "did not advance for this operator start",
        "all_supported_layers",
        "non-positive or unrecognized GPU offload",
        "KV cache device is not CUDA",
        "wait_for_operator_log_stop_markers",
        "desktop.compute_node_bridge.unregister.succeeded",
        "desktop.compute_node.bridge_process_exited",
    ):
        assert required in source
    hardware_branch = source.split("if hardware_mode:", 1)[1]
    assert 'env.pop(key, None)' in hardware_branch
    assert 'env["USE_MOCK_LLM"] = "1"' not in hardware_branch.split("else:", 1)[0]


def test_ui_e2e_cleanup_cannot_mask_primary_test_failure():
    source = UI_PATH.read_text(encoding="utf-8")
    finally_block = source.split("\n    finally:", 1)[1].split("\n\n    return 0", 1)[0]
    assert finally_block.count("contextlib.suppress(Exception)") >= 3
    assert "driver.quit()" in finally_block
    assert "terminate_process(tauri_driver)" in finally_block
    assert "terminate_process(relay)" in finally_block


class _Element:
    def __init__(self, text: str):
        self.text = text


class _Driver:
    def __init__(self, values: dict[str, str]):
        self.values = values

    def find_element(self, _by, xpath):
        label = next(label for label in self.values if f"'{label}:'" in xpath)
        return _Element(self.values[label])


def _valid_status_driver(
    *,
    session_id="session-current",
    sequence="3",
    fallback_reason="none",
    # Explicit GPU mode requests n_gpu_layers=-1, which ModelManager surfaces as this
    # exact sentinel (utils/llm/model_manager.py::_resolve_compute_plan), not a layer
    # count -- this is what production actually emits, not invented numeric evidence.
    offloaded_layers="all_supported_layers",
    kv_cache_device="cuda",
    runtime_id="bundled-cpython-3.11-win-x86_64-cu124",
    bundled_runtime_id="bundled-cpython-3.11-win-x86_64-cu124",
    launcher_source="bundled",
    registered="yes",
):
    return _Driver(
        {
            "Requested mode": "gpu",
            "Backend available": "cuda",
            "Backend selected": "cuda",
            "Backend used": "cuda",
            "Context tier": "8k-fast",
            "Worker state": "ready",
            "Worker alive": "yes",
            "Fallback reason": fallback_reason,
            "Runtime ID": runtime_id,
            "Bundled runtime ID": bundled_runtime_id,
            "Launcher source": launcher_source,
            "Interpreter": "python.exe",
            "Registered": registered,
            "Operator session ID": session_id,
            "Sequence": sequence,
            "Readiness diagnostics": f"offloaded_layers={offloaded_layers} kv_cache_device={kv_cache_device}",
        }
    )


def _load_assert_packaged_windows_nvidia_status():
    source = UI_PATH.read_text(encoding="utf-8")
    start = source.index("def _status_value")
    end = source.index("\ndef main(", start)
    namespace = {
        "webdriver": SimpleNamespace(Remote=object),
        "By": SimpleNamespace(XPATH="xpath"),
        "Path": Path,
    }
    exec(source[start:end], namespace)  # noqa: S102 - isolated repository helper
    return namespace["assert_packaged_windows_nvidia_status"]


def test_hardware_status_accepts_current_session_structured_evidence():
    validator = _load_assert_packaged_windows_nvidia_status()
    validator(_valid_status_driver(), "8k-fast", "session-previous")


def test_hardware_status_rejects_stale_or_unbound_session_evidence():
    validator = _load_assert_packaged_windows_nvidia_status()
    with pytest.raises(AssertionError, match="did not advance"):
        validator(_valid_status_driver(session_id="session-previous"), "8k-fast", "session-previous")
    for missing in ("", "pending", "unknown", "none"):
        with pytest.raises(AssertionError, match="concrete operator session"):
            validator(_valid_status_driver(session_id=missing), "8k-fast", "session-previous")


def test_hardware_status_rejects_non_positive_sequence():
    validator = _load_assert_packaged_windows_nvidia_status()
    for bad_sequence in ("0", "-1", "unknown", ""):
        with pytest.raises(AssertionError, match="positive current-session"):
            validator(_valid_status_driver(sequence=bad_sequence), "8k-fast", "session-previous")


def test_hardware_status_rejects_fallback():
    validator = _load_assert_packaged_windows_nvidia_status()
    with pytest.raises(AssertionError):
        validator(_valid_status_driver(fallback_reason="cpu"), "8k-fast", "session-previous")


def test_hardware_status_accepts_full_offload_sentinel():
    # This is what production actually emits for explicit GPU mode
    # (n_gpu_layers=-1 -> "all_supported_layers"), not a layer count.
    validator = _load_assert_packaged_windows_nvidia_status()
    validator(_valid_status_driver(offloaded_layers="all_supported_layers"), "8k-fast", "session-previous")


def test_hardware_status_accepts_positive_numeric_gpu_offload():
    validator = _load_assert_packaged_windows_nvidia_status()
    validator(_valid_status_driver(offloaded_layers="32"), "8k-fast", "session-previous")


def test_hardware_status_rejects_non_positive_or_unrecognized_gpu_offload():
    validator = _load_assert_packaged_windows_nvidia_status()
    for bad_offload in ("0", "-1", "unknown", "", "some_layers", "All_Supported_Layers"):
        with pytest.raises(AssertionError, match="non-positive or unrecognized GPU offload"):
            validator(_valid_status_driver(offloaded_layers=bad_offload), "8k-fast", "session-previous")


def test_hardware_status_rejects_non_cuda_kv_cache_device():
    validator = _load_assert_packaged_windows_nvidia_status()
    for bad_device in ("cpu", "unknown", "none", "", "partial"):
        with pytest.raises(AssertionError, match="KV cache device is not CUDA"):
            validator(_valid_status_driver(kv_cache_device=bad_device), "8k-fast", "session-previous")


def test_hardware_status_rejects_mismatched_or_placeholder_runtime_id():
    validator = _load_assert_packaged_windows_nvidia_status()
    with pytest.raises(AssertionError, match="does not match"):
        validator(
            _valid_status_driver(runtime_id="bundled-cpython-3.11-win-x86_64-cu999"),
            "8k-fast",
            "session-previous",
        )
    for placeholder in ("", "pending", "unknown"):
        with pytest.raises(AssertionError, match="bundled runtime ID was not concrete"):
            validator(_valid_status_driver(bundled_runtime_id=placeholder), "8k-fast", "session-previous")


def test_hardware_status_rejects_non_bundled_launcher_source():
    validator = _load_assert_packaged_windows_nvidia_status()
    for bad_source in ("environment_override", "system_development", "bundled_runtime", ""):
        with pytest.raises(AssertionError, match="Launcher source"):
            validator(_valid_status_driver(launcher_source=bad_source), "8k-fast", "session-previous")


def test_hardware_status_rejects_wrong_interpreter():
    validator = _load_assert_packaged_windows_nvidia_status()
    driver = _valid_status_driver()
    driver.values["Interpreter"] = "python3.exe"
    with pytest.raises(AssertionError, match="bundled Windows interpreter"):
        validator(driver, "8k-fast", "session-previous")


def test_hardware_status_rejects_non_ready_relay_registration():
    validator = _load_assert_packaged_windows_nvidia_status()
    for bad_registered in ("no", "pending", ""):
        with pytest.raises(AssertionError, match="relay registration was not ready"):
            validator(_valid_status_driver(registered=bad_registered), "8k-fast", "session-previous")


def test_workflow_runs_both_installed_hardware_tiers_without_enabling_runner():
    workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(encoding="utf-8")
    job = workflow.split("windows-nvidia-release-gate:", 1)[1].split("\n  publish:", 1)[0]
    assert "if: ${{ false }}" in job
    assert "runs-on: [self-hosted, Windows, X64, NVIDIA, RTX]" in job
    assert job.count("windows_nvidia_gpu_smoke_test.py") == 2
    assert "--context-tier 8k-fast" in job
    assert "--context-tier 64k-full" in job
    assert "--installer $setup.FullName" in job


def test_workflow_has_ordered_execution_prerequisites_before_smoke_test():
    workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(encoding="utf-8")
    job = workflow.split("windows-nvidia-release-gate:", 1)[1].split("\n  publish:", 1)[0]
    steps = ("Checkout", "Set up Python", "Install Python dependencies", "Set up Rust",
              "Install tauri-driver", "Verify native WebDriver prerequisites",
              "Download Windows release artifacts", "Run artifact-backed Windows NVIDIA smoke test")
    positions = [job.index(f"name: {step}") for step in steps]
    assert positions == sorted(positions)
    assert "actions/setup-python@v5" in job
    assert "pip install selenium" in job
    assert "dtolnay/rust-toolchain@stable" in job
    assert "cargo install tauri-driver" in job
    assert "msedgedriver" in job
