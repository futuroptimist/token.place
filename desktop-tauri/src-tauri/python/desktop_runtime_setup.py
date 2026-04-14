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
    error: Optional[str] = None


GPU_MODES = frozenset({"auto", "gpu", "hybrid"})
PIP_INSTALL_TIMEOUT_SECONDS = 300
ENABLE_BOOTSTRAP_ENV = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP"


def _probe_llama_runtime() -> RuntimeProbe:
    payload = detect_llama_runtime_capabilities()

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=bool(payload.get("gpu_offload_supported", False)),
        detected_device=str(payload.get("detected_device", "cpu")),
        error=payload.get("error"),
    )


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Probe runtime by default; install/repair only when explicit bootstrap env is enabled."""

    selected_mode = (mode or "auto").strip().lower()
    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
        }

    before = _probe_llama_runtime()
    if before.gpu_offload_supported and before.backend in {"cuda", "metal"}:
        return {
            "selected_backend": before.backend,
            "fallback_reason": "",
            "runtime_action": "already_supported",
            "detected_device": before.detected_device,
        }

    if os.environ.get(ENABLE_BOOTSTRAP_ENV) != "1":
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"GPU runtime unavailable ({before.error or before.backend}); "
                f"set {ENABLE_BOOTSTRAP_ENV}=1 for one-time bootstrap"
            ),
            "runtime_action": "probe_only",
            "detected_device": before.detected_device or "cpu",
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

        if plan.backend in {"cuda", "metal"}:
            return {
                "selected_backend": plan.backend,
                "fallback_reason": "bootstrap install complete; restart sidecar to activate runtime",
                "runtime_action": f"installed_{plan.backend}_restart_required",
                "detected_device": "cpu",
            }

        if plan.backend == "cpu":
            return {
                "selected_backend": "cpu",
                "fallback_reason": (
                    "GPU runtime unavailable after install attempts; using CPU wheel fallback"
                ),
                "runtime_action": "installed_cpu_fallback",
                "detected_device": "cpu",
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
