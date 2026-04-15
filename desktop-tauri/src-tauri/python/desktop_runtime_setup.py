"""Desktop runtime bootstrap for llama-cpp backend availability."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from desktop_gpu_packaging import LlamaCppInstallPlan, llama_cpp_install_plan_fallbacks
from utils.llm.model_manager import detect_llama_runtime_capabilities


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
ENABLE_BOOTSTRAP_ENV = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP"
RUNTIME_REEXEC_ENV = "TOKEN_PLACE_DESKTOP_RUNTIME_REEXEC"


def _probe_llama_runtime() -> RuntimeProbe:
    payload = detect_llama_runtime_capabilities()

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=bool(payload.get("gpu_offload_supported", False)),
        detected_device=str(payload.get("detected_device", "cpu")),
        python_executable=str(payload.get("python_executable") or sys.executable),
        python_prefix=str(payload.get("python_prefix") or sys.prefix),
        llama_cpp_path=str(payload.get("llama_cpp_path") or "missing"),
        error=payload.get("error"),
    )


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Ensure desktop sidecars run with a GPU-capable llama-cpp runtime when possible."""

    selected_mode = (mode or "auto").strip().lower()
    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
            "python_executable": sys.executable,
            "python_prefix": sys.prefix,
            "llama_cpp_path": "not_loaded",
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
    if os.environ.get(ENABLE_BOOTSTRAP_ENV) == "0":
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"GPU runtime unavailable ({before.error or before.backend}); "
                f"{ENABLE_BOOTSTRAP_ENV}=0 disables automatic bootstrap"
            ),
            "runtime_action": "bootstrap_disabled",
            "detected_device": before.detected_device or "cpu",
            "python_executable": before.python_executable,
            "python_prefix": before.python_prefix,
            "llama_cpp_path": before.llama_cpp_path,
        }

    target_root = repo_root or Path(__file__).resolve().parents[3]
    requirements_path = target_root / "requirements.txt"

    try:
        plans = llama_cpp_install_plan_fallbacks(platform=sys.platform, requirements_path=requirements_path)
    except (FileNotFoundError, ValueError) as exc:
        plans = _fallback_unpinned_plans(sys.platform)
        fallback_setup_error = str(exc)
    else:
        fallback_setup_error = ""
    plans = _prioritized_repair_plans(plans, sys.platform)

    last_install_error = ""

    for plan in plans:
        env = os.environ.copy()
        env.update(plan.pip_env())
        cmd = [sys.executable, "-m", "pip", "install", *plan.pip_install_args(), plan.package_spec]
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
        if install.returncode != 0:
            last_install_error = (install.stderr or install.stdout or "").strip()
            continue

        after = _probe_llama_runtime()
        if plan.backend in {"cuda", "metal"}:
            activated_now = after.gpu_offload_supported and after.backend in {"cuda", "metal"}
            return {
                "selected_backend": after.backend if activated_now else plan.backend,
                "fallback_reason": (
                    ""
                    if activated_now
                    else "bootstrap install complete; restarting sidecar to activate runtime"
                ),
                "runtime_action": (
                    f"installed_{plan.backend}_active"
                    if activated_now
                    else f"installed_{plan.backend}_reexec_required"
                ),
                "detected_device": after.detected_device,
                "python_executable": after.python_executable,
                "python_prefix": after.python_prefix,
                "llama_cpp_path": after.llama_cpp_path,
            }

        if plan.backend == "cpu":
            return {
                "selected_backend": "cpu",
                "fallback_reason": (
                    "GPU runtime unavailable after install attempts; using CPU wheel fallback"
                ),
                "runtime_action": "installed_cpu_fallback",
                "detected_device": "cpu",
                "python_executable": after.python_executable,
                "python_prefix": after.python_prefix,
                "llama_cpp_path": after.llama_cpp_path,
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
        "detected_device": "cpu",
        "python_executable": before.python_executable,
        "python_prefix": before.python_prefix,
        "llama_cpp_path": before.llama_cpp_path,
    }


def _prioritized_repair_plans(
    plans: list[LlamaCppInstallPlan],
    platform: str,
) -> list[LlamaCppInstallPlan]:
    if not platform.lower().startswith("win"):
        return plans
    source_plan = LlamaCppInstallPlan(
        platform=platform.lower(),
        backend="cuda",
        package_spec="llama-cpp-python",
        cmake_args="-DGGML_CUDA=on",
        force_cmake=True,
        index_url=None,
        extra_index_url=None,
        only_binary=False,
        no_binary=True,
        force_reinstall=True,
        verbose=True,
    )
    return [source_plan, *plans]


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
