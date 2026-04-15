"""Desktop runtime bootstrap for llama-cpp backend availability."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from desktop_gpu_packaging import (
    LlamaCppInstallPlan,
    llama_cpp_install_plan_fallbacks,
    llama_cpp_requirement_spec,
)


@dataclass(frozen=True)
class RuntimeProbe:
    backend: str
    gpu_offload_supported: bool
    detected_device: str
    python_executable: str
    python_prefix: str
    llama_cpp_path: str
    error: Optional[str] = None


GPU_MODES = frozenset({"auto", "gpu", "hybrid"})
PIP_INSTALL_TIMEOUT_SECONDS = 300
DISABLE_BOOTSTRAP_ENV = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP"
VERBOSE_LOG_ENV = "TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS"
VERBOSE_LLM_LOG_ENV = "TOKEN_PLACE_VERBOSE_LLM_LOGS"


def _probe_llama_runtime() -> RuntimeProbe:
    probe_script = """
import json
import sys

payload = {
    "backend": "missing",
    "gpu_offload_supported": False,
    "detected_device": "none",
    "python_executable": sys.executable,
    "python_prefix": sys.prefix,
    "llama_cpp_path": "missing",
    "error": None,
}
try:
    import llama_cpp
except Exception as exc:
    payload["error"] = str(exc)
else:
    backend = "cpu"
    if bool(getattr(llama_cpp, "GGML_USE_CUDA", False)):
        backend = "cuda"
    elif bool(getattr(llama_cpp, "GGML_USE_METAL", False)):
        backend = "metal"
    supports_gpu = getattr(llama_cpp, "llama_supports_gpu_offload", None)
    gpu_offload_supported = False
    if callable(supports_gpu):
        try:
            gpu_offload_supported = bool(supports_gpu())
        except Exception:
            gpu_offload_supported = False
    else:
        gpu_offload_supported = backend in {"cuda", "metal"}
    if gpu_offload_supported and backend == "cpu":
        backend = "metal" if sys.platform == "darwin" else "cuda"
    payload["backend"] = backend
    payload["gpu_offload_supported"] = gpu_offload_supported
    payload["detected_device"] = backend if gpu_offload_supported else "cpu"
    payload["llama_cpp_path"] = str(getattr(llama_cpp, "__file__", "unknown"))

