import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop-tauri" / "src-tauri" / "python"))

import desktop_gpu_packaging as gpu
import desktop_runtime_setup as setup


def test_windows_install_plan_is_cu124_wheel_only_no_source_or_cpu_fallback():
    plans = gpu.llama_cpp_install_plan_fallbacks("win32", ROOT / "requirements.txt")
    assert len(plans) == 1
    plan = plans[0]
    assert plan.backend == "cuda"
    assert plan.only_binary is True
    assert plan.no_binary is False
    assert plan.force_cmake is False
    assert plan.cmake_args is None
    assert "cu124" in plan.extra_index_url
    args = " ".join(plan.pip_install_args())
    assert "--no-binary" not in args


def test_windows_runtime_manifest_pins_cpython_cu124_wheel_and_dll_inventory():
    manifest = json.loads((ROOT / "desktop-tauri/src-tauri/python/embedded_python_runtime_manifest_windows_x86_64.json").read_text())
    assert manifest["target_triple"] == "x86_64-pc-windows-msvc"
    assert manifest["expected_interpreter_path"] == "python.exe"
    wheel = manifest["llama_cpp_python_wheel"]
    assert wheel["filename"] == "llama_cpp_python-0.3.32-py3-none-win_amd64.whl"
    assert wheel["version"] == "0.3.32"
    assert wheel["cuda"] == "cu124"
    assert wheel["platform_tag"] == "win_amd64"
    assert len(wheel["sha256"]) == 64
    assert {"python311.dll", "llama.dll", "ggml-cuda.dll"}.issubset(set(manifest["required_native_dlls"]))


def test_bundled_windows_runtime_fail_closed_does_not_call_pip_or_emit_cuda_build(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    fake_exe = tmp_path / "resources" / "python-runtime" / "python.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_text("")
    monkeypatch.setattr(setup.sys, "executable", str(fake_exe))
    calls = []
    heartbeats = []
    monkeypatch.setattr(setup, "_run_pip_install", lambda *a, **k: calls.append((a, k)) or (True, "unexpected"))
    monkeypatch.setattr(setup, "_probe_runtime", lambda *a, **k: setup.RuntimeProbe(backend="missing", gpu_offload_supported=False, detected_device="none", interpreter="bundled", prefix="bundled", llama_module_path="missing", error="missing"))
    payload = setup.ensure_desktop_llama_runtime("gpu", repo_root=tmp_path, context_tier="64k-full", heartbeat=lambda e: heartbeats.append(e))
    assert payload["runtime_action"] == "bundled_runtime_invalid"
    assert calls == []
    assert all(e.get("startup_phase") != "cuda_build" for e in heartbeats)
    assert payload["selected_backend"] == "cpu"
