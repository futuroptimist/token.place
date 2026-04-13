"""Unit tests for desktop llama-cpp-python packaging contract helpers."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = ROOT / "desktop-tauri" / "src-tauri" / "python"
if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))

from desktop_gpu_packaging import llama_cpp_install_plan


def test_windows_install_plan_requests_cuda_build_flags():
    plan = llama_cpp_install_plan(platform="win32", requirements_path=ROOT / "requirements.txt")

    assert plan.backend == "cuda"
    assert plan.package_spec.startswith("llama-cpp-python==")
    assert plan.index_url == "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    assert plan.extra_index_url == "https://pypi.org/simple"
    assert plan.only_binary is True
    assert plan.pip_env() == {}
    assert plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://abetlen.github.io/llama-cpp-python/whl/cu124",
        "--extra-index-url",
        "https://pypi.org/simple",
        "--only-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_macos_install_plan_requests_metal_build_flags():
    plan = llama_cpp_install_plan(platform="darwin", requirements_path=ROOT / "requirements.txt")

    assert plan.backend == "metal"
    assert plan.package_spec.startswith("llama-cpp-python==")
    assert plan.index_url == "https://abetlen.github.io/llama-cpp-python/whl/metal"
    assert plan.extra_index_url == "https://pypi.org/simple"
    assert plan.only_binary is True
    assert plan.pip_env() == {}
    assert plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://abetlen.github.io/llama-cpp-python/whl/metal",
        "--extra-index-url",
        "https://pypi.org/simple",
        "--only-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_linux_install_plan_remains_cpu_default():
    plan = llama_cpp_install_plan(platform="linux", requirements_path=ROOT / "requirements.txt")

    assert plan.backend == "cpu"
    assert plan.pip_env() == {}
    assert plan.pip_install_args() == ["--upgrade", "--no-cache-dir"]
