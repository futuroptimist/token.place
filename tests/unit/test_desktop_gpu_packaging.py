"""Unit tests for desktop llama-cpp-python packaging contract helpers."""

from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = ROOT / "desktop-tauri" / "src-tauri" / "python"
if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))

from desktop_gpu_packaging import (
    LlamaCppInstallPlan,
    backend_probe_satisfies_install_plan,
    llama_cpp_install_plan,
    llama_cpp_install_plan_fallbacks,
    llama_cpp_requirement_spec,
)


def test_windows_install_plan_requests_cuda_then_cpu_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 2

    gpu_plan = plans[0]
    assert gpu_plan.backend == "cuda"
    assert gpu_plan.package_spec.startswith("llama-cpp-python==")
    assert gpu_plan.index_url == "https://pypi.org/simple"
    assert gpu_plan.only_binary is False
    assert gpu_plan.no_binary is True
    assert gpu_plan.pip_env() == {"CMAKE_ARGS": "-DGGML_CUDA=on", "FORCE_CMAKE": "1"}

    cpu_fallback = plans[1]
    assert cpu_fallback.backend == "cpu"
    assert cpu_fallback.package_spec == "llama-cpp-python"
    assert cpu_fallback.index_url == "https://pypi.org/simple"
    assert cpu_fallback.only_binary is False
    assert cpu_fallback.no_binary is False


def test_macos_install_plan_requests_metal_then_source_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 2

    wheel_plan = plans[0]
    assert wheel_plan.backend == "metal"
    assert wheel_plan.index_url == "https://pypi.org/simple"
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
        only_binary=True,
        no_binary=True,
    )

    assert plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://example.invalid/simple",
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


def test_windows_cpu_fallback_install_args_allow_wheel_or_source():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    cpu_fallback = plans[1]

    assert cpu_fallback.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
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


def test_llama_cpp_install_plan_darwin_selects_metal_plan_with_pypi_index():
    plan = llama_cpp_install_plan(platform="darwin", requirements_path=ROOT / "requirements.txt")

    assert plan.backend == "metal"
    assert plan.package_spec.startswith("llama-cpp-python==")
    assert plan.index_url == "https://pypi.org/simple"
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


def test_backend_probe_satisfies_plan_for_matching_backend():
    plan = LlamaCppInstallPlan(
        platform="win32",
        backend="cuda",
        package_spec="llama-cpp-python==0.3.16",
        cmake_args="-DGGML_CUDA=on",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="cuda", llama_module_path="site-packages/llama_cpp/__init__.py", error=None)

    assert backend_probe_satisfies_install_plan(plan, probe) is True


def test_backend_probe_rejects_mismatched_cuda_backend():
    plan = LlamaCppInstallPlan(
        platform="win32",
        backend="cuda",
        package_spec="llama-cpp-python==0.3.16",
        cmake_args="-DGGML_CUDA=on",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="cpu", llama_module_path="site-packages/llama_cpp/__init__.py", error=None)

    assert backend_probe_satisfies_install_plan(plan, probe) is False


def test_backend_probe_accepts_macos_metal_source_build_with_clean_import_probe():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.16",
        cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="cpu", llama_module_path="site-packages/llama_cpp/__init__.py", error=None)

    assert backend_probe_satisfies_install_plan(plan, probe) is True


def test_backend_probe_rejects_macos_metal_source_build_when_probe_errors():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.16",
        cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="missing", llama_module_path="missing", error="import failed")

    assert backend_probe_satisfies_install_plan(plan, probe) is False
