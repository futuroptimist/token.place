"""Unit tests for desktop llama-cpp-python packaging contract helpers."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = ROOT / "desktop-tauri" / "src-tauri" / "python"
if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))

from desktop_gpu_packaging import llama_cpp_install_plan_fallbacks


def test_windows_install_plan_requests_cuda_then_cpu_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 2

    gpu_plan = plans[0]
    assert gpu_plan.backend == "cuda"
    assert gpu_plan.package_spec.startswith("llama-cpp-python==")
    assert gpu_plan.index_url == "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    assert gpu_plan.extra_index_url == "https://pypi.org/simple"
    assert gpu_plan.only_binary is True
    assert gpu_plan.pip_env() == {}

    cpu_fallback = plans[1]
    assert cpu_fallback.backend == "cpu"
    assert cpu_fallback.index_url == "https://pypi.org/simple"
    assert cpu_fallback.extra_index_url is None
    assert cpu_fallback.only_binary is False


def test_macos_install_plan_requests_metal_then_source_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 2

    wheel_plan = plans[0]
    assert wheel_plan.backend == "metal"
    assert wheel_plan.index_url == "https://abetlen.github.io/llama-cpp-python/whl/metal"
    assert wheel_plan.extra_index_url == "https://pypi.org/simple"
    assert wheel_plan.only_binary is True

    source_fallback = plans[1]
    assert source_fallback.backend == "metal"
    assert source_fallback.index_url == "https://pypi.org/simple"
    assert source_fallback.only_binary is False
    assert source_fallback.force_cmake is True
    assert source_fallback.pip_env() == {
        "CMAKE_ARGS": "-DGGML_METAL=on -DGGML_NATIVE=off",
        "FORCE_CMAKE": "1",
    }


def test_linux_install_plan_remains_cpu_default():
    plans = llama_cpp_install_plan_fallbacks(platform="linux", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 1

    plan = plans[0]
    assert plan.backend == "cpu"
    assert plan.pip_env() == {}
    assert plan.pip_install_args() == ["--upgrade", "--no-cache-dir"]
