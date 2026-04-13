"""Helpers for desktop llama-cpp-python GPU packaging/install contract."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LlamaCppInstallPlan:
    """Deterministic install plan for platform-specific llama-cpp-python builds."""

    platform: str
    backend: str
    package_spec: str
    cmake_args: str | None
    force_cmake: bool

    def pip_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.cmake_args:
            env["CMAKE_ARGS"] = self.cmake_args
        if self.force_cmake:
            env["FORCE_CMAKE"] = "1"
        return env


def llama_cpp_requirement_spec(requirements_path: str | Path = "requirements.txt") -> str:
    """Return pinned llama-cpp-python requirement from requirements.txt."""

    path = Path(requirements_path)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = line.replace("-", "_").lower()
        if normalized.startswith("llama_cpp_python=="):
            version = line.split("==", 1)[1].strip()
            return f"llama-cpp-python=={version}"
    raise ValueError(f"llama_cpp_python pin not found in {path}")


def llama_cpp_install_plan(
    platform: str | None = None,
    requirements_path: str | Path = "requirements.txt",
) -> LlamaCppInstallPlan:
    """Return explicit, conservative desktop install plan for llama-cpp-python."""

    detected_platform = (platform or sys.platform).lower()
    package_spec = llama_cpp_requirement_spec(requirements_path)

    if detected_platform.startswith("win"):
        return LlamaCppInstallPlan(
            platform=detected_platform,
            backend="cuda",
            package_spec=package_spec,
            cmake_args="-DGGML_CUDA=on",
            force_cmake=True,
        )

    if detected_platform == "darwin":
        return LlamaCppInstallPlan(
            platform=detected_platform,
            backend="metal",
            package_spec=package_spec,
            cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
            force_cmake=True,
        )

    return LlamaCppInstallPlan(
        platform=detected_platform,
        backend="cpu",
        package_spec=package_spec,
        cmake_args=None,
        force_cmake=False,
    )