print(json.dumps(payload))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe_script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            python_executable=sys.executable,
            python_prefix=sys.prefix,
            llama_cpp_path="missing",
            error=(result.stderr or result.stdout or "runtime probe failed").strip(),
        )
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            python_executable=sys.executable,
            python_prefix=sys.prefix,
            llama_cpp_path="missing",
            error=f"runtime probe returned malformed JSON: {exc}",
        )

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=bool(payload.get("gpu_offload_supported", False)),
        detected_device=str(payload.get("detected_device", "cpu")),
        python_executable=str(payload.get("python_executable", sys.executable)),
        python_prefix=str(payload.get("python_prefix", sys.prefix)),
        llama_cpp_path=str(payload.get("llama_cpp_path", "unknown")),
        error=payload.get("error"),
    )


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Ensure a GPU-capable runtime for desktop sidecars when mode prefers GPU."""

    selected_mode = (mode or "auto").strip().lower()
    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
            "python_executable": sys.executable,
            "python_prefix": sys.prefix,
            "llama_cpp_path": "unprobed",
        }

    before = _probe_llama_runtime()
    if before.gpu_offload_supported and before.backend in {"cuda", "metal"}:
        return {
            "selected_backend": before.backend,
            "fallback_reason": "",
            "runtime_action": "already_supported",
            "detected_device": before.detected_device,
            "python_executable": before.python_executable,
            "python_prefix": before.python_prefix,
            "llama_cpp_path": before.llama_cpp_path,
        }

    if os.environ.get(DISABLE_BOOTSTRAP_ENV) == "1":
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"GPU runtime unavailable ({before.error or before.backend}); "
                f"{DISABLE_BOOTSTRAP_ENV}=1 disabled automatic repair"
            ),
            "runtime_action": "auto_repair_disabled",
            "detected_device": before.detected_device or "cpu",
            "python_executable": before.python_executable,
            "python_prefix": before.python_prefix,
            "llama_cpp_path": before.llama_cpp_path,
        }

    target_root = repo_root or Path(__file__).resolve().parents[3]
    requirements_path = target_root / "requirements.txt"

    try:
        plans = llama_cpp_install_plan_fallbacks(
            platform=sys.platform,
            requirements_path=requirements_path,
        )
    except (FileNotFoundError, ValueError) as exc:
        plans = _fallback_unpinned_plans(sys.platform)
        fallback_setup_error = str(exc)
    else:
        fallback_setup_error = ""

    if sys.platform.lower().startswith("win"):
        plans = _windows_source_repair_plans(requirements_path) + plans

    last_install_error = ""
    last_install_summary = ""
    verbose_logs = os.getenv(VERBOSE_LOG_ENV) == "1" or os.getenv(VERBOSE_LLM_LOG_ENV) == "1"

    for plan in plans:
        env = os.environ.copy()
        env.update(plan.pip_env())
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--verbose",
            *plan.pip_install_args(),
            plan.package_spec,
        ]
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
            last_install_error = (
                f"pip install timed out after {PIP_INSTALL_TIMEOUT_SECONDS}s for backend={plan.backend}"
            )
            continue
        last_install_summary = (
            f"backend={plan.backend} exit_code={install.returncode} package={plan.package_spec}"
        )
        if install.returncode != 0:
            last_install_error = (install.stderr or install.stdout or "").strip()
            continue

        after = _probe_llama_runtime()
        if after.gpu_offload_supported and after.backend in {"cuda", "metal"}:
            return {
                "selected_backend": after.backend,
                "fallback_reason": "",
                "runtime_action": f"installed_{after.backend}",
                "detected_device": after.detected_device,
                "python_executable": after.python_executable,
                "python_prefix": after.python_prefix,
                "llama_cpp_path": after.llama_cpp_path,
                "install_summary": last_install_summary,
                "install_debug": (
                    (install.stderr or install.stdout or "").strip() if verbose_logs else ""
                ),
            }

        if plan.backend == "cpu" and not after.gpu_offload_supported:
            return {
                "selected_backend": "cpu",
                "fallback_reason": (
                    "GPU runtime unavailable after install attempts; using CPU wheel fallback"
                ),
                "runtime_action": "installed_cpu_fallback",
                "detected_device": after.detected_device or "cpu",
                "python_executable": after.python_executable,
                "python_prefix": after.python_prefix,
                "llama_cpp_path": after.llama_cpp_path,
                "install_summary": last_install_summary,
                "install_debug": (
                    (install.stderr or install.stdout or "").strip() if verbose_logs else ""
                ),
            }

    return {
        "selected_backend": "cpu",
        "fallback_reason": (
            before.error
            or fallback_setup_error
            or last_install_error
            or "unable to install a GPU-capable llama-cpp runtime"
        ),
        "runtime_action": "failed",
        "detected_device": before.detected_device or "cpu",
        "python_executable": before.python_executable,
        "python_prefix": before.python_prefix,
        "llama_cpp_path": before.llama_cpp_path,
        "install_summary": last_install_summary,
    }


def _windows_source_repair_plans(requirements_path: Path) -> list[LlamaCppInstallPlan]:
    try:
        package_spec = llama_cpp_requirement_spec(requirements_path)
    except (FileNotFoundError, ValueError):
        package_spec = "llama-cpp-python"
    return [
        LlamaCppInstallPlan(
            platform="win32",
            backend="cuda",
            package_spec=package_spec,
            cmake_args="-DGGML_CUDA=on",
            force_cmake=True,
            index_url=None,
            extra_index_url=None,
            only_binary=False,
            no_binary=True,
        )
    ]


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
