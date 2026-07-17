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
    LLAMA_CPP_CPU_WHEEL_INDEX_URL,
    LLAMA_CPP_CUDA_124_WHEEL_SHA256,
    LLAMA_CPP_CUDA_124_WHEEL_URL,
    LLAMA_CPP_METAL_WHEEL_INDEX_URL,
    backend_probe_satisfies_install_plan,
    llama_cpp_install_plan,
    llama_cpp_install_plan_fallbacks,
    llama_cpp_requirement_spec,
)


def test_windows_install_plan_is_bundled_cuda_wheel_only():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 1

    gpu_plan = plans[0]
    assert gpu_plan.backend == "cuda"
    assert gpu_plan.package_spec == "llama-cpp-python==0.3.32"
    assert gpu_plan.index_url is None
    assert gpu_plan.only_binary is True
    assert gpu_plan.no_binary is False
    assert gpu_plan.pip_env() == {}
    assert "v0.3.32-cu124" in LLAMA_CPP_CUDA_124_WHEEL_URL
    assert LLAMA_CPP_CUDA_124_WHEEL_URL.endswith("llama_cpp_python-0.3.32-py3-none-win_amd64.whl")
    assert len(LLAMA_CPP_CUDA_124_WHEEL_SHA256) == 64


def test_macos_install_plan_requests_metal_then_cpu_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT / "requirements.txt")
    assert len(plans) == 3

    metal_wheel_plan = plans[0]
    assert metal_wheel_plan.backend == "metal"
    assert metal_wheel_plan.index_url == "https://pypi.org/simple"
    assert metal_wheel_plan.extra_index_url == LLAMA_CPP_METAL_WHEEL_INDEX_URL
    assert metal_wheel_plan.only_binary is True
    assert metal_wheel_plan.force_cmake is False
    assert metal_wheel_plan.no_binary is False
    assert metal_wheel_plan.pip_env() == {}

    metal_source_plan = plans[1]
    assert metal_source_plan.backend == "metal"
    assert metal_source_plan.index_url == "https://pypi.org/simple"
    assert metal_source_plan.only_binary is False
    assert metal_source_plan.force_cmake is True
    assert metal_source_plan.no_binary is True
    cmake_args = metal_source_plan.pip_env()["CMAKE_ARGS"]
    assert metal_source_plan.pip_env()["FORCE_CMAKE"] == "1"
    for flag in (
        "-DGGML_METAL=ON",
        "-DGGML_METAL_EMBED_LIBRARY=ON",
        "-DGGML_NATIVE=OFF",
        "-DGGML_OPENMP=OFF",
        "-DGGML_ACCELERATE=ON",
        "-DGGML_BLAS=ON",
        "-DGGML_BLAS_VENDOR=Apple",
        "-DLLAMA_CURL=OFF",
        "-DLLAMA_OPENSSL=OFF",
    ):
        assert flag in cmake_args

    cpu_fallback = plans[2]
    assert cpu_fallback.backend == "cpu"
    assert cpu_fallback.index_url == "https://pypi.org/simple"
    assert cpu_fallback.extra_index_url == LLAMA_CPP_CPU_WHEEL_INDEX_URL
    assert cpu_fallback.only_binary is True
    assert cpu_fallback.no_binary is False


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
        "llama-cpp-python==0.3.32\n",
        encoding="utf-8",
    )

    assert llama_cpp_requirement_spec(requirements) == "llama-cpp-python==0.3.32"


def test_requirement_spec_accepts_underscore_package_name(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("llama_cpp_python==0.2.90\n", encoding="utf-8")

    assert llama_cpp_requirement_spec(requirements) == "llama-cpp-python==0.2.90"


def test_server_llama_cpp_pin_matches_root_requirements():
    root_spec = llama_cpp_requirement_spec(ROOT / "requirements.txt")
    server_spec = llama_cpp_requirement_spec(ROOT / "config" / "requirements_server.txt")

    assert server_spec == root_spec


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
        package_spec="llama-cpp-python==0.3.32",
        cmake_args=None,
        force_cmake=False,
        index_url="https://example.invalid/simple",
        extra_index_url="https://wheels.example.invalid/simple",
        only_binary=True,
        no_binary=True,
    )

    assert plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://example.invalid/simple",
        "--extra-index-url",
        "https://wheels.example.invalid/simple",
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


def test_windows_install_args_never_request_source_or_cpu_fallback():
    plans = llama_cpp_install_plan_fallbacks(platform="win32", requirements_path=ROOT / "requirements.txt")
    assert [p.backend for p in plans] == ["cuda"]
    assert plans[0].pip_install_args() == ["--upgrade", "--no-cache-dir", "--only-binary", "llama-cpp-python"]


