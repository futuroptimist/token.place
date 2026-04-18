"""High-level contract test for Windows desktop GPU bootstrap behavior."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = ROOT / "desktop-tauri" / "src-tauri" / "python"
if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_PATH = DESKTOP_PYTHON / "desktop_runtime_setup.py"
SPEC = importlib.util.spec_from_file_location("desktop_runtime_setup_e2e", MODULE_PATH)
desktop_runtime_setup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["desktop_runtime_setup_e2e"] = desktop_runtime_setup
SPEC.loader.exec_module(desktop_runtime_setup)


class _SysStub:
    platform = "win32"
    executable = sys.executable
    prefix = sys.prefix
    argv = [str(MODULE_PATH)]


def _probe(*, backend="cpu", gpu=False, device="cpu"):
    return desktop_runtime_setup.RuntimeProbe(
        backend=backend,
        gpu_offload_supported=gpu,
        detected_device=device,
        interpreter=sys.executable,
        prefix=sys.prefix,
        llama_module_path="C:/Python/Lib/site-packages/llama_cpp/__init__.py",
        error=None,
    )


def test_windows_gpu_bootstrap_contract_prefers_cuda_reinstall_before_cpu_fallback(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, "sys", _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, "_should_attempt_source_repair", lambda: (False, "cooldown"))
    probes = iter([_probe(), _probe(backend="cuda", gpu=True, device="cuda")])
    monkeypatch.setattr(desktop_runtime_setup, "_probe_llama_runtime", lambda: next(probes))
    monkeypatch.setattr(
        desktop_runtime_setup,
        "llama_cpp_install_plan_fallbacks",
        lambda **_kwargs: [
            desktop_runtime_setup.LlamaCppInstallPlan(
                platform="win32",
                backend="cuda",
                package_spec="llama-cpp-python==0.3.16",
                cmake_args=None,
                force_cmake=False,
                index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124",
                extra_index_url="https://pypi.org/simple",
                only_binary=True,
                no_binary=False,
            ),
            desktop_runtime_setup.LlamaCppInstallPlan(
                platform="win32",
                backend="cpu",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=True,
                no_binary=False,
            ),
        ],
    )
    pip_cmds: list[list[str]] = []

    def _run_pip(cmd, env, timeout_seconds=0):
        pip_cmds.append(cmd)
        return True, "ok"

    monkeypatch.setattr(desktop_runtime_setup, "_run_pip_install", _run_pip)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime("auto", repo_root=Path.cwd())

    assert result["runtime_action"] == "installed_cuda_reexec"
    assert result["selected_backend"] == "cuda"
    assert len(pip_cmds) == 1
    assert "--force-reinstall" in pip_cmds[0]
    assert pip_cmds[0][-1] == "llama-cpp-python==0.3.16"
