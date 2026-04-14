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
    index_url: str | None = None
    extra_index_url: str | None = None
    only_binary: bool = False
    no_binary: bool = False

    def pip_install_args(self) -> list[str]:
        args = ["--upgrade", "--no-cache-dir"]
        if self.index_url:
            args.extend(["--index-url", self.index_url])
        if self.extra_index_url:
            args.extend(["--extra-index-url", self.extra_index_url])
        if self.only_binary:
            args.extend(["--only-binary", "llama-cpp-python"])
        if self.no_binary:
            args.extend(["--no-binary", "llama-cpp-python"])
        if self.index_url or self.extra_index_url:
            args.append("--prefer-binary")
        return args

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
            cmake_args=None,
            force_cmake=False,
            index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124",
            extra_index_url="https://pypi.org/simple",
            only_binary=True,
        )

    if detected_platform == "darwin":
        return LlamaCppInstallPlan(
            platform=detected_platform,
            backend="metal",
            package_spec=package_spec,
            cmake_args=None,
            force_cmake=False,
            index_url="https://abetlen.github.io/llama-cpp-python/whl/metal",
            extra_index_url="https://pypi.org/simple",
            only_binary=True,
        )

    return LlamaCppInstallPlan(
        platform=detected_platform,
        backend="cpu",
        package_spec=package_spec,
        cmake_args=None,
        force_cmake=False,
    )


def llama_cpp_install_plan_fallbacks(
    platform: str | None = None,
    requirements_path: str | Path = "requirements.txt",
) -> list[LlamaCppInstallPlan]:
    """Return ordered install plans with conservative fallbacks per platform."""

    primary = llama_cpp_install_plan(platform=platform, requirements_path=requirements_path)
    plans = [primary]

    if primary.platform.startswith("win"):
        # 0.3.16 CUDA indexes publish Linux wheels only; keep desktop CI/release
        # buildable by falling back to a deterministic source build when CUDA
        # wheels are unavailable for the current Python ABI.
        plans.append(
            LlamaCppInstallPlan(
                platform=primary.platform,
                backend="cpu",
                package_spec=primary.package_spec,
                cmake_args=None,
                force_cmake=False,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=False,
                no_binary=True,
            )
        )

    if primary.platform == "darwin":
        # The Metal wheel can intermittently fail integrity checks in CI.
        # Fall back to a deterministic source build with Metal enabled and
        # GGML native tuning disabled to avoid arm64 i8mm compile issues.
        plans.append(
            LlamaCppInstallPlan(
                platform=primary.platform,
                backend="metal",
                package_spec=primary.package_spec,
                cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
                force_cmake=True,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=False,
                no_binary=True,
            )
        )

    return plans