def test_macos_metal_install_args_try_wheel_before_source_build():
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT / "requirements.txt")
    metal_wheel_plan = plans[0]
    metal_source_plan = plans[1]

    assert metal_wheel_plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
        "--extra-index-url",
        LLAMA_CPP_METAL_WHEEL_INDEX_URL,
        "--only-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]
    assert metal_source_plan.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
        "--no-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_macos_cpu_fallback_install_args_only_accept_unpinned_wheels():
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT / "requirements.txt")
    cpu_fallback = plans[2]

    assert cpu_fallback.package_spec == "llama-cpp-python"
    assert cpu_fallback.pip_install_args() == [
        "--upgrade",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
        "--extra-index-url",
        LLAMA_CPP_CPU_WHEEL_INDEX_URL,
        "--only-binary",
        "llama-cpp-python",
        "--prefer-binary",
    ]


def test_llama_cpp_install_plan_uses_current_platform_for_windows(monkeypatch):
    monkeypatch.setattr("desktop_gpu_packaging.sys.platform", "win32")
    plan = llama_cpp_install_plan(requirements_path=ROOT / "requirements.txt")

    assert plan.platform == "win32"
    assert plan.backend == "cuda"
    assert plan.only_binary is True
    assert plan.no_binary is False


def test_llama_cpp_install_plan_darwin_selects_metal_plan_with_pypi_index():
    plan = llama_cpp_install_plan(platform="darwin", requirements_path=ROOT / "requirements.txt")

    assert plan.backend == "metal"
    assert plan.package_spec.startswith("llama-cpp-python==")
    assert plan.index_url == "https://pypi.org/simple"
    assert plan.extra_index_url == LLAMA_CPP_METAL_WHEEL_INDEX_URL
    assert plan.only_binary is True
    assert plan.no_binary is False
    assert plan.force_cmake is False
    assert plan.cmake_args is None


def test_non_desktop_platform_fallbacks_return_only_primary_plan():
    plans = llama_cpp_install_plan_fallbacks(platform="freebsd13", requirements_path=ROOT / "requirements.txt")

    assert len(plans) == 1
    assert plans[0].platform == "freebsd13"
    assert plans[0].backend == "cpu"


def test_pip_env_omits_force_cmake_when_disabled():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.32",
        cmake_args="-DGGML_METAL=on",
        force_cmake=False,
    )

    assert plan.pip_env() == {"CMAKE_ARGS": "-DGGML_METAL=on"}


def test_requirement_spec_strips_spaces_around_version_pin(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("llama-cpp-python== 0.3.32 \n", encoding="utf-8")

    assert llama_cpp_requirement_spec(requirements) == "llama-cpp-python==0.3.32"


def test_backend_probe_satisfies_plan_for_matching_backend():
    plan = LlamaCppInstallPlan(
        platform="win32",
        backend="cuda",
        package_spec="llama-cpp-python==0.3.32",
        cmake_args="-DGGML_CUDA=on",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="cuda", llama_module_path="site-packages/llama_cpp/__init__.py", error=None)

    assert backend_probe_satisfies_install_plan(plan, probe) is True


def test_backend_probe_rejects_mismatched_cuda_backend():
    plan = LlamaCppInstallPlan(
        platform="win32",
        backend="cuda",
        package_spec="llama-cpp-python==0.3.32",
        cmake_args="-DGGML_CUDA=on",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="cpu", llama_module_path="site-packages/llama_cpp/__init__.py", error=None)

    assert backend_probe_satisfies_install_plan(plan, probe) is False


def test_backend_probe_accepts_macos_metal_source_build_with_clean_import_probe():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.32",
        cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="cpu", llama_module_path="site-packages/llama_cpp/__init__.py", error=None)

    assert backend_probe_satisfies_install_plan(plan, probe) is True


def test_backend_probe_rejects_macos_metal_source_build_when_probe_errors():
    plan = LlamaCppInstallPlan(
        platform="darwin",
        backend="metal",
        package_spec="llama-cpp-python==0.3.32",
        cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
        force_cmake=True,
    )
    probe = SimpleNamespace(backend="missing", llama_module_path="missing", error="import failed")

    assert backend_probe_satisfies_install_plan(plan, probe) is False


def test_windows_embedded_runtime_manifest_pins_cuda_wheel_and_dlls():
    import json

    manifest = json.loads(
        (ROOT / "desktop-tauri" / "src-tauri" / "python" / "embedded_python_runtime_windows_x86_64_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["expected_interpreter_path"] == "python.exe"
    assert manifest["expected_architecture"] == "AMD64"
    wheel = manifest["llama_cpp_cuda_wheel"]
    assert wheel == {
        "name": "llama_cpp_python-0.3.32-py3-none-win_amd64.whl",
        "version": "0.3.32",
        "flavor": "cu124",
        "url": LLAMA_CPP_CUDA_124_WHEEL_URL,
        "sha256": LLAMA_CPP_CUDA_124_WHEEL_SHA256,
    }
    for dll in ("python311.dll", "vcruntime140.dll", "llama.dll"):
        assert dll in manifest["required_native_dlls"]
