"""Unit tests for desktop llama-cpp-python packaging contract helpers."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = ROOT / "desktop-tauri" / "src-tauri" / "python"
if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))

from desktop_gpu_packaging import (
    LlamaCppInstallPlan,
    llama_cpp_install_plan,
    llama_cpp_install_plan_fallbacks,
    llama_cpp_requirement_spec,
)


def test_windows_install_plan_requests_cuda_then_cpu_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 4

    gpu_plan = plans[0]
    assert gpu_plan.backend == "cuda"
    assert gpu_plan.package_spec.startswith("llama-cpp-python==")
    assert gpu_plan.index_url == "https://pypi.org/simple"
    assert gpu_plan.extra_index_url is None
    assert gpu_plan.only_binary is False
    assert gpu_plan.no_binary is True
    assert gpu_plan.pip_env() == {"CMAKE_ARGS": "-DGGML_CUDA=on", "FORCE_CMAKE": "1"}

    unpinned_cuda_fallback = plans[1]
    assert unpinned_cuda_fallback.backend == "cuda"
    assert unpinned_cuda_fallback.package_spec.startswith("llama-cpp-python==")
    assert unpinned_cuda_fallback.index_url == "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    assert unpinned_cuda_fallback.extra_index_url == "https://pypi.org/simple"
    assert unpinned_cuda_fallback.only_binary is True
    assert unpinned_cuda_fallback.no_binary is False

    unpinned_cuda_fallback = plans[2]
    assert unpinned_cuda_fallback.backend == "cuda"
    assert unpinned_cuda_fallback.package_spec == "llama-cpp-python"
    assert unpinned_cuda_fallback.index_url == "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    assert unpinned_cuda_fallback.extra_index_url == "https://pypi.org/simple"
    assert unpinned_cuda_fallback.only_binary is True
    assert unpinned_cuda_fallback.no_binary is False

    cpu_fallback = plans[3]
    assert cpu_fallback.backend == "cpu"
    assert cpu_fallback.package_spec == "llama-cpp-python"
    assert cpu_fallback.index_url == "https://pypi.org/simple"
    assert cpu_fallback.extra_index_url is None
    assert cpu_fallback.only_binary is True
    assert cpu_fallback.no_binary is False


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
    assert source_fallback.no_binary is True
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


def test_requirement_spec_parses_comments_and_blank_lines(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# comment\n\n"
        "numpy==2.0.0\n"
        "llama-cpp-python==0.3.16\n",
        encoding="utf-8",
    )

    assert llama_cpp_requirement_spec(requirements) == "llama-cpp-python==0.3.16"


def test_requirement_spec_accepts_underscore_package_name(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("llama_cpp_python==0.2.90\n", encoding="utf-8")

    assert llama_cpp_requirement_spec(requirements) == "llama-cpp-python==0.2.90"


def test_requirement_spec_raises_when_pin_missing(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("numpy==2.0.0\n", encoding="utf-8")

    try:
        llama_cpp_requirement_spec(requirements)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "llama_cpp_python pin not found" in str(exc)


def test_pip_install_args_include_index_and_binary_flags():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.16",
        cmake_args=None,
        force_cmake=False,
        index_url="https://example.invalid/simple",
        extra_index_url="https://pypi.org/simple",
        only_binary=True,
        no_binary=True,
    )

    assert plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://example.invalid/simple",
        "--extra-index-url",
        "https://pypi.org/simple",
        "--only-binary",
        "llama-cpp-python",
        "--no-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_llama_cpp_install_plan_uses_current_platform_by_default(monkeypatch):
    monkeypatch.setattr("desktop_gpu_packaging.sys.platform", "linux")
    plan = llama_cpp_install_plan(requirements_path=ROOT / "requirements.txt")

    assert plan.platform == "linux"
    assert plan.backend == "cpu"


def test_windows_cpu_fallback_install_args_are_binary_only():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    cpu_fallback = plans[3]

    assert cpu_fallback.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
        "--only-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_macos_source_fallback_install_args_force_source_build():
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT / "requirements.txt")
    source_fallback = plans[1]

    assert source_fallback.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
        "--no-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_llama_cpp_install_plan_uses_current_platform_for_windows(monkeypatch):
    monkeypatch.setattr("desktop_gpu_packaging.sys.platform", "win32")
    plan = llama_cpp_install_plan(requirements_path=ROOT / "requirements.txt")

    assert plan.platform == "win32"
    assert plan.backend == "cuda"
    assert plan.only_binary is False
    assert plan.no_binary is True


def test_llama_cpp_install_plan_darwin_selects_metal_wheel_index():
    plan = llama_cpp_install_plan(platform="darwin", requirements_path=ROOT / "requirements.txt")

    assert plan.backend == "metal"
    assert plan.package_spec.startswith("llama-cpp-python==")
    assert plan.index_url == "https://abetlen.github.io/llama-cpp-python/whl/metal"
    assert plan.extra_index_url == "https://pypi.org/simple"
    assert plan.only_binary is True
    assert plan.no_binary is False


def test_non_desktop_platform_fallbacks_return_only_primary_plan():
    plans = llama_cpp_install_plan_fallbacks(platform="freebsd13", requirements_path=ROOT / "requirements.txt")

    assert len(plans) == 1
    assert plans[0].platform == "freebsd13"
    assert plans[0].backend == "cpu"


def test_pip_env_omits_force_cmake_when_disabled():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.16",
        cmake_args="-DGGML_METAL=on",
        force_cmake=False,
    )

    assert plan.pip_env() == {"CMAKE_ARGS": "-DGGML_METAL=on"}


def test_requirement_spec_strips_spaces_around_version_pin(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("llama-cpp-python== 0.3.16 \n", encoding="utf-8")

    assert llama_cpp_requirement_spec(requirements) == "llama-cpp-python==0.3.16"
