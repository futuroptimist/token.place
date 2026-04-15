"""Desktop runtime bootstrap for llama-cpp backend availability."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from desktop_gpu_packaging import LlamaCppInstallPlan, llama_cpp_install_plan_fallbacks


@dataclass(frozen=True)
class RuntimeProbe:
    backend: str
    gpu_offload_supported: bool
    detected_device: str
    interpreter: str
    prefix: str
    llama_module_path: str
    error: Optional[str] = None


GPU_MODES = frozenset({"auto", "gpu", "hybrid"})
PIP_INSTALL_TIMEOUT_SECONDS = 300
REEXEC_GUARD_ENV = "TOKEN_PLACE_DESKTOP_RUNTIME_REEXECED"

_PROBE_SNIPPET = """
import json, sys
payload = {
    "backend": "cpu",
    "gpu_offload_supported": False,
    "detected_device": "cpu",
    "interpreter": sys.executable,
    "prefix": sys.prefix,
    "llama_module_path": "missing",
    "error": None,
}
try:
    import llama_cpp
    payload["llama_module_path"] = getattr(llama_cpp, "__file__", "unknown") or "unknown"
    backend = "cpu"
    if bool(getattr(llama_cpp, "GGML_USE_CUDA", False)):
        backend = "cuda"
    elif bool(getattr(llama_cpp, "GGML_USE_METAL", False)):
        backend = "metal"
    supports_gpu = getattr(llama_cpp, "llama_supports_gpu_offload", None)
    if callable(supports_gpu):
        try:
            payload["gpu_offload_supported"] = bool(supports_gpu())
        except Exception:
            payload["gpu_offload_supported"] = False
    else:
        payload["gpu_offload_supported"] = backend in {"cuda", "metal"}
    if payload["gpu_offload_supported"] and backend == "cpu":
        backend = "metal" if sys.platform == "darwin" else "cuda"
    payload["backend"] = backend
    payload["detected_device"] = backend if payload["gpu_offload_supported"] else "cpu"
except Exception as exc:
    payload["backend"] = "missing"
    payload["detected_device"] = "none"
    payload["error"] = str(exc)
