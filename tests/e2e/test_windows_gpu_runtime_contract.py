"""E2E-style regression checks for Windows desktop GPU runtime packaging contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = REPO_ROOT / "desktop-tauri" / "src-tauri" / "python"

if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_windows_cuda_contract_prefers_source_then_cuda_wheels_then_cpu_fallback() -> None:
    desktop_gpu_packaging = _load_module(
        "desktop_gpu_packaging",
        DESKTOP_PYTHON / "desktop_gpu_packaging.py",
    )
    plans = desktop_gpu_packaging.llama_cpp_install_plan_fallbacks(
        platform="win32",
        requirements_path=REPO_ROOT / "requirements.txt",
    )

    # Ordered expectations for deterministic bootstrap behavior:
    # 1) Source build with CUDA flags (README hardware acceleration contract)
    # 2) Pinned CUDA wheel fallback
    # 3) Unpinned CUDA wheel fallback
    # 4) CPU wheel fallback
    assert [plan.backend for plan in plans] == ["cuda", "cuda", "cuda", "cpu"]
    assert plans[0].no_binary is True
    assert plans[0].pip_env() == {"CMAKE_ARGS": "-DGGML_CUDA=on", "FORCE_CMAKE": "1"}
    assert plans[1].only_binary is True
    assert plans[1].index_url == "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    assert plans[2].package_spec == "llama-cpp-python"


def test_packaged_resources_include_requirements_for_runtime_repair() -> None:
    tauri_conf = (REPO_ROOT / "desktop-tauri" / "src-tauri" / "tauri.conf.json").read_text(
        encoding="utf-8"
    )
    assert '"../../requirements.txt"' in tauri_conf
