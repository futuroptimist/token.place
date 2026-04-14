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
    error: Optional[str] = None


GPU_MODES = frozenset({"auto", "gpu", "hybrid"})
PIP_INSTALL_TIMEOUT_SECONDS = 300


def _probe_llama_runtime() -> RuntimeProbe:
    probe_code = """
import json

try:
    import llama_cpp
except Exception as exc:
    print(json.dumps({
        "backend": "missing",
        "gpu_offload_supported": False,
        "detected_device": "none",
        "error": str(exc),
    }))
    raise SystemExit(0)

backend = "cpu"
if bool(getattr(llama_cpp, "GGML_USE_CUDA", False)):
    backend = "cuda"
elif bool(getattr(llama_cpp, "GGML_USE_METAL", False)):
    backend = "metal"

supports = getattr(llama_cpp, "llama_supports_gpu_offload", None)
gpu = False
if callable(supports):
    try:
        gpu = bool(supports())
    except Exception:
        gpu = False
else:
    gpu = backend in {"cuda", "metal"}

print(json.dumps({
    "backend": backend,
    "gpu_offload_supported": gpu,
    "detected_device": backend if gpu else "cpu",
    "error": None,
}))
""".strip()

    result = subprocess.run(
        [sys.executable, "-c", probe_code],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            error=(result.stderr or result.stdout or "probe failed").strip(),
        )

    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            error=(result.stdout or "invalid probe response").strip(),
        )

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=bool(payload.get("gpu_offload_supported", False)),
        detected_device=str(payload.get("detected_device", "cpu")),
        error=payload.get("error"),
    )


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Ensure desktop runs a GPU-capable llama-cpp runtime for GPU-preferring modes."""

    selected_mode = (mode or "auto").strip().lower()
    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
        }

    if os.environ.get("TOKEN_PLACE_DESKTOP_SKIP_RUNTIME_BOOTSTRAP") == "1":
        return {
            "selected_backend": "cpu",
            "fallback_reason": "runtime bootstrap disabled by env",
            "runtime_action": "skipped",
        }

    target_root = repo_root or Path(__file__).resolve().parents[3]
    requirements_path = target_root / "requirements.txt"

    before = _probe_llama_runtime()
    if before.gpu_offload_supported and before.backend in {"cuda", "metal"}:
        return {
            "selected_backend": before.backend,
            "fallback_reason": "",
            "runtime_action": "already_supported",
            "detected_device": before.detected_device,
        }

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

        after = _probe_llama_runtime()
        if after.gpu_offload_supported and after.backend in {"cuda", "metal"}:
            return {
                "selected_backend": after.backend,
                "fallback_reason": "",
                "runtime_action": f"installed_{plan.backend}",
                "detected_device": after.detected_device,
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