print(json.dumps(payload))
""".strip()


def _probe_llama_runtime() -> RuntimeProbe:
    cmd = [sys.executable, "-c", _PROBE_SNIPPET]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path="missing",
            error=str(exc),
        )

    stdout = (result.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = {
            "backend": "missing",
            "gpu_offload_supported": False,
            "detected_device": "none",
            "interpreter": sys.executable,
            "prefix": sys.prefix,
            "llama_module_path": "missing",
            "error": (result.stderr or "").strip() or "probe parse failure",
        }

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=bool(payload.get("gpu_offload_supported", False)),
        detected_device=str(payload.get("detected_device", "cpu")),
        interpreter=str(payload.get("interpreter", sys.executable)),
        prefix=str(payload.get("prefix", sys.prefix)),
        llama_module_path=str(payload.get("llama_module_path", "missing")),
        error=payload.get("error"),
    )


def _run_pip_install(cmd: list[str], env: dict[str, str]) -> tuple[bool, str]:
    try:
        install = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=PIP_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"pip install timed out after {PIP_INSTALL_TIMEOUT_SECONDS}s"

    if install.returncode == 0:
        return True, (install.stdout or "").strip()

    return False, (install.stderr or install.stdout or "").strip()


def _windows_cuda_source_repair() -> tuple[bool, str]:
    env = os.environ.copy()
    env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
    env["FORCE_CMAKE"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "llama-cpp-python",
        "--force-reinstall",
        "--upgrade",
        "--no-cache-dir",
        "--verbose",
    ]
    return _run_pip_install(cmd, env)


def _summarize_install_error(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "install failed"
    return text.splitlines()[-1][:240]


def _probe_result_payload(probe: RuntimeProbe) -> Dict[str, str]:
    return {
        "detected_device": probe.detected_device or "cpu",
        "interpreter": probe.interpreter,
        "interpreter_prefix": probe.prefix,
        "llama_module_path": probe.llama_module_path,
    }


def maybe_reexec_for_runtime_refresh(runtime_setup: Dict[str, str]) -> None:
    if runtime_setup.get("runtime_action") != "installed_cuda_reexec":
        return
    if os.environ.get(REEXEC_GUARD_ENV) == "1":
        return
    env = os.environ.copy()
    env[REEXEC_GUARD_ENV] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Ensure the sidecar interpreter has a GPU-capable runtime when mode prefers GPU."""

    selected_mode = (mode or "auto").strip().lower()
    before = _probe_llama_runtime()

    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
            **_probe_result_payload(before),
        }

    if before.gpu_offload_supported and before.backend in {"cuda", "metal"}:
        return {
            "selected_backend": before.backend,
            "fallback_reason": "",
            "runtime_action": "already_supported",
            **_probe_result_payload(before),
        }

    if not sys.platform.startswith("win"):
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"GPU runtime unavailable ({before.error or before.backend}); "
                "desktop auto-repair is currently Windows-focused"
            ),
            "runtime_action": "probe_only",
            **_probe_result_payload(before),
        }

    last_error = ""
    source_ok, source_log = _windows_cuda_source_repair()
    if source_ok:
        after = _probe_llama_runtime()
        if after.gpu_offload_supported and after.backend == "cuda":
            return {
                "selected_backend": "cuda",
                "fallback_reason": "installed CUDA runtime; re-executing sidecar",
                "runtime_action": "installed_cuda_reexec",
                **_probe_result_payload(after),
            }
        last_error = (
            "CUDA source reinstall completed but runtime still CPU-only; "
            "check CUDA toolkit/build tools"
        )
    else:
        last_error = _summarize_install_error(source_log)

    target_root = repo_root or Path(__file__).resolve().parents[3]
    requirements_path = target_root / "requirements.txt"
    try:
        plans = llama_cpp_install_plan_fallbacks(
            platform=sys.platform,
            requirements_path=requirements_path,
        )
    except (FileNotFoundError, ValueError):
        plans = _fallback_unpinned_plans(sys.platform)

    for plan in plans:
        env = os.environ.copy()
        env.update(plan.pip_env())
        cmd = [sys.executable, "-m", "pip", "install", *plan.pip_install_args(), plan.package_spec]
        ok, log_output = _run_pip_install(cmd, env)
        if not ok:
            last_error = _summarize_install_error(log_output)
            continue

        after = _probe_llama_runtime()
        if after.gpu_offload_supported and after.backend in {"cuda", "metal"}:
            return {
                "selected_backend": after.backend,
                "fallback_reason": "installed GPU runtime; re-executing sidecar",
                "runtime_action": "installed_cuda_reexec",
                **_probe_result_payload(after),
            }

        if plan.backend == "cpu":
            return {
                "selected_backend": "cpu",
                "fallback_reason": "GPU runtime unavailable after repair; using CPU runtime",
                "runtime_action": "installed_cpu_fallback",
                **_probe_result_payload(after),
            }

    return {
        "selected_backend": "cpu",
        "fallback_reason": before.error or last_error or "unable to install a GPU-capable runtime",
        "runtime_action": "failed",
        **_probe_result_payload(before),
    }


def _fallback_unpinned_plans(platform: str) -> list[LlamaCppInstallPlan]:
    detected_platform = platform.lower()
    if detected_platform.startswith("win"):
        return [
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cuda",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124",
                extra_index_url="https://pypi.org/simple",
                only_binary=True,
                no_binary=False,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cpu",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=True,
                no_binary=False,
            ),
        ]

    if detected_platform == "darwin":
        return [
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="metal",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://abetlen.github.io/llama-cpp-python/whl/metal",
                extra_index_url="https://pypi.org/simple",
                only_binary=True,
                no_binary=False,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="metal",
                package_spec="llama-cpp-python",
                cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
                force_cmake=True,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=False,
                no_binary=True,
            ),
        ]

    return [
        LlamaCppInstallPlan(
            platform=detected_platform,
            backend="cpu",
            package_spec="llama-cpp-python",
            cmake_args=None,
            force_cmake=False,
            index_url=None,
            extra_index_url=None,
            only_binary=False,
            no_binary=False,
        )
    ]
