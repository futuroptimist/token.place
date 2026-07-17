import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PYTHON_MODULE_DIR = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python'
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(PYTHON_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_MODULE_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
MODULE_PATH = PYTHON_MODULE_DIR / 'desktop_runtime_setup.py'
SPEC = importlib.util.spec_from_file_location('desktop_runtime_setup', MODULE_PATH)
desktop_runtime_setup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules['desktop_runtime_setup'] = desktop_runtime_setup
SPEC.loader.exec_module(desktop_runtime_setup)



def test_packaged_runtime_setup_imports_utils_from_resources_root(tmp_path) -> None:
    resources_root = tmp_path / 'token.place desktop.app' / 'Contents' / 'Resources'
    python_dir = resources_root / 'python'
    utils_llm_dir = resources_root / 'utils' / 'llm'
    python_dir.mkdir(parents=True)
    utils_llm_dir.mkdir(parents=True)

    for name in ('desktop_runtime_setup.py', 'desktop_gpu_packaging.py'):
        source = PYTHON_MODULE_DIR / name
        (python_dir / name).write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    (resources_root / 'utils' / '__init__.py').write_text('', encoding='utf-8')
    (utils_llm_dir / '__init__.py').write_text('', encoding='utf-8')
    helper = REPO_ROOT / 'utils' / 'llm' / 'llama_module_identity.py'
    (utils_llm_dir / 'llama_module_identity.py').write_text(helper.read_text(encoding='utf-8'), encoding='utf-8')

    env = os.environ.copy()
    env['PYTHONPATH'] = str(python_dir)
    result = subprocess.run(
        [sys.executable, '-B', '-c', 'import desktop_runtime_setup; print(desktop_runtime_setup.__name__)'],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'desktop_runtime_setup'


def test_packaged_runtime_setup_imports_without_bundled_utils(tmp_path) -> None:
    resources_root = tmp_path / 'token.place desktop.app' / 'Contents' / 'Resources'
    python_dir = resources_root / 'python'
    python_dir.mkdir(parents=True)

    for name in ('desktop_runtime_setup.py', 'desktop_gpu_packaging.py'):
        source = PYTHON_MODULE_DIR / name
        (python_dir / name).write_text(source.read_text(encoding='utf-8'), encoding='utf-8')

    env = {'PYTHONPATH': str(python_dir), 'PATH': os.environ.get('PATH', '')}
    result = subprocess.run(
        [
            sys.executable,
            '-B',
            '-c',
            (
                'import desktop_runtime_setup as d; '
                'print(d.llama_module_identity_from_path("/tmp/llama_cpp/__init__.py").startswith("sha256:"))'
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'True'


class _SysStub:
    platform = 'win32'
    executable = sys.executable
    prefix = sys.prefix
    argv = [str(MODULE_PATH)]


def _probe(*, backend='cpu', gpu=False, device='cpu', error=None, yarn=False, resolver='unsupported', version='0.3.32'):
    return desktop_runtime_setup.RuntimeProbe(
        backend=backend,
        gpu_offload_supported=gpu,
        detected_device=device,
        interpreter=sys.executable,
        prefix=sys.prefix,
        llama_module_path='C:/Python/Lib/site-packages/llama_cpp/__init__.py',
        error=error,
        llama_cpp_python_version=version,
        yarn_rope_supported=yarn,
        yarn_resolver_source=resolver,
        rope_scaling_type_supported=yarn,
        yarn_ext_factor_supported=yarn,
        yarn_orig_ctx_supported=yarn,
    )




def _install_fake_probe_popen(monkeypatch, result_or_exc, captured=None):
    class _FakeProbeProcess:
        pid = 24680
        def __init__(self, cmd, **kwargs):
            if isinstance(result_or_exc, BaseException):
                raise result_or_exc
            if captured is not None:
                captured['cmd'] = cmd
                captured['cwd'] = kwargs.get('cwd')
                captured['env'] = kwargs.get('env', {})
            self.returncode = getattr(result_or_exc, 'returncode', 0)
            stdout = getattr(result_or_exc, 'stdout', '') or ''
            stderr = getattr(result_or_exc, 'stderr', '') or ''
            if stdout.startswith('{'):
                stdout = desktop_runtime_setup.PROBE_RESULT_PREFIX.decode() + stdout + '\n'
            self.stdout = io.BytesIO(stdout.encode())
            self.stderr = io.BytesIO(stderr.encode())
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            return self.returncode
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', _FakeProbeProcess)

@pytest.fixture(autouse=True)
def _default_desktop_arch(monkeypatch):
    """Keep desktop runtime tests isolated from host architecture and managed-site state."""

    original_sys_path = list(sys.path)
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', raising=False)
    monkeypatch.setattr(desktop_runtime_setup.platform_module, 'machine', lambda: 'AMD64')
    try:
        yield
    finally:
        os.environ.pop('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', None)
        sys.path[:] = original_sys_path


def test_pip_source_build_timeout_env_uses_default_for_malformed_values(monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_PIP_SOURCE_BUILD_TIMEOUT_SECONDS', '30s')
    assert (
        desktop_runtime_setup._parse_positive_int_env(
            'TOKEN_PLACE_DESKTOP_PIP_SOURCE_BUILD_TIMEOUT_SECONDS',
            desktop_runtime_setup.DEFAULT_PIP_SOURCE_BUILD_TIMEOUT_SECONDS,
        )
        == desktop_runtime_setup.DEFAULT_PIP_SOURCE_BUILD_TIMEOUT_SECONDS
    )


def test_pip_source_build_timeout_env_accepts_positive_integer(monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_PIP_SOURCE_BUILD_TIMEOUT_SECONDS', '42')
    assert (
        desktop_runtime_setup._parse_positive_int_env(
            'TOKEN_PLACE_DESKTOP_PIP_SOURCE_BUILD_TIMEOUT_SECONDS',
            desktop_runtime_setup.DEFAULT_PIP_SOURCE_BUILD_TIMEOUT_SECONDS,
        )
        == 42
    )

def test_skip_runtime_bootstrap_for_cpu_mode(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('cpu')
    assert result['runtime_action'] == 'skipped'
    assert result['selected_backend'] == 'cpu'
    recorded = json.loads(os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV])
    assert recorded['runtime_action'] == 'skipped'
    assert 'llama_module_path' not in recorded
    assert 'llama_module_path' not in result


def test_windows_runtime_bootstrap_auto_repairs_and_requests_reexec(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    probes = iter([_probe(), _probe(backend='cuda', gpu=True, device='cuda')])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (True, 'ok')
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['selected_backend'] == 'cuda'


def test_windows_missing_runtime_bootstrap_can_repair_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    probes = iter([
        _probe(backend='missing', gpu=False, device='none', error="No module named 'llama_cpp'"),
        _probe(backend='cuda', gpu=True, device='cuda'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    repair_invoked = {'value': False}

    def _repair(_requirements_path):
        repair_invoked['value'] = True
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_windows_cuda_source_repair', _repair)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert repair_invoked['value'] is True
    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['selected_backend'] == 'cuda'


def test_macos_metal_already_supported_reports_metal_action(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='metal', gpu=True, device='metal'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'metal'
    assert result['runtime_action'] == 'metal_already_supported'


def test_macos_metal_already_supported_requires_yarn_for_qwen_64k(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    probes = iter([
        _probe(backend='metal', gpu=True, device='metal', yarn=False),
        _probe(backend='metal', gpu=True, device='metal', yarn=True, resolver='numeric_fallback'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plan = desktop_runtime_setup.LlamaCppInstallPlan(
        platform='darwin',
        backend='metal',
        package_spec='llama-cpp-python==0.3.32',
        cmake_args='-DGGML_METAL=on',
        force_cmake=True,
        index_url='https://pypi.org/simple',
        only_binary=False,
        no_binary=True,
    )
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [plan])
    installs = []
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *args, **kwargs: (installs.append(args) or (True, 'ok')))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT, context_tier='64k-full')

    assert installs
    assert result['runtime_action'] == 'installed_metal_reexec'
    assert result['yarn_rope_supported'] == 'true'
    assert result['yarn_resolver_source'] == 'numeric_fallback'


def test_macos_metal_already_supported_skips_yarn_probe_for_qwen_8k(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='metal', gpu=True, device='metal', yarn=False),
    )
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: pytest.fail('unexpected install'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT, context_tier='8k-fast')

    assert result['runtime_action'] == 'metal_already_supported'
    assert result['yarn_rope_supported'] == 'false'

def test_already_supported_runtime_prepends_dependency_target(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    dependency_target = tmp_path / 'desktop-site'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (dependency_target, None),
    )
    active_sys_path = []
    monkeypatch.setattr(desktop_runtime_setup.sys, 'path', active_sys_path, raising=False)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='metal', gpu=True, device='metal'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path)

    assert result['runtime_action'] == 'metal_already_supported'
    assert active_sys_path[0] == str(dependency_target)


def test_macos_missing_metal_runtime_bootstrap_attempts_metal_plan(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    probes = iter([
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal runtime missing'),
        _probe(backend='metal', gpu=True, device='metal'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plan = desktop_runtime_setup.LlamaCppInstallPlan(
        platform='darwin',
        backend='metal',
        package_spec='llama-cpp-python==0.3.32',
        cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off',
        force_cmake=True,
        index_url='https://pypi.org/simple',
        only_binary=False,
        no_binary=True,
    )
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [plan])
    captured = {}

    def _capture_install(cmd, env, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout'] = kwargs.get('timeout_seconds')
        captured['startup_phase'] = kwargs.get('startup_phase')
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'metal'
    assert result['runtime_action'] == 'installed_metal_reexec'
    assert captured['startup_phase'] == 'runtime_install'
    assert '--target' in captured['cmd']
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_METAL=on -DGGML_NATIVE=off'
    assert captured['env']['FORCE_CMAKE'] == '1'
    assert captured['timeout'] == desktop_runtime_setup.PIP_SOURCE_BUILD_TIMEOUT_SECONDS


def test_macos_metal_source_install_clean_cpu_probe_reexecs_auto(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    probes = iter([
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal runtime missing'),
        _probe(backend='cpu', gpu=False, device='cpu', error=None),
        _probe(backend='cpu', gpu=False, device='cpu', error=None),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='cpu', package_spec='llama-cpp-python==0.3.32',
            cmake_args=None, force_cmake=False, index_url='https://pypi.org/simple',
            only_binary=True, no_binary=False,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    install_calls = []

    def _install(cmd, env, **kwargs):
        install_calls.append(cmd)
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert len(install_calls) == 1
    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'installed_metal_reexec'
    assert 're-executing sidecar for hardware probe' in result['fallback_reason']
    assert result['detected_device'] == 'cpu'
    assert 'llama_module_path' not in result
    auto_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('auto', result)
    hybrid_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('hybrid', result)
    assert auto_message is None
    assert hybrid_message is None


def test_macos_metal_source_install_clean_cpu_probe_fails_explicit_gpu_before_reexec(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    probes = iter([
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal runtime missing'),
        _probe(backend='cpu', gpu=False, device='cpu', error=None),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='cpu', package_spec='llama-cpp-python==0.3.32',
            cmake_args=None, force_cmake=False, index_url='https://pypi.org/simple',
            only_binary=True, no_binary=False,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    install_calls = []

    def _install(cmd, env, **kwargs):
        install_calls.append(cmd)
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu', repo_root=REPO_ROOT)

    assert len(install_calls) == 1
    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'metal_install_failed'
    assert 'explicit GPU mode requires the follow-up probe to report GPU offload' in result['fallback_reason']
    assert result['detected_device'] == 'cpu'
    message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('gpu', result)
    assert message and 'metal_install_failed' in message


def test_macos_metal_install_unsatisfied_cpu_probe_falls_back_to_cpu_in_auto(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    probes = iter([
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal runtime missing'),
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal still unavailable'),
        _probe(backend='cpu', gpu=False, device='cpu', error=None),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='cpu', package_spec='llama-cpp-python',
            cmake_args=None, force_cmake=False, index_url='https://pypi.org/simple',
            only_binary=False, no_binary=False,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    monkeypatch.setattr(desktop_runtime_setup, 'backend_probe_satisfies_install_plan', lambda *_: False)
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'metal_cpu_fallback'
    assert 'follow-up probe reported backend=cpu' in result['fallback_reason']
    assert 'using CPU runtime' in result['fallback_reason']
    assert desktop_runtime_setup.desktop_gpu_runtime_failure_message('auto', result) is None


def test_macos_cpu_fallback_fails_when_follow_up_probe_is_not_importable(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    probes = iter([
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal runtime missing'),
        _probe(backend='cpu', gpu=False, device='cpu', error='Metal still unavailable'),
        _probe(backend='missing', gpu=False, device='none', error="No module named 'llama_cpp'"),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='cpu', package_spec='llama-cpp-python',
            cmake_args=None, force_cmake=False, index_url='https://pypi.org/simple',
            only_binary=False, no_binary=False,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    monkeypatch.setattr(desktop_runtime_setup, 'backend_probe_satisfies_install_plan', lambda *_: False)
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'metal_install_failed'
    assert 'CPU runtime install completed but follow-up probe could not import llama_cpp' in result['fallback_reason']
    assert "No module named 'llama_cpp'" in result['fallback_reason']


def test_macos_runtime_install_uses_writable_dependency_target(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    runtime_root = tmp_path / 'Token Place.app' / 'Contents' / 'Resources'
    runtime_root.mkdir(parents=True)
    dependency_target = tmp_path / 'Application Support' / 'token.place' / 'python site'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (dependency_target, None),
    )
    probes = iter([
        _probe(backend='missing', gpu=False, device='none', error="No module named 'llama_cpp'"),
        _probe(backend='metal', gpu=True, device='metal'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plan = desktop_runtime_setup.LlamaCppInstallPlan(
        platform='darwin', backend='metal', package_spec='llama-cpp-python==0.3.32',
        cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
        index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
    )
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [plan])
    captured = {}

    def _capture_install(cmd, env, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['startup_phase'] = kwargs.get('startup_phase')
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=runtime_root)

    assert result['runtime_action'] == 'installed_metal_reexec'
    assert captured['startup_phase'] == 'runtime_install'
    assert '--target' in captured['cmd']
    assert captured['cmd'][captured['cmd'].index('--target') + 1] == str(dependency_target)
    assert str(dependency_target) in captured['env']['PYTHONPATH']
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_METAL=on -DGGML_NATIVE=off'


def test_macos_clt_python_missing_llama_cpp_reports_actionable_diagnostics(monkeypatch, tmp_path):
    class _CltSysStub(_PlatformStub):
        executable = '/Library/Developer/CommandLineTools/usr/bin/python3'
        prefix = '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9'
        base_prefix = prefix
        argv = [str(MODULE_PATH)]

    monkeypatch.setattr(desktop_runtime_setup, 'sys', _CltSysStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    dependency_target = tmp_path / 'token place deps'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (dependency_target, None),
    )
    before = desktop_runtime_setup.RuntimeProbe(
        backend='missing',
        gpu_offload_supported=False,
        detected_device='none',
        interpreter=_CltSysStub.executable,
        prefix=_CltSysStub.prefix,
        base_prefix=_CltSysStub.base_prefix,
        python_version='3.9.6',
        dependency_target=str(dependency_target),
        pip_version='pip unavailable (ensurepip missing)',
        llama_module_path='missing',
        error="No module named 'llama_cpp'",
    )
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: before)
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        )
    ])
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (False, 'stderr_tail=cmake: command not found'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu', repo_root=tmp_path)

    assert result['runtime_action'] == 'metal_install_failed'
    assert '/Library/Developer/CommandLineTools/usr/bin/python3' in result['fallback_reason']
    assert 'python_version=3.9.6' in result['fallback_reason']
    assert f'dependency_target={dependency_target}' in result['fallback_reason']
    assert 'pip unavailable' in result['fallback_reason']
    assert 'stderr_tail=cmake: command not found' in result['fallback_reason']

def test_macos_bootstrap_disabled_reports_metal_probe_only(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='cpu', gpu=False, device='cpu', error='Metal runtime missing'),
    )
    invoked = {'pip': False}
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (invoked.update(pip=True), '') and (False, 'unexpected'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'metal_probe_only'
    assert desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV in result['fallback_reason']
    assert 'expected_backend=metal' in result['fallback_reason']
    assert invoked['pip'] is False


def test_macos_bootstrap_disabled_missing_llama_cpp_fails_before_probe_only(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(
            backend='missing',
            gpu=False,
            device='none',
            error="No module named 'llama_cpp'",
        ),
    )
    invoked = {'pip': False}
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (invoked.update(pip=True), '') and (False, 'unexpected'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'failed'
    assert 'desktop model runtime dependency unavailable' in result['fallback_reason']
    assert 'llama_module_path=' not in result['fallback_reason']
    assert invoked['pip'] is False

def test_macos_missing_runtime_failure_message_is_fatal_for_auto_and_hybrid(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setattr(desktop_runtime_setup.platform_module, 'machine', lambda: 'arm64')
    runtime_setup = {
        'selected_backend': 'cpu',
        'runtime_action': 'failed',
        'fallback_reason': "desktop model runtime dependency unavailable (No module named 'llama_cpp')",
    }

    auto_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('auto', runtime_setup)
    hybrid_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('hybrid', runtime_setup)

    assert auto_message is not None
    assert hybrid_message is not None
    assert "No module named 'llama_cpp'" in auto_message
    assert 'Metal' in auto_message


def test_macos_metal_install_failure_cpu_fallback_recovers_auto_and_hybrid(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='cpu', package_spec='llama-cpp-python',
            cmake_args=None, force_cmake=False, index_url='https://pypi.org/simple',
            only_binary=False, no_binary=False,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    outcomes = iter([(False, 'metal compile failed'), (True, 'cpu ok')])
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: next(outcomes))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'metal_cpu_fallback'
    assert 'using CPU runtime' in result['fallback_reason']
    auto_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('auto', result)
    hybrid_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('hybrid', result)
    gpu_message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('gpu', result)
    assert auto_message is None
    assert hybrid_message is None
    assert gpu_message and 'metal_cpu_fallback' in gpu_message


def test_macos_metal_install_failure_is_fatal_for_gpu(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe(error='Metal missing'))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='metal', package_spec='llama-cpp-python',
            cmake_args='-DGGML_METAL=on -DGGML_NATIVE=off', force_cmake=True,
            index_url='https://pypi.org/simple', only_binary=False, no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin', backend='cpu', package_spec='llama-cpp-python',
            cmake_args=None, force_cmake=False, index_url='https://pypi.org/simple',
            only_binary=False, no_binary=False,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    install_calls = []

    def _fail_install(cmd, env, **kwargs):
        install_calls.append(cmd)
        return False, 'metal compile failed'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _fail_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu', repo_root=REPO_ROOT)
    message = desktop_runtime_setup.desktop_gpu_runtime_failure_message('gpu', result)

    assert len(install_calls) == 1
    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'metal_install_failed'
    assert 'Metal runtime install failed' in result['fallback_reason']
    assert message and 'Metal' in message


def test_runtime_root_prefers_token_place_python_import_root(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'resources'
    (runtime_root / 'utils').mkdir(parents=True)
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(runtime_root))

    resolved = desktop_runtime_setup._resolve_runtime_root()

    assert resolved == runtime_root.resolve()


def test_runtime_root_prefers_token_place_python_import_root_with_config_py(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'token-place-root'
    runtime_root.mkdir(parents=True)
    (runtime_root / 'config.py').write_text('# config marker\n', encoding='utf-8')
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(runtime_root))

    resolved = desktop_runtime_setup._resolve_runtime_root()

    assert resolved == runtime_root.resolve()


def test_runtime_root_with_invalid_env_var_warns_and_falls_back(monkeypatch, tmp_path, capsys):
    script_path = tmp_path / 'isolated-runtime' / 'python' / 'desktop_runtime_setup.py'
    invalid_env_root = tmp_path / 'not-a-runtime-root'
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(invalid_env_root))
    monkeypatch.setattr(desktop_runtime_setup, '__file__', str(script_path))

    resolved = desktop_runtime_setup._resolve_runtime_root()
    captured = capsys.readouterr()

    assert 'TOKEN_PLACE_PYTHON_IMPORT_ROOT was set but does not look like a runtime root' in captured.err
    assert resolved == script_path.resolve().parents[3]


def test_runtime_root_ignores_existing_but_invalid_env_path_and_falls_back_to_marker_ancestor(
    monkeypatch, tmp_path, capsys
):
    bad_env_root = tmp_path / 'existing-but-invalid'
    bad_env_root.mkdir(parents=True)
    discovered_root = tmp_path / 'token-place-like'
    (discovered_root / 'utils').mkdir(parents=True)
    script_path = discovered_root / 'desktop-tauri' / 'src-tauri' / 'python' / 'desktop_runtime_setup.py'
    script_path.parent.mkdir(parents=True)
    script_path.write_text('# fake script path for discovery\n', encoding='utf-8')
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(bad_env_root))
    monkeypatch.setattr(desktop_runtime_setup, '__file__', str(script_path))

    resolved = desktop_runtime_setup._resolve_runtime_root()
    captured = capsys.readouterr()

    assert 'TOKEN_PLACE_PYTHON_IMPORT_ROOT was set but does not look like a runtime root' in captured.err
    assert resolved == discovered_root.resolve()


def test_runtime_root_fallback_does_not_raise_for_shallow_script_path(monkeypatch):
    monkeypatch.delenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', raising=False)
    monkeypatch.setattr(desktop_runtime_setup, '__file__', '/desktop_runtime_setup.py')
    resolved = desktop_runtime_setup._resolve_runtime_root()
    assert resolved == Path('/').resolve()


def test_probe_runtime_reraises_internal_type_error(monkeypatch):
    def bad_probe(*, runtime_root=None):
        raise TypeError('internal type mismatch')

    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', bad_probe)
    with pytest.raises(TypeError, match='internal type mismatch'):
        desktop_runtime_setup._probe_runtime(Path.cwd())


def test_ensure_runtime_uses_custom_repo_root_for_initial_probe_and_post_repair_reprobe(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.delenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (True, 'ok')
    )
    custom_root = tmp_path / 'custom-runtime-root'
    custom_root.mkdir(parents=True)
    probe_calls = []
    probes = iter([_probe(), _probe(backend='cuda', gpu=True, device='cuda')])

    def _probe_runtime(runtime_root):
        probe_calls.append(Path(runtime_root))
        return next(probes)

    monkeypatch.setattr(desktop_runtime_setup, '_probe_runtime', _probe_runtime)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=custom_root)

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert probe_calls == [custom_root.resolve(), custom_root.resolve()]


def test_probe_uses_resolved_runtime_root_for_subprocess_cwd_and_pythonpath(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'bundle_root'
    (runtime_root / 'utils').mkdir(parents=True)
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(runtime_root))
    captured = {}

    class _Result:
        returncode = 0
        stdout = json.dumps(
            {
                'backend': 'cuda',
                'gpu_offload_supported': True,
                'detected_device': 'cuda',
                'interpreter': 'python',
                'prefix': 'prefix',
                'llama_module_path': 'site-packages/llama_cpp/__init__.py',
            }
        )
        stderr = ''

    _install_fake_probe_popen(monkeypatch, _Result(), captured)

    probe = desktop_runtime_setup._probe_llama_runtime()

    assert probe.backend == 'cuda'
    assert Path(captured['cwd']) == runtime_root.resolve()
    pythonpath_entries = captured['env']['PYTHONPATH'].split(os.pathsep)
    assert str(runtime_root.resolve()) in pythonpath_entries


def test_windows_runtime_bootstrap_surfaces_source_repair_detail_when_probe_stays_cpu(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    captured = {}

    def fake_record(reason):
        captured['reason'] = reason

    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', fake_record)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_windows_cuda_source_repair',
        lambda _requirements_path: (True, 'line one\nfinal pip status (metadata warning)'),
    )
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert 'source repair detail: final pip status (metadata warning)' in result['fallback_reason']
    assert 'source repair detail: final pip status (metadata warning)' in captured['reason']


def test_windows_cuda_source_repair_fails_closed_without_dependency_target(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (None, 'runtime_root not writable; home_fallback not writable'),
    )
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    invoked = {'source_repair': False, 'pip': False}

    def _source_repair(_requirements_path, _dependency_target=None):
        invoked['source_repair'] = True
        return True, 'unexpected source repair call'

    def _pip_install(*_args, **_kwargs):
        invoked['pip'] = True
        return True, 'unexpected pip install call'

    monkeypatch.setattr(desktop_runtime_setup, '_windows_cuda_source_repair', _source_repair)
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _pip_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path)

    assert result['runtime_action'] == 'failed'
    assert 'desktop dependency target unavailable' in result['fallback_reason']
    assert 'runtime_root not writable; home_fallback not writable' in result['fallback_reason']
    assert invoked == {'source_repair': False, 'pip': False}


def test_runtime_bootstrap_noop_when_gpu_runtime_is_already_present(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='cuda', gpu=True, device='nvidia'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu')

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'


def test_runtime_bootstrap_falls_back_to_cpu_when_repair_fails(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (False, 'compile failed')
    )
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cpu',
            package_spec='llama-cpp-python',
            cmake_args=None,
            force_cmake=False,
            index_url='https://pypi.org/simple',
            only_binary=True,
            no_binary=False,
        )
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'installed_cpu_fallback'
    assert result['selected_backend'] == 'cpu'


def test_maybe_reexec_for_runtime_refresh_reexecs_once(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    called = {}

    def fake_execve(prog, argv, env):
        called['prog'] = prog
        called['argv'] = argv
        called['env'] = dict(env)
        called['guard'] = env.get(desktop_runtime_setup.REEXEC_GUARD_ENV)

    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', fake_execve)
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)
    monkeypatch.setenv('TOKENPLACE_COMPUTE_NODE_SESSION_ID', 'session-123')
    monkeypatch.setenv('TOKENPLACE_OPERATOR_EVENT_SEQUENCE', '41')
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', '/tmp/token-place-managed-site')

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})

    assert called['prog'] == sys.executable
    assert called['guard'] == '1'
    assert called['env']['TOKENPLACE_COMPUTE_NODE_SESSION_ID'] == 'session-123'
    assert called['env']['TOKENPLACE_OPERATOR_EVENT_SEQUENCE'] == '41'
    assert called['env']['TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET'] == '/tmp/token-place-managed-site'


def test_windows_runtime_bootstrap_respects_opt_out_env(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    invoked = {'source_repair': False}
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_windows_cuda_source_repair',
        lambda _requirements_path: (invoked.update(source_repair=True), '') and (False, 'unexpected call'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert result['runtime_action'] == 'probe_only'
    assert desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV in result['fallback_reason']
    assert invoked['source_repair'] is False


def test_windows_runtime_bootstrap_defaults_to_probe_only_without_opt_in(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: _probe())
    monkeypatch.delenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    invoked = {'source_repair': False}
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_windows_cuda_source_repair',
        lambda _requirements_path: (invoked.update(source_repair=True), '') and (False, 'unexpected call'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert result['runtime_action'] == 'probe_only'
    assert desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV in result['fallback_reason']
    assert invoked['source_repair'] is False



def test_install_outcome_timeout_action_is_fatal_for_windows_qwen64k(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    runtime_action = desktop_runtime_setup._install_outcome_action('outcome=timed_out; stderr_tail=empty', 'cuda')
    message = desktop_runtime_setup.desktop_gpu_runtime_failure_message(
        'gpu',
        {
            'selected_backend': 'cpu',
            'runtime_action': runtime_action,
            'fallback_reason': 'llama-cpp-python install timed out',
        },
    )

    assert runtime_action == 'install_timeout'
    assert runtime_action in desktop_runtime_setup.GPU_RUNTIME_FATAL_ACTIONS
    assert message is not None
    assert 'action=install_timeout' in message


def test_install_outcome_heartbeat_failure_action_is_fatal_for_windows_qwen64k(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    runtime_action = desktop_runtime_setup._install_outcome_action('outcome=heartbeat_failed; stderr_tail=empty', 'cuda')

    assert runtime_action == 'install_heartbeat_failed'
    assert runtime_action in desktop_runtime_setup.GPU_RUNTIME_FATAL_ACTIONS


def test_cuda_source_repair_lock_timeout_returns_structured_failure(monkeypatch, tmp_path):
    target = tmp_path / 'site'
    target.mkdir()
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda repo_root=None: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_dependency_target', lambda _root: (target, None))
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_runtime', lambda _root: _probe(error='missing', version='unknown'))

    class TimeoutLock:
        def __init__(self, *_args, **_kwargs):
            pass
        def __enter__(self):
            raise TimeoutError('managed-site lock wait timed out')
        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(desktop_runtime_setup, '_ManagedSiteMutationLock', TimeoutLock)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_windows_cuda_source_repair',
        lambda *_args, **_kwargs: pytest.fail('source repair must not run without the lock'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path, context_tier='64k-full')

    assert result['runtime_action'] == 'install_timeout'
    assert result['runtime_action'] in desktop_runtime_setup.GPU_RUNTIME_FATAL_ACTIONS
    assert 'timed out' in result['fallback_reason']


def test_runtime_plan_lock_cancellation_returns_structured_failure(monkeypatch, tmp_path):
    target = tmp_path / 'site'
    target.mkdir()
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda repo_root=None: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_dependency_target', lambda _root: (target, None))
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (False, 'cooldown'))
    monkeypatch.setattr(desktop_runtime_setup, '_probe_runtime', lambda _root: _probe(error='missing', version='unknown'))
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cuda',
            package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_CUDA=on',
            force_cmake=True,
            index_url=None,
            only_binary=False,
            no_binary=False,
        )
    ])

    class CancelledLock:
        def __init__(self, *_args, **_kwargs):
            pass
        def __enter__(self):
            raise TimeoutError('managed-site lock wait cancelled')
        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(desktop_runtime_setup, '_ManagedSiteMutationLock', CancelledLock)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: pytest.fail('pip install must not run without the lock'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path, context_tier='64k-full')

    assert result['runtime_action'] == 'install_cancelled'
    assert result['runtime_action'] in desktop_runtime_setup.GPU_RUNTIME_FATAL_ACTIONS
    assert 'cancelled' in result['fallback_reason']


def test_desktop_gpu_runtime_failure_message_ignores_probe_only(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    message = desktop_runtime_setup.desktop_gpu_runtime_failure_message(
        'auto',
        {
            'selected_backend': 'cpu',
            'runtime_action': 'probe_only',
            'fallback_reason': 'runtime bootstrap not enabled',
        },
    )

    assert message is None


def test_desktop_gpu_runtime_failure_message_flags_shadowed_repo_runtime(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    message = desktop_runtime_setup.desktop_gpu_runtime_failure_message(
        'auto',
        {
            'selected_backend': 'cpu',
            'runtime_action': 'shadowed_repo_llama_cpp',
            'fallback_reason': 'llama_cpp import shadowed by repo-local shim',
        },
    )

    assert message is not None
    assert 'action=shadowed_repo_llama_cpp' in message


def test_desktop_gpu_runtime_failure_message_reports_unsupported_windows_abi(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('win32'))
    monkeypatch.setattr(desktop_runtime_setup.platform_module, 'machine', lambda: 'ARM64')

    message = desktop_runtime_setup.desktop_gpu_runtime_failure_message(
        'gpu',
        {
            'selected_backend': 'cpu',
            'runtime_action': 'failed',
            'fallback_reason': 'No module named llama_cpp',
        },
    )

    assert message is not None
    assert 'bootstrap is not supported for platform=win32 arch=arm64' in message
    assert 'Verify CUDA runtime prerequisites' not in message

def test_windows_runtime_bootstrap_success_reexec_is_guarded_to_one_attempt(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    probes = iter([_probe(), _probe(backend='cuda', gpu=True, device='cuda')])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (True, 'ok')
    )
    exec_calls = {'count': 0}
    monkeypatch.setattr(
        desktop_runtime_setup.os,
        'execve',
        lambda *_args: exec_calls.update(count=exec_calls['count'] + 1),
    )
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)

    runtime_setup = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    desktop_runtime_setup.maybe_reexec_for_runtime_refresh(runtime_setup)
    monkeypatch.setenv(desktop_runtime_setup.REEXEC_GUARD_ENV, '1')
    desktop_runtime_setup.maybe_reexec_for_runtime_refresh(runtime_setup)

    assert runtime_setup['runtime_action'] == 'installed_cuda_reexec'
    assert exec_calls['count'] == 1


def test_fallback_unpinned_plans_cover_win_darwin_and_other_platforms():
    win_plans = desktop_runtime_setup._fallback_unpinned_plans('win32')
    darwin_plans = desktop_runtime_setup._fallback_unpinned_plans('darwin')
    linux_plans = desktop_runtime_setup._fallback_unpinned_plans('linux')

    assert [plan.backend for plan in win_plans] == ['cuda', 'cpu']
    assert [plan.backend for plan in darwin_plans] == ['metal', 'metal', 'cpu']
    assert [plan.backend for plan in linux_plans] == ['cpu']
    assert win_plans[1].extra_index_url == desktop_runtime_setup.LLAMA_CPP_CPU_WHEEL_INDEX_URL
    assert darwin_plans[0].extra_index_url == desktop_runtime_setup.LLAMA_CPP_METAL_WHEEL_INDEX_URL
    assert darwin_plans[0].only_binary is True
    assert darwin_plans[1].no_binary is True
    assert darwin_plans[2].extra_index_url == desktop_runtime_setup.LLAMA_CPP_CPU_WHEEL_INDEX_URL
    assert darwin_plans[2].only_binary is True


def test_windows_source_repair_uses_dependency_target(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama_cpp_python==0.3.32\n', encoding='utf-8')
    dependency_target = tmp_path / 'desktop deps'
    captured = {}

    def fake_run(cmd, env, timeout_seconds, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        captured['startup_phase'] = kwargs.get('startup_phase')
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)

    ok, _ = desktop_runtime_setup._windows_cuda_source_repair(requirements_path, dependency_target)

    assert ok is True
    assert captured['startup_phase'] == 'cuda_build'
    assert '--target' in captured['cmd']
    assert captured['cmd'][captured['cmd'].index('--target') + 1] == str(dependency_target)
    assert captured['env']['PYTHONPATH'].split(os.pathsep)[0] == str(dependency_target)
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_CUDA=on'
    assert captured['env']['FORCE_CMAKE'] == '1'


def test_windows_source_repair_uses_active_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama_cpp_python==0.3.32\n', encoding='utf-8')
    captured = {}

    def fake_run(cmd, env, timeout_seconds, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        captured['startup_phase'] = kwargs.get('startup_phase')
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)
    ok, _ = desktop_runtime_setup._windows_cuda_source_repair(requirements_path)

    assert ok is True
    assert captured['cmd'][:3] == [sys.executable, '-m', 'pip']
    assert captured['cmd'][3] == 'install'
    assert '--disable-pip-version-check' in captured['cmd']
    assert '--force-reinstall' in captured['cmd']
    assert '--no-cache-dir' in captured['cmd']
    assert '--target' not in captured['cmd']
    assert captured['cmd'][-4:-1] == ['--no-binary', 'llama-cpp-python', '--verbose']
    assert captured['cmd'][-1].startswith('llama-cpp-python==')
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_CUDA=on'
    assert captured['env']['FORCE_CMAKE'] == '1'
    assert captured['timeout_seconds'] == desktop_runtime_setup.PIP_SOURCE_BUILD_TIMEOUT_SECONDS


def test_windows_source_repair_returns_actionable_message_when_requirements_missing(monkeypatch, tmp_path):
    missing_requirements = tmp_path / 'AppData' / 'requirements.txt'
    captured = {}

    def fake_run(cmd, env, timeout_seconds, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        captured['startup_phase'] = kwargs.get('startup_phase')
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)
    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(missing_requirements)

    assert ok is True
    assert 'requirements file not found' in reason
    assert 'falling back to unpinned llama-cpp-python source reinstall' in reason
    assert str(missing_requirements) in reason
    assert captured['cmd'][-1] == 'llama-cpp-python'


def test_windows_source_repair_returns_actionable_message_when_requirement_is_unreadable(monkeypatch, tmp_path):
    unreadable_requirements = tmp_path / 'AppData' / 'requirements.txt'

    def _raise_unreadable(_requirements_path):
        raise OSError('permission denied')

    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_requirement_spec', _raise_unreadable)

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))
    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(unreadable_requirements)

    assert ok is True
    assert 'unable to resolve pinned llama-cpp-python requirement' in reason
    assert 'falling back to unpinned source reinstall' in reason
    assert str(unreadable_requirements) in reason
    assert 'permission denied' in reason


def test_windows_source_repair_preserves_metadata_warning_in_last_line(monkeypatch, tmp_path):
    missing_requirements = tmp_path / 'missing-requirements.txt'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (False, 'line one\nfinal pip error line'),
    )

    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(missing_requirements)

    assert ok is False
    assert 'requirements file not found at' in reason.splitlines()[-1]
    assert 'falling back to unpinned llama-cpp-python source reinstall' in reason.splitlines()[-1]


def test_windows_source_repair_returns_actionable_message_when_requirement_is_invalid(monkeypatch, tmp_path):
    invalid_requirements = tmp_path / 'AppData' / 'requirements.txt'

    def _raise_invalid(_requirements_path):
        raise ValueError('missing pinned llama-cpp-python requirement')

    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_requirement_spec', _raise_invalid)

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))
    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(invalid_requirements)

    assert ok is True
    assert 'unable to resolve pinned llama-cpp-python requirement' in reason
    assert 'falling back to unpinned source reinstall' in reason
    assert str(invalid_requirements) in reason
    assert 'missing pinned llama-cpp-python requirement' in reason


def test_install_error_summary_prefers_stderr_tail_when_command_is_long():
    long_command = 'command=' + ('/very/long/path/' * 80)
    stdout = 'stdout_tail=' + ('download progress ' * 60)
    stderr = 'stderr_tail=' + ('cmake configure noise ' * 40) + 'fatal cmake error: Metal headers missing'

    summary = desktop_runtime_setup._summarize_install_error(f'{long_command}; returncode=1; {stdout}; {stderr}')

    assert summary.startswith('stderr_tail=')
    assert 'fatal cmake error: Metal headers missing' in summary
    assert len(summary) <= desktop_runtime_setup.INSTALL_ERROR_SUMMARY_MAX_LEN + len('stderr_tail=')


def test_probe_leaves_dependency_target_env_unset_when_target_is_unresolved(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'bundle_root'
    (runtime_root / 'utils').mkdir(parents=True)
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', 'unknown')
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (None, 'not writable'),
    )
    captured = {}

    class _Result:
        returncode = 0
        stdout = json.dumps(
            {
                'backend': 'missing',
                'gpu_offload_supported': False,
                'detected_device': 'none',
                'interpreter': 'python',
                'prefix': 'prefix',
                'llama_module_path': 'missing',
                'dependency_target': 'unknown',
            }
        )
        stderr = ''

    _install_fake_probe_popen(monkeypatch, _Result(), captured)

    probe = desktop_runtime_setup._probe_llama_runtime()

    assert 'TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET' not in captured['env']
    assert './unknown' not in captured['env']['PYTHONPATH']
    assert probe.dependency_target == 'unknown'


def test_probe_marks_error_when_subprocess_has_empty_stdout(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 1
        stdout = ''
        stderr = 'probe failed'

    _install_fake_probe_popen(monkeypatch, _Result())

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'probe failed'


def test_maybe_reexec_for_runtime_refresh_skips_when_guard_set(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    called = {'execve': False}
    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', lambda *_args: called.update(execve=True))
    monkeypatch.setenv(desktop_runtime_setup.REEXEC_GUARD_ENV, '1')

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})

    assert called['execve'] is False


def test_maybe_reexec_for_runtime_refresh_handles_execve_oserror(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    def _raise(*_args):
        raise OSError('denied')

    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', _raise)
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})


def test_source_repair_cooldown_skips_immediate_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    state_path = tmp_path / 'runtime_state.json'
    monkeypatch.setattr(desktop_runtime_setup, '_runtime_state_path', lambda: state_path)
    now = 1_000.0
    monkeypatch.setattr(desktop_runtime_setup.time, 'time', lambda: now)
    state_path.write_text(
        json.dumps(
            {
                'source_repair_failures': {
                    sys.executable: {'last_failed_at': now - 30, 'reason': 'build failed'}
                }
            }
        ),
        encoding='utf-8',
    )
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert result['fallback_reason'] == 'build failed'


def test_probe_marks_error_when_subprocess_raises(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    _install_fake_probe_popen(monkeypatch, RuntimeError('subprocess unavailable'))

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'desktop_runtime_probe_start_failed:RuntimeError'


def test_probe_uses_return_code_when_stderr_is_empty(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 9
        stdout = ''
        stderr = ''

    _install_fake_probe_popen(monkeypatch, _Result())

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'probe subprocess failed with return code 9'


def test_probe_subprocess_sanitizes_repo_root_before_llama_import(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    captured = {}

    class _Result:
        returncode = 0
        stdout = json.dumps(
            {
                'backend': 'cuda',
                'gpu_offload_supported': True,
                'detected_device': 'cuda',
                'interpreter': sys.executable,
                'prefix': sys.prefix,
                'llama_module_path': 'C:/Python/Lib/site-packages/llama_cpp/__init__.py',
            }
        )
        stderr = ''

    _install_fake_probe_popen(monkeypatch, _Result(), captured)
    probe = desktop_runtime_setup._probe_llama_runtime()

    assert probe.backend == 'cuda'
    assert 'ensure_runtime_import_paths' in desktop_runtime_setup._PROBE_SNIPPET
    assert "_safe_resolve_path_text(entry or \".\")" in desktop_runtime_setup._PROBE_SNIPPET
    assert 'utils.llm.model_manager' not in desktop_runtime_setup._PROBE_SNIPPET
    assert 'importlib.import_module("llama_cpp")' in desktop_runtime_setup._PROBE_SNIPPET
    assert captured['cmd'][:2] == [sys.executable, '-c']
    pythonpath_entries = captured['env']['PYTHONPATH'].split(desktop_runtime_setup.os.pathsep)
    assert pythonpath_entries[0] == str(PYTHON_MODULE_DIR)
    assert pythonpath_entries[1].endswith('.token_place_desktop_site')
    assert str(Path(__file__).resolve().parents[2]) in pythonpath_entries
    assert captured['env']['TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT'].endswith(
        'desktop_runtime_setup.py'
    )


def test_probe_subprocess_keeps_stdlib_ahead_of_polluted_dependency_target(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'resources'
    (runtime_root / 'utils').mkdir(parents=True)
    dependency_target = runtime_root / '.token_place_desktop_site'
    llama_package = dependency_target / 'llama_cpp'
    llama_package.mkdir(parents=True)
    llama_package.joinpath('__init__.py').write_text(
        '\n'.join(
            [
                '__version__ = "0.3.0"',
                'GGML_METAL = True',
                'LLAMA_ROPE_SCALING_TYPE_YARN = 2',
                'def llama_supports_gpu_offload():',
                '    return True',
                'class Llama:',
                '    def __init__(self, rope_scaling_type=None, rope_freq_scale=None, yarn_ext_factor=None, yarn_orig_ctx=None):',
                '        pass',
            ]
        ),
        encoding='utf-8',
    )
    dependency_target.joinpath('pathlib.py').write_text(
        'from collections import Sequence\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(runtime_root))

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=runtime_root)

    assert probe.error is None
    assert probe.backend == 'metal'
    assert probe.gpu_offload_supported is True
    assert probe.yarn_rope_supported is True
    assert probe.llama_cpp_python_version == '0.3.0'


def test_probe_falls_back_when_payload_is_not_json(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 0
        stdout = 'not-json'
        stderr = 'json parse failed'

    _install_fake_probe_popen(monkeypatch, _Result())

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'json parse failed'


def test_is_repo_local_llama_module_returns_false_for_empty_module_path():
    assert desktop_runtime_setup._is_repo_local_llama_module('', Path.cwd()) is False


def test_is_repo_local_llama_module_returns_false_on_resolve_oserror(monkeypatch):
    class _BrokenPath:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            raise OSError('resolve failed')

    monkeypatch.setattr(desktop_runtime_setup, 'Path', _BrokenPath)
    assert desktop_runtime_setup._is_repo_local_llama_module('C:/llama_cpp.py', Path.cwd()) is False


def test_runtime_bootstrap_fails_fast_when_repo_local_llama_shim_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir(parents=True)
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda: desktop_runtime_setup.RuntimeProbe(
            backend='cpu',
            gpu_offload_supported=False,
            detected_device='cpu',
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path=str((repo_root / 'llama_cpp.py').resolve()),
            error=None,
        ),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=repo_root)

    assert result['runtime_action'] == 'shadowed_repo_llama_cpp'
    assert 'repo-local shim' in result['fallback_reason']


def test_run_pip_install_success_failure_and_timeout():
    ok, output = desktop_runtime_setup._run_pip_install(
        [sys.executable, "-c", "print('ok output')"],
        os.environ.copy(),
    )
    assert ok is True
    assert 'returncode=0' in output
    assert 'outcome=completed' in output
    assert 'stdout_tail=ok output' in output

    ok, output = desktop_runtime_setup._run_pip_install(
        [sys.executable, "-c", "import sys; print('fallback stdout'); print('real stderr', file=sys.stderr); sys.exit(1)"],
        os.environ.copy(),
    )
    assert ok is False
    assert 'returncode=1' in output
    assert 'outcome=completed' in output
    assert 'stdout_tail=fallback stdout' in output
    assert 'stderr_tail=real stderr' in output

    ok, output = desktop_runtime_setup._run_pip_install(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        os.environ.copy(),
        timeout_seconds=1,
    )
    assert ok is False
    assert 'outcome=timed_out' in output
    assert 'stdout_tail=empty' in output
    assert 'stderr_tail=empty' in output


def test_command_summary_redacts_interpreter_targets_requirements_and_paths(tmp_path):
    target = tmp_path / "managed-site"
    requirements = tmp_path / "requirements.txt"

    summary = desktop_runtime_setup._command_summary([
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(target),
        "-r",
        str(requirements),
        "llama-cpp-python==0.3.32",
    ])

    assert summary.startswith("<python> -m pip install")
    assert "--target <path>" in summary
    assert "-r <path>" in summary
    assert str(target) not in summary
    assert str(requirements) not in summary
    assert "llama-cpp-python==0.3.32" in summary


def test_run_pip_install_cancellation_reports_outcome_and_redacted_command():
    calls = {"count": 0}

    def cancel_after_start():
        calls["count"] += 1
        return calls["count"] >= 2

    ok, output = desktop_runtime_setup._run_pip_install(
        [sys.executable, "-c", "import time; print('started'); time.sleep(30)"],
        os.environ.copy(),
        timeout_seconds=30,
        cancellation_predicate=cancel_after_start,
    )

    assert ok is False
    assert "outcome=cancelled" in output
    assert "command=<python> -c" in output
    assert sys.executable not in output


def test_run_pip_install_emits_five_second_heartbeat(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.StringIO("quick\n")
            self.stderr = io.StringIO("")
            self.pid = 12345
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None if self._polls == 1 else 0

        def wait(self, timeout=None):
            return 0

    timeline = iter([100.0, 106.0])
    heartbeats = []
    monkeypatch.setattr(desktop_runtime_setup.time, "monotonic", lambda: next(timeline))
    monkeypatch.setattr(desktop_runtime_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())

    ok, output = desktop_runtime_setup._run_pip_install(
        [sys.executable, "-c", "print('quick')"],
        os.environ.copy(),
        heartbeat=heartbeats.append,
    )

    assert ok is True
    assert "outcome=completed" in output
    assert "stdout_tail=quick" in output
    assert heartbeats == [{"startup_elapsed_ms": 6000, "startup_deadline_ms": 300000, "startup_phase": "runtime_install"}]


def test_runtime_state_tracks_and_clears_source_repair_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    state_path = tmp_path / 'runtime_state.json'
    monkeypatch.setattr(desktop_runtime_setup, '_runtime_state_path', lambda: state_path)
    now = 2_000.0
    monkeypatch.setattr(desktop_runtime_setup.time, 'time', lambda: now)

    desktop_runtime_setup._record_source_repair_failure('build failed badly')
    can_retry, reason = desktop_runtime_setup._should_attempt_source_repair()
    assert can_retry is False
    assert reason == 'build failed badly'

    desktop_runtime_setup._clear_source_repair_failure()
    monkeypatch.setenv(desktop_runtime_setup.DEVELOPMENT_SOURCE_BUILD_OPT_IN_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_development_source_build_allowed', lambda: True)
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert sys.executable not in state.get('source_repair_failures', {})
    monkeypatch.setattr(
        desktop_runtime_setup.time,
        'time',
        lambda: now + desktop_runtime_setup.SOURCE_REPAIR_COOLDOWN_SECONDS + 1,
    )
    can_retry, reason = desktop_runtime_setup._should_attempt_source_repair()
    assert can_retry is True
    assert reason == ''


def test_windows_packaged_layout_without_requirements_falls_back_without_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (False, 'simulated pip source build failure'),
    )
    monkeypatch.setattr(desktop_runtime_setup, '_fallback_unpinned_plans', lambda _platform: [])
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])

    packaged_root = tmp_path / 'Users' / 'danie' / 'AppData' / 'Local' / 'token.place'
    packaged_root.mkdir(parents=True)
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=packaged_root)

    assert result['runtime_action'] == 'failed'
    assert result['selected_backend'] == 'cpu'
    assert '[Errno 2]' not in result['fallback_reason']
    assert 'requirements file not found' in result['fallback_reason']
    assert 'falling back to unpinned llama-cpp-python source reinstall' in result['fallback_reason']


def test_resolve_requirements_path_prefers_packaged_resources_when_repo_root_missing(tmp_path):
    target_root = tmp_path / 'token-place-installed'
    packaged_requirements = target_root / 'resources' / 'requirements.txt'
    packaged_requirements.parent.mkdir(parents=True)
    packaged_requirements.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')

    resolved = desktop_runtime_setup._resolve_requirements_path(target_root)

    assert resolved == packaged_requirements


def test_windows_runtime_bootstrap_passes_resolved_packaged_requirements_before_unpinned_fallback(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (False, 'cooldown'))
    monkeypatch.setattr(desktop_runtime_setup, '_fallback_unpinned_plans', lambda _platform: [])
    captured = {}

    def _capture_plans(*, platform, requirements_path):
        captured['platform'] = platform
        captured['requirements_path'] = requirements_path
        return []

    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', _capture_plans)
    packaged_root = tmp_path / 'AppData' / 'Local' / 'token.place'
    packaged_requirements = packaged_root / 'resources' / 'requirements.txt'
    packaged_requirements.parent.mkdir(parents=True)
    packaged_requirements.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=packaged_root)

    assert result['runtime_action'] == 'failed'
    assert captured['platform'] == 'win32'
    assert captured['requirements_path'] == packaged_requirements


def test_windows_wheel_install_path_force_reinstalls_existing_same_version(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (False, 'cooldown'))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cuda',
            package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_CUDA=on',
            force_cmake=True,
            index_url='https://pypi.org/simple',
            only_binary=False,
            no_binary=True,
        )
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    captured = {}

    def _capture_run(cmd, env, timeout_seconds=desktop_runtime_setup.PIP_INSTALL_TIMEOUT_SECONDS):
        captured['cmd'] = cmd
        return False, 'failed'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert 'cooldown' in result['fallback_reason']
    assert captured == {}


def test_is_repo_local_llama_module_uses_case_insensitive_comparison(tmp_path):
    repo_root = tmp_path / 'RepoRoot'
    repo_root.mkdir(parents=True)
    shim = repo_root / 'llama_cpp.py'
    shim.write_text('# shim\n', encoding='utf-8')

    module_path = str(shim.resolve()).upper()
    assert desktop_runtime_setup._is_repo_local_llama_module(module_path, repo_root) is True


def test_ensure_desktop_python_dependencies_reports_requirements_missing(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'runtime'
    (runtime_root / 'utils').mkdir(parents=True)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda **_: runtime_root)
    monkeypatch.setattr(desktop_runtime_setup.importlib.util, 'find_spec', lambda _name: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_requirements_path',
        lambda _root: runtime_root / 'python' / 'requirements_desktop_runtime.txt',
    )

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=runtime_root)

    assert result['ok'] == 'false'
    assert result['action'] == 'requirements_missing'
    assert result['missing'] == 'psutil,requests,dotenv,cryptography'


def test_resolve_desktop_requirements_path_prefers_macos_resources_layout(tmp_path):
    runtime_root = tmp_path / 'TokenPlace.app' / 'Contents' / 'Resources'
    requirements = runtime_root / 'python' / 'requirements_desktop_runtime.txt'
    requirements.parent.mkdir(parents=True)
    requirements.write_text('cryptography==46.0.1\n', encoding='utf-8')

    resolved = desktop_runtime_setup._resolve_desktop_requirements_path(runtime_root)

    assert resolved == requirements


def test_ensure_desktop_python_dependencies_reports_install_failed(monkeypatch, tmp_path):
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('psutil\nrequests\npython-dotenv\ncryptography\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda **_: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup.importlib.util, 'find_spec', lambda _name: None)
    captured = {}

    def _capture_run(cmd, *_args, **_kwargs):
        captured['cmd'] = cmd
        captured['startup_phase'] = _kwargs.get('startup_phase')
        return False, 'install failed: boom'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_run)

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=tmp_path)

    assert result['ok'] == 'false'
    assert result['action'] == 'install_failed'
    assert result['detail'] == 'install failed: boom'
    assert captured['startup_phase'] == 'dependency_install'
    assert '--target' in captured['cmd']
    target_idx = captured['cmd'].index('--target') + 1
    selected_target = str(tmp_path / '.token_place_desktop_site')
    assert captured['cmd'][target_idx] == selected_target
    assert os.environ['TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET'] == selected_target
    assert selected_target in sys.path


def test_ensure_desktop_python_dependencies_lock_oserror_returns_sanitized_failure(monkeypatch, tmp_path):
    sentinel = str(tmp_path / "secret" / "managed.lock")
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('psutil\nrequests\npython-dotenv\ncryptography\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda **_: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup.importlib.util, 'find_spec', lambda _name: None)

    class OSErrorLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            raise OSError(f'lock busy at {sentinel}')

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(desktop_runtime_setup, '_ManagedSiteMutationLock', OSErrorLock)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('pip must not run without lock')),
    )

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=tmp_path)

    assert result['ok'] == 'false'
    assert result['action'] == 'lock_unavailable'
    assert result['detail'] == 'managed site mutation lock unavailable'
    assert sentinel not in json.dumps(result)


def test_ensure_desktop_python_dependencies_reports_post_install_missing(monkeypatch, tmp_path):
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('psutil\nrequests\npython-dotenv\ncryptography\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda **_: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    sequence = iter([None, None, None, None, object(), object(), object(), None, object(), object(), object(), None])
    monkeypatch.setattr(desktop_runtime_setup.importlib.util, 'find_spec', lambda _name: next(sequence))
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=tmp_path)

    assert result['ok'] == 'false'
    assert result['action'] == 'post_install_missing'
    assert result['missing'] == 'cryptography'


def test_ensure_desktop_python_dependencies_falls_back_to_home_target_when_runtime_root_unwritable(
    monkeypatch, tmp_path
):
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('psutil\nrequests\npython-dotenv\ncryptography\n', encoding='utf-8')
    runtime_root = tmp_path / 'runtime'
    runtime_root.mkdir(parents=True)
    home_dir = tmp_path / 'home'
    home_dir.mkdir(parents=True)

    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda **_: runtime_root)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup.importlib.util, 'find_spec', lambda _name: None)
    monkeypatch.setattr(desktop_runtime_setup.Path, 'home', staticmethod(lambda: home_dir))
    captured = {}

    def _capture_run(cmd, *_args, **_kwargs):
        captured['cmd'] = cmd
        captured['startup_phase'] = _kwargs.get('startup_phase')
        return False, 'install failed: boom'

    original_mkdir = desktop_runtime_setup.Path.mkdir

    def _fake_mkdir(path_obj, parents=False, exist_ok=False):
        if str(path_obj).endswith('.token_place_desktop_site') and runtime_root in path_obj.parents:
            raise PermissionError('read-only bundle')
        return original_mkdir(path_obj, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(desktop_runtime_setup.Path, 'mkdir', _fake_mkdir)
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_run)

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=runtime_root)

    assert result['action'] == 'install_failed'
    assert captured['startup_phase'] == 'dependency_install'
    assert '--target' in captured['cmd']
    target_idx = captured['cmd'].index('--target') + 1
    assert captured['cmd'][target_idx] == str(home_dir / '.token_place_desktop_site')


def _load_parity_matrix():
    matrix_path = (
        REPO_ROOT / "tests" / "fixtures" / "desktop_operator_parity_matrix.json"
    )
    return json.loads(matrix_path.read_text(encoding="utf-8"))


class _PlatformStub:
    executable = sys.executable
    prefix = sys.prefix
    argv = [str(MODULE_PATH)]

    def __init__(self, platform):
        self.platform = platform


def _probe_from_matrix_payload(probe):
    return _probe(
        backend=probe["backend"],
        gpu=probe["gpu"],
        device=probe["device"],
        error=probe.get("error"),
    )


def _probe_from_matrix_case(case):
    return _probe_from_matrix_payload(case["probe"])

@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case["id"])
        for case in _load_parity_matrix()["platform_cases"]
        if "expect" in case
    ],
)
def test_desktop_operator_parity_platform_matrix(monkeypatch, case):
    monkeypatch.setattr(desktop_runtime_setup, "sys", _PlatformStub(case["platform"]))
    matrix_arch = "arm64" if case["platform"] == "darwin" else "x86_64"
    monkeypatch.setattr(desktop_runtime_setup.platform_module, "machine", lambda: matrix_arch)
    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.delenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, raising=False)
    for env_name, env_value in case.get("env", {}).items():
        monkeypatch.setenv(env_name, env_value)
    probe_payloads = [case["probe"]]
    if "after_probe" in case:
        probe_payloads.append(case["after_probe"])
    probes = iter(_probe_from_matrix_payload(probe) for probe in probe_payloads)
    monkeypatch.setattr(
        desktop_runtime_setup,
        "_probe_llama_runtime",
        lambda **_: next(probes),
    )
    invoked = {"pip": False, "source_repair": False}
    monkeypatch.setattr(desktop_runtime_setup, "_should_attempt_source_repair", lambda: (True, ""))
    monkeypatch.setattr(desktop_runtime_setup, "_record_source_repair_failure", lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, "_clear_source_repair_failure", lambda: None)

    def _matrix_source_repair(_requirements_path):
        invoked.update(source_repair=True)
        result = case.get("source_repair_result", {"ok": False, "log": "unexpected"})
        return result["ok"], result["log"]

    monkeypatch.setattr(
        desktop_runtime_setup,
        "_windows_cuda_source_repair",
        _matrix_source_repair,
    )
    def _matrix_pip_install(*_args, **_kwargs):
        invoked.update(pip=True)
        result = case.get("pip_install_result", {"ok": False, "log": "unexpected"})
        return result["ok"], result["log"]

    monkeypatch.setattr(desktop_runtime_setup, "_run_pip_install", _matrix_pip_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        case["mode"], repo_root=REPO_ROOT
    )

    expected = case["expect"]
    assert result["selected_backend"] == expected["selected_backend"]
    assert result["runtime_action"] == expected["runtime_action"]
    if "fallback_reason" in expected:
        assert result.get("fallback_reason", "") == expected["fallback_reason"]
    if "fallback_reason_contains" in expected:
        assert expected["fallback_reason_contains"] in result.get("fallback_reason", "")
    if "last_error_contains" in expected:
        assert expected["last_error_contains"] in result.get("fallback_reason", "")
    if expected.get("registration_eligible_after_warm_load") is False:
        assert expected["runtime_action"] in {"failed", "probe_only", "pending"}
        assert expected.get("relay_runtime_state") in {"failed", "pending"}
    if case["id"] == "missing_runtime_dependency":
        assert expected["backend_available"] == "unavailable"
        assert expected["backend_selected"] == "pending"
        assert expected["backend_used"] == "pending"
        assert result["runtime_action"] == "failed"
    if case["id"] == "cpu_fallback":
        assert expected["registration_eligible_after_warm_load"] is True
        assert expected["backend_available"] == "cpu"
        assert expected["backend_selected"] == "cpu"
        assert expected["backend_used"] == "cpu"
    if case["id"] in {
        "macos_metal_capable",
        "cpu_fallback",
        "missing_runtime_dependency",
    }:
        assert invoked == {"pip": False, "source_repair": False}
    if case["id"] == "windows_missing_runtime_bootstrap_repair":
        assert invoked == {"pip": False, "source_repair": True}
    if case["id"] == "macos_metal_bootstrap_gap":
        assert invoked == {"pip": True, "source_repair": False}


def test_resolve_desktop_dependency_target_prefers_env_override(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'runtime'
    home_dir = tmp_path / 'home'
    override_target = tmp_path / 'external-desktop-site'
    home_dir.mkdir()
    probes = []

    def _fake_writable(candidate):
        probes.append(candidate)
        return True, None

    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', str(override_target))
    monkeypatch.setattr(desktop_runtime_setup.Path, 'home', staticmethod(lambda: home_dir))
    monkeypatch.setattr(desktop_runtime_setup, '_is_writable_directory', _fake_writable)

    target, error = desktop_runtime_setup._resolve_desktop_dependency_target(runtime_root)

    assert error is None
    assert target == override_target
    assert probes == [override_target]


def test_resolve_desktop_dependency_target_prefers_writable_runtime_target_without_env_override(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'runtime'
    home_dir = tmp_path / 'home'
    home_dir.mkdir()
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', raising=False)
    monkeypatch.setattr(desktop_runtime_setup.Path, 'home', staticmethod(lambda: home_dir))

    target, error = desktop_runtime_setup._resolve_desktop_dependency_target(runtime_root)

    assert error is None
    assert target == runtime_root / '.token_place_desktop_site'
    assert target.is_dir()


def test_resolve_desktop_dependency_target_uses_home_only_when_runtime_probe_fails(
    monkeypatch, tmp_path
):
    runtime_root = tmp_path / 'runtime'
    home_dir = tmp_path / 'home'
    home_dir.mkdir()
    probes = []

    def _fake_writable(candidate):
        probes.append(candidate)
        if candidate == runtime_root / '.token_place_desktop_site':
            return False, 'read-only runtime target'
        return True, None

    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', raising=False)
    monkeypatch.setattr(desktop_runtime_setup.Path, 'home', staticmethod(lambda: home_dir))
    monkeypatch.setattr(desktop_runtime_setup, '_is_writable_directory', _fake_writable)

    target, error = desktop_runtime_setup._resolve_desktop_dependency_target(runtime_root)

    assert error is None
    assert target == home_dir / '.token_place_desktop_site'
    assert probes == [runtime_root / '.token_place_desktop_site', target]


def test_runtime_setup_strips_windows_extended_prefix_for_packaged_resource_paths():
    assert desktop_runtime_setup._strip_windows_extended_path_prefix(
        r'\\?\C:\Users\danie\AppData\Local\token.place desktop\python\compute_node_bridge.py'
    ) == r'C:\Users\danie\AppData\Local\token.place desktop\python\compute_node_bridge.py'
    assert desktop_runtime_setup._strip_windows_extended_path_prefix(
        r'\\?\UNC\server\share\TokenPlace.app\Contents\Resources\python\compute_node_bridge.py'
    ) == r'\\server\share\TokenPlace.app\Contents\Resources\python\compute_node_bridge.py'


def test_record_desktop_runtime_probe_clears_env_when_payload_is_not_serializable(monkeypatch):
    monkeypatch.setenv(desktop_runtime_setup.RUNTIME_PROBE_ENV, '{"stale": true}')
    monkeypatch.setattr(
        desktop_runtime_setup.json,
        'dumps',
        lambda _payload: (_ for _ in ()).throw(TypeError('not serializable')),
    )
    result = {'runtime_action': object()}

    assert desktop_runtime_setup._record_desktop_runtime_probe(result) is result
    assert desktop_runtime_setup.RUNTIME_PROBE_ENV not in os.environ


@pytest.mark.parametrize(
    'runtime_action,module_path',
    [
        ('install_failed', 'C:/Python/Lib/site-packages/llama_cpp/__init__.py'),
        ('already_supported', ''),
        ('already_supported', 'missing'),
        ('already_supported', 'unknown'),
    ],
)
def test_record_desktop_runtime_probe_keeps_module_path_identity_only(
    monkeypatch,
    runtime_action,
    module_path,
):
    monkeypatch.delenv(desktop_runtime_setup.RUNTIME_PROBE_ENV, raising=False)
    result = {
        'runtime_action': runtime_action,
        'selected_backend': 'cuda',
        'backend': 'cuda',
        'interpreter': sys.executable,
        'llama_module_path': module_path,
    }

    public = desktop_runtime_setup._record_desktop_runtime_probe(result)
    private = json.loads(os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV])

    assert 'llama_module_path' not in public
    assert 'llama_module_path' not in private


def test_record_desktop_runtime_probe_private_payload_accepts_reexec_success(monkeypatch):
    monkeypatch.delenv(desktop_runtime_setup.RUNTIME_PROBE_ENV, raising=False)
    result = {
        'runtime_action': 'installed_cuda_reexec',
        'selected_backend': 'cuda',
        'backend': 'cuda',
        'interpreter': sys.executable,
        'llama_module_path': 'C:/Python/Lib/site-packages/llama_cpp/__init__.py',
        'yarn_rope_supported': True,
    }

    public = desktop_runtime_setup._record_desktop_runtime_probe(result)
    private = json.loads(os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV])

    expected_identity = desktop_runtime_setup.llama_module_identity_from_path(result['llama_module_path'])
    serialized_env = os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV]

    assert 'llama_module_path' not in public
    assert 'llama_module_path' not in private
    assert result['llama_module_path'] not in serialized_env
    assert private['llama_module_identity'] == expected_identity
    assert public['yarn_rope_supported'] == 'true'
    assert private['yarn_rope_supported'] is True


def test_windows_cuda_already_supported_preserves_runtime_action(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (tmp_path / 'desktop-site', None),
    )
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='cuda', gpu=True, device='cuda'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path)
    recorded = json.loads(os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV])

    expected_identity = desktop_runtime_setup.llama_module_identity_from_path(
        'C:/Python/Lib/site-packages/llama_cpp/__init__.py'
    )
    serialized_env = os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV]

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'
    assert 'llama_module_path' not in result
    assert 'llama_module_path' not in recorded
    assert 'C:/Python/Lib/site-packages/llama_cpp/__init__.py' not in serialized_env
    assert recorded['llama_module_identity'] == expected_identity


def test_windows_cuda_bootstrap_uses_cuda_target_without_macos_metal_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (False, 'cooldown'))
    dependency_target = tmp_path / 'desktop-site'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (dependency_target, None),
    )
    probes = iter([
        _probe(backend='missing', gpu=False, device='none', error="No module named 'llama_cpp'"),
        _probe(backend='cuda', gpu=True, device='cuda'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cuda',
            package_spec='llama-cpp-python',
            cmake_args='-DGGML_CUDA=on',
            force_cmake=True,
            index_url='https://example.invalid/cuda',
            only_binary=False,
            no_binary=True,
        )
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    captured = {}

    def _run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        return True, 'command=python -m pip install --target target; returncode=0; stdout_tail=ok; stderr_tail=empty'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path)

    assert result['runtime_action'] == 'failed'
    assert 'cooldown' in result['fallback_reason']
    assert captured == {}


def test_windows_cuda_source_repair_continues_when_qwen_64k_yarn_missing(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements_desktop_runtime.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    recorded_failures = []
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', recorded_failures.append)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (tmp_path / 'desktop-site', None),
    )
    probes = iter([
        _probe(backend='cuda', gpu=True, device='cuda', yarn=False),
        _probe(backend='cuda', gpu=True, device='cuda', yarn=False),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    monkeypatch.setattr(desktop_runtime_setup, '_run_windows_cuda_source_repair', lambda *_args: (True, 'ok'))
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])
    monkeypatch.setattr(desktop_runtime_setup, '_fallback_unpinned_plans', lambda _platform: [])

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] == 'failed'
    assert result['yarn_rope_supported'] == 'false'
    assert (
        'Qwen 64K requires YaRN/RoPE support in llama-cpp-python; runtime repair failed'
        in result['fallback_reason']
    )
    assert 'resolver=unsupported' in result['fallback_reason']
    assert 'version=0.3.32' in result['fallback_reason']
    assert 'module=' not in result['fallback_reason']
    assert 'C:/Python/Lib/site-packages/llama_cpp/__init__.py' not in result['fallback_reason']
    assert 'rope_scaling_type_supported=False' in result['fallback_reason']
    assert 'rope_freq_scale_supported=False' in result['fallback_reason']
    assert 'yarn_orig_ctx_supported=False' in result['fallback_reason']
    assert recorded_failures


def test_windows_cuda_source_repair_returns_reexec_when_qwen_64k_yarn_verified(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements_desktop_runtime.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (tmp_path / 'desktop-site', None),
    )
    probes = iter([
        _probe(backend='cuda', gpu=True, device='cuda', yarn=False),
        _probe(backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    monkeypatch.setattr(desktop_runtime_setup, '_run_windows_cuda_source_repair', lambda *_args: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['selected_backend'] == 'cuda'
    assert result['yarn_rope_supported'] == 'true'


def test_qwen_64k_install_plan_continues_when_gpu_runtime_still_lacks_yarn(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements_desktop_runtime.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin'))
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    dependency_target = tmp_path / 'desktop-site'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (dependency_target, None),
    )
    probes = iter([
        _probe(backend='metal', gpu=True, device='metal', yarn=False),
        _probe(backend='metal', gpu=True, device='metal', yarn=False),
        _probe(backend='metal', gpu=True, device='metal', yarn=True, resolver='numeric_fallback'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin',
            backend='metal',
            package_spec='llama-cpp-python==0.3.31',
            cmake_args='-DGGML_METAL=on',
            force_cmake=True,
            index_url='https://pypi.org/simple',
            only_binary=False,
            no_binary=True,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='darwin',
            backend='metal',
            package_spec='llama-cpp-python==0.3.32',
            cmake_args='-DGGML_METAL=on',
            force_cmake=True,
            index_url='https://pypi.org/simple',
            only_binary=False,
            no_binary=True,
        ),
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    installs = []
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *args, **kwargs: (installs.append(args), 'ok') and (True, 'ok'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert len(installs) == 2
    assert result['runtime_action'] == 'installed_metal_reexec'
    assert result['yarn_rope_supported'] == 'true'


def test_qwen_64k_probe_only_fallback_reason_keeps_yarn_context(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements_desktop_runtime.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='cuda', gpu=True, device='cuda', yarn=False),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] == 'probe_only'
    assert 'Qwen 64K requires YaRN/RoPE support' in result['fallback_reason']


def test_probe_result_payload_preserves_native_capability_types():
    support = {name: True for name in desktop_runtime_setup.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cuda',
        gpu_offload_supported=True,
        detected_device='cuda',
        interpreter=sys.executable,
        prefix=sys.prefix,
        llama_module_path='C:/Python/Lib/site-packages/llama_cpp/__init__.py',
        llama_cpp_python_version='0.3.32',
        yarn_rope_supported=True,
        yarn_resolver_source='numeric_fallback',
        rope_scaling_type_supported=True,
        yarn_ext_factor_supported=True,
        rope_freq_scale_supported=True,
        yarn_orig_ctx_supported=True,
        constructor_kwarg_support=support,
        constructor_has_var_kwargs=False,
        constructor_signature_inspectable=True,
        qwen_64k_yarn_support='supported',
        yarn_enum_value=2,
        q8_kv_cache_type_value=8,
        q4_kv_cache_type_value=2,
        f16_kv_cache_type_value=1,
    )

    payload = desktop_runtime_setup._probe_result_payload(probe)
    encoded = json.loads(json.dumps(payload))

    assert encoded['gpu_offload_supported'] is True
    assert encoded['constructor_kwarg_support'] == support
    assert encoded['constructor_signature_inspectable'] is True
    assert encoded['qwen_64k_yarn_support'] == 'supported'
    assert encoded['yarn_enum_value'] == 2
    assert encoded['q8_kv_cache_type_value'] == 8
    assert encoded['q4_kv_cache_type_value'] == 2
    assert encoded['f16_kv_cache_type_value'] == 1
    assert encoded['capability_source'] == 'desktop_runtime_setup_probe'


def test_llama_cpp_version_match_uses_stdlib_when_packaging_is_unavailable(monkeypatch):
    real_import = __import__

    def import_without_packaging(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith('packaging.'):
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr('builtins.__import__', import_without_packaging)

    assert (
        desktop_runtime_setup._llama_cpp_version_matches(
            '0.3.32',
            'llama-cpp-python==0.3.32',
        )
        == 'match'
    )


def test_runtime_probe_payload_filters_unknown_constructor_kwarg_support(monkeypatch, tmp_path):
    payload = {
        'backend': 'cuda',
        'gpu_offload_supported': True,
        'detected_device': 'cuda',
        'constructor_kwarg_support': {
            'rope_scaling_type': True,
            'unexpected_future_kwarg': True,
        },
    }

    class Result:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ''

    _install_fake_probe_popen(monkeypatch, Result())

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

    assert probe.constructor_kwarg_support == {'rope_scaling_type': True}


def test_qwen_64k_runtime_repair_failed_reason_omits_module_path():
    probe = _probe(backend='cuda', gpu=True, device='cuda', yarn=False)

    reason = desktop_runtime_setup._qwen_64k_runtime_repair_failed_reason(
        probe,
        version_match='match',
    )

    assert 'module=' not in reason
    assert probe.llama_module_path not in reason
    assert 'version=0.3.32' in reason
    assert 'version_match=match' in reason
    assert 'rope_scaling_type_supported=False' in reason


def test_qwen_64k_compatible_runtime_after_dependency_provision_is_already_supported(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )
    os.environ.pop('TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON', None)

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'
    assert result['llama_cpp_python_installed_version'] == '0.3.32'
    assert result['llama_cpp_python_required_version'] == '0.3.32'
    assert result['llama_cpp_python_version_match'] == 'match'


def test_qwen_64k_stale_cuda_0316_repairs_to_0322_reexec(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (tmp_path / 'desktop-site', None),
    )
    probes = iter([
        _probe(backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback', version='0.3.16'),
        _probe(backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback', version='0.3.32'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    monkeypatch.setattr(desktop_runtime_setup, '_run_windows_cuda_source_repair', lambda *_args: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['llama_cpp_python_installed_version'] == '0.3.32'
    assert result['llama_cpp_python_version_match'] == 'match'


def test_qwen_64k_unknown_version_never_already_supported(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(
            backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback', version='unknown'
        ),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] != 'already_supported'
    assert result['llama_cpp_python_version_match'] == 'unknown'


@pytest.mark.parametrize(('backend', 'action'), [('cuda', 'already_supported'), ('metal', 'metal_already_supported')])
def test_qwen_64k_exact_0322_cuda_and_metal_fast_paths(monkeypatch, tmp_path, backend, action):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _PlatformStub('darwin') if backend == 'metal' else _SysStub)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(
            backend=backend, gpu=True, device=backend, yarn=True, resolver='numeric_fallback', version='0.3.32'
        ),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] == action
    assert result['llama_cpp_python_version_match'] == 'match'


def test_qwen_64k_local_0322_build_satisfies_exact_requirement(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: _probe(
            backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback', version='0.3.32+local'
        ),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert result['runtime_action'] == 'already_supported'
    assert result['llama_cpp_python_installed_version'] == '0.3.32+local'
    assert result['llama_cpp_python_version_match'] == 'match'


def test_qwen_64k_stale_post_install_probe_does_not_reexec(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_resolve_desktop_dependency_target',
        lambda _root: (tmp_path / 'desktop-site', None),
    )
    probes = iter([
        _probe(backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback', version='0.3.16'),
        _probe(backend='cuda', gpu=True, device='cuda', yarn=True, resolver='numeric_fallback', version='0.3.16'),
    ])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda **_: next(probes))
    monkeypatch.setattr(desktop_runtime_setup, '_run_windows_cuda_source_repair', lambda *_args: (True, 'ok'))
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])
    monkeypatch.setattr(desktop_runtime_setup, '_fallback_unpinned_plans', lambda _platform: [])

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )

    assert not result['runtime_action'].startswith('installed_')
    assert result['runtime_action'] == 'failed'
    assert result['llama_cpp_python_installed_version'] == '0.3.16'
    assert result['llama_cpp_python_version_match'] == 'mismatch'


def test_qwen_64k_bootstrap_disabled_version_mismatch_failed_without_module_path(monkeypatch, tmp_path):
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    sentinel = 'C:/SECRET/SENTINEL/llama_cpp/__init__.py'
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_requirements_path', lambda _root: requirements_path)
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda **_: desktop_runtime_setup.RuntimeProbe(
            backend='cuda',
            gpu_offload_supported=True,
            detected_device='cuda',
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path=sentinel,
            llama_cpp_python_version='0.3.16',
            yarn_rope_supported=True,
            yarn_resolver_source='numeric_fallback',
            rope_scaling_type_supported=True,
            yarn_ext_factor_supported=True,
            yarn_orig_ctx_supported=True,
        ),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime(
        'auto', repo_root=tmp_path, context_tier='64k-full'
    )
    emitted = os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV]

    assert result['runtime_action'] == 'version_mismatch_failed'
    assert result['llama_cpp_python_version_match'] == 'mismatch'
    assert sentinel not in result['fallback_reason']
    assert sentinel not in json.dumps(result, sort_keys=True)
    assert sentinel not in emitted


def test_managed_site_lock_closes_handle_when_enter_times_out(monkeypatch, tmp_path):
    closed = {'value': False}

    class FakeHandle:
        def fileno(self):
            return 42

        def seek(self, _offset):
            return None

        def close(self):
            closed['value'] = True

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2

        @staticmethod
        def flock(_fileno, _flags):
            raise OSError('busy')

    monkeypatch.setattr(desktop_runtime_setup, 'open', lambda *_args, **_kwargs: FakeHandle(), raising=False)
    monkeypatch.setitem(sys.modules, 'fcntl', FakeFcntl)
    monkeypatch.setattr(desktop_runtime_setup.time, 'sleep', lambda _seconds: None)

    with pytest.raises(TimeoutError):
        with desktop_runtime_setup._ManagedSiteMutationLock(tmp_path, timeout_seconds=0):
            pass

    assert closed['value'] is True


def test_ensure_desktop_python_dependencies_maps_compat_timeout_to_timeout_action(monkeypatch, tmp_path):
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('psutil==1\nrequests==1\npython-dotenv==1\ncryptography==1\n', encoding='utf-8')
    target = tmp_path / 'site'
    target.mkdir()

    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda repo_root=None: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_dependency_target', lambda _root: (target, None))
    monkeypatch.setattr(desktop_runtime_setup, '_module_missing', lambda _modules: ['psutil'])
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (False, 'pip install timed out after 12s; stderr_tail=empty'),
    )

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=tmp_path)

    assert result['action'] == 'install_timeout'


def test_small_helpers_cover_redaction_truncation_and_requirements_edges(tmp_path, monkeypatch):
    assert desktop_runtime_setup._tail_text('abcdef', limit=3) == '...def'
    cmd = [sys.executable, '-m', 'pip', 'install', '/tmp/local.whl', 'pkg==1']
    summary = desktop_runtime_setup._command_summary(cmd)
    assert '<python>' in summary
    assert '<path>' in summary
    assert 'pkg==1' in summary

    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('\n# comment\nrequests==2\npython-dotenv==1\n', encoding='utf-8')
    assert desktop_runtime_setup._requirements_for_missing(requirements, ['dotenv']) == ['python-dotenv==1']
    assert desktop_runtime_setup._requirements_for_missing(tmp_path / 'missing.txt', ['requests']) == []

    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', raising=False)
    monkeypatch.setattr(desktop_runtime_setup.Path, 'home', lambda: tmp_path / 'home')
    assert desktop_runtime_setup._existing_desktop_dependency_target(tmp_path) == (None, 'no existing desktop dependency target')


class _KillFailProcess:
    pid = 999999

    def kill(self):
        raise RuntimeError('kill failed')


def test_terminate_process_tree_falls_back_when_group_kill_fails(monkeypatch):
    calls = []

    def fail_killpg(_pid, _sig):
        calls.append('killpg')
        raise OSError('no group')

    monkeypatch.setattr(desktop_runtime_setup.os, 'name', 'posix')
    monkeypatch.setattr(desktop_runtime_setup.os, 'killpg', fail_killpg)
    desktop_runtime_setup._terminate_process_tree(_KillFailProcess())
    assert calls == ['killpg']


def test_lock_cancel_and_exit_no_handle_paths(tmp_path):
    lock = desktop_runtime_setup._ManagedSiteMutationLock(
        tmp_path / 'site' / '.lock',
        timeout_seconds=1,
        cancellation_predicate=lambda: True,
    )
    with pytest.raises(TimeoutError, match='cancelled'):
        lock.__enter__()
    assert lock._handle is None
    lock.__exit__(None, None, None)


def test_read_only_dependency_preflight_reports_missing_without_target_creation(tmp_path, monkeypatch):
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', raising=False)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda repo_root=None: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: tmp_path / 'requirements.txt')
    monkeypatch.setattr(desktop_runtime_setup.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(desktop_runtime_setup, '_module_missing', lambda _required: ['requests'])

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(mutate=False)

    assert result['action'] == 'missing_read_only'
    assert result['dependency_target'] == ''
    assert not (tmp_path / 'desktop-python-site').exists()


def test_dependency_preflight_post_lock_satisfied_and_unmapped(tmp_path, monkeypatch):
    target = tmp_path / 'site'
    target.mkdir()
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('requests==2\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda repo_root=None: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_dependency_target', lambda _root: (target, None))
    missing_sequences = iter([['requests'], []])
    monkeypatch.setattr(desktop_runtime_setup, '_module_missing', lambda _required: next(missing_sequences))

    result = desktop_runtime_setup.ensure_desktop_python_dependencies()
    assert result['action'] == 'already_satisfied_post_lock'

    missing_sequences = iter([['not_allowlisted'], ['not_allowlisted']])
    monkeypatch.setattr(desktop_runtime_setup, '_module_missing', lambda _required: next(missing_sequences))
    result = desktop_runtime_setup.ensure_desktop_python_dependencies()
    assert result['action'] == 'missing_requirement_unmapped'


def test_pip_install_start_failure_and_wait_timeout(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_a, **_k: (_ for _ in ()).throw(OSError('boom')))
    ok, detail = desktop_runtime_setup._run_pip_install([sys.executable, '-m', 'pip'], os.environ.copy())
    assert ok is False
    assert 'failed to start' in detail

    class Stream:
        def readline(self):
            return ''
        def close(self):
            raise RuntimeError('close failed')

    class Proc:
        pid = 999999
        stdout = Stream()
        stderr = Stream()
        def __init__(self):
            self.waits = 0
        def poll(self):
            return 0
        def wait(self, timeout=None):
            raise desktop_runtime_setup.subprocess.TimeoutExpired(['pip'], timeout)
        def kill(self):
            pass

    proc = Proc()
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_a, **_k: proc)
    monkeypatch.setattr(desktop_runtime_setup, '_terminate_process_tree', lambda process: None)
    ok, detail = desktop_runtime_setup._run_pip_install([sys.executable, '-m', 'pip'], os.environ.copy())
    assert ok is True
    assert 'returncode=0' in detail


def test_runtime_install_threads_cancellation_and_heartbeat_kwargs(monkeypatch, tmp_path):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    target = tmp_path / 'site'
    target.mkdir()
    captured = []
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (False, 'cooling'))
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [desktop_runtime_setup.LlamaCppInstallPlan(platform='win32', force_cmake=True, index_url=None, only_binary=False, backend='cuda', package_spec='llama-cpp-python==0.3.32', no_binary=True, cmake_args='-DGGML_CUDA=on')])
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda cmd, env, **kwargs: captured.append(kwargs) or (False, 'outcome=cancelled'))
    monkeypatch.setattr(desktop_runtime_setup, '_probe_runtime', lambda _root: _probe(error='missing', version='unknown'))
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda repo_root=None: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_dependency_target', lambda _root: (target, None))
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup.platform_module, 'system', lambda: 'Windows')
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')

    cancel = lambda: False
    heartbeat = lambda _extra: None
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', context_tier='8k', cancellation_predicate=cancel, heartbeat=heartbeat)

    assert result['runtime_action'] in {'runtime_repair_failed', 'version_mismatch_failed', 'failed'}
    assert captured == []


def test_lock_wait_heartbeat_and_windows_branches(tmp_path, monkeypatch):
    events = []
    times = iter([0.0, 0.0, 5.1, 5.2, 6.1])
    monkeypatch.setattr(desktop_runtime_setup.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(desktop_runtime_setup.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(desktop_runtime_setup.os, 'name', 'nt')

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2
        calls = 0
        @classmethod
        def locking(cls, _fd, mode, _size):
            cls.calls += 1
            if mode == cls.LK_NBLCK:
                raise OSError('busy')

    monkeypatch.setitem(sys.modules, 'msvcrt', FakeMsvcrt)
    lock = desktop_runtime_setup._ManagedSiteMutationLock(tmp_path / '.lock', timeout_seconds=6, heartbeat=events.append)
    with pytest.raises(TimeoutError):
        lock.__enter__()
    assert events == [{'startup_elapsed_ms': 5100, 'startup_deadline_ms': 6000, 'startup_phase': 'lock_wait'}]
    assert lock._handle is None

    # Cover the Windows unlock path on __exit__ with an already-held fake handle.
    class Handle:
        def seek(self, _pos):
            pass
        def fileno(self):
            return 1
        def close(self):
            pass
    lock._handle = Handle()
    FakeMsvcrt.locking = classmethod(lambda cls, _fd, _mode, _size: None)
    lock.__exit__(None, None, None)
    assert lock._handle is None


def test_run_pip_install_heartbeat_failure_terminates_process_tree(monkeypatch):
    class Stream:
        def readline(self):
            return ''
        def close(self):
            pass

    class Proc:
        pid = 12345
        stdout = Stream()
        stderr = Stream()
        def __init__(self):
            self.polls = 0
        def poll(self):
            self.polls += 1
            return None if self.polls < 3 else -15
        def wait(self, timeout=None):
            return -15
        def kill(self):
            pass

    proc = Proc()
    terminated = []
    times = iter([0.0, 5.1])
    monkeypatch.setattr(desktop_runtime_setup.time, 'monotonic', lambda: next(times, 5.2))
    monkeypatch.setattr(desktop_runtime_setup.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(desktop_runtime_setup, '_terminate_process_tree', lambda process: terminated.append(process.pid))

    def failing_heartbeat(_extra):
        raise RuntimeError('emit failed')

    ok, detail = desktop_runtime_setup._run_pip_install(
        [sys.executable, '-m', 'pip', 'install', 'requests==2'],
        os.environ.copy(),
        heartbeat=failing_heartbeat,
    )

    assert ok is False
    assert terminated == [12345]
    assert 'outcome=heartbeat_failed' in detail
    assert 'heartbeat_error=RuntimeError' in detail


def test_managed_site_lock_heartbeat_failure_closes_handle(tmp_path, monkeypatch):
    times = iter([0.0, 0.0, 5.1])
    monkeypatch.setattr(desktop_runtime_setup.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(desktop_runtime_setup.time, 'sleep', lambda _seconds: None)

    def busy_flock(_fileno, _flags):
        raise OSError('busy')

    monkeypatch.setattr(desktop_runtime_setup.os, 'name', 'posix')
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=busy_flock))

    def failing_heartbeat(_extra):
        raise RuntimeError('emit failed')

    lock = desktop_runtime_setup._ManagedSiteMutationLock(tmp_path / 'site', timeout_seconds=10, heartbeat=failing_heartbeat)
    with pytest.raises(TimeoutError, match='heartbeat failed'):
        lock.__enter__()

    assert lock._handle is None


def test_probe_llama_runtime_cancellable_path_emits_runtime_probe_heartbeat(monkeypatch, tmp_path):
    class ProbeProcess:
        pid = 43210
        returncode = None
        stdout = io.BytesIO(desktop_runtime_setup.PROBE_RESULT_PREFIX + json.dumps({'backend': 'cpu', 'gpu_offload_supported': False}).encode() + b'\n')
        stderr = io.StringIO('')
        def __init__(self):
            self.polls = 0
        def poll(self):
            self.polls += 1
            if self.polls > 2:
                self.returncode = 0
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    proc = ProbeProcess()
    heartbeats = []
    times = iter([0.0, 0.0, 5.1, 5.2])
    monkeypatch.setattr(desktop_runtime_setup.time, 'monotonic', lambda: next(times, 5.2))
    monkeypatch.setattr(desktop_runtime_setup.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_args, **_kwargs: proc)

    probe = desktop_runtime_setup._probe_llama_runtime(
        runtime_root=tmp_path,
        cancellation_predicate=lambda: False,
        heartbeat=heartbeats.append,
    )

    assert probe.backend == 'cpu'
    assert heartbeats in ([], [{'startup_elapsed_ms': 5100, 'startup_deadline_ms': 30000, 'startup_phase': 'runtime_probe'}])


def test_probe_llama_runtime_cancellation_terminates_subprocess(monkeypatch, tmp_path):
    class ProbeProcess:
        pid = 54321
        returncode = None
        stdout = io.StringIO('')
        stderr = io.StringIO('')
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = -15
            return -15

    proc = ProbeProcess()
    terminated = []
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(desktop_runtime_setup, '_terminate_process_tree', lambda process: terminated.append(process.pid))

    probe = desktop_runtime_setup._probe_llama_runtime(
        runtime_root=tmp_path,
        cancellation_predicate=lambda: True,
        heartbeat=lambda _extra: None,
    )

    assert probe.error == 'desktop_runtime_probe_cancelled'
    assert terminated == [54321]


def test_probe_llama_runtime_drains_large_stderr_before_json_payload(monkeypatch, tmp_path):
    snippet = r"""
import json
import sys
sys.stderr.write('x' * (1024 * 1024 * 2) + '\n')
sys.stderr.flush()
print('TOKEN_PLACE_RUNTIME_PROBE_RESULT ' + json.dumps({
    'backend': 'metal',
    'gpu_offload_supported': True,
    'detected_device': 'apple-gpu',
    'interpreter': sys.executable,
    'prefix': sys.prefix,
    'llama_module_path': '/safe/site/llama_cpp/__init__.py',
}))
"""
    monkeypatch.setattr(desktop_runtime_setup, '_PROBE_SNIPPET', snippet)

    started = time.monotonic()
    probe = desktop_runtime_setup._probe_llama_runtime(
        runtime_root=tmp_path,
        cancellation_predicate=lambda: False,
        heartbeat=lambda _extra: None,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 30
    assert probe.backend == 'metal'
    assert probe.gpu_offload_supported is True
    assert probe.error is None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_gone(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_alive(pid)


def test_probe_returns_promptly_after_valid_result_and_reaps_hung_child(monkeypatch, tmp_path):
    marker = tmp_path / 'probe_pid.txt'
    snippet = f"""
import json
import os
import sys
import time
with open({str(marker)!r}, 'w', encoding='utf-8') as handle:
    handle.write(str(os.getpid()))
    handle.flush()
print('TOKEN_PLACE_RUNTIME_PROBE_RESULT ' + json.dumps({{'backend':'cpu','gpu_offload_supported':False,'detected_device':'cpu','interpreter':sys.executable,'prefix':sys.prefix,'llama_module_path':'/tmp/llama_cpp/__init__.py'}}), flush=True)
time.sleep(60)
"""
    monkeypatch.setattr(desktop_runtime_setup, '_PROBE_SNIPPET', snippet)

    started = time.monotonic()
    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)
    elapsed = time.monotonic() - started

    child_pid = int(marker.read_text())
    assert elapsed < 10
    assert probe.backend == 'cpu'
    assert probe.error is None
    assert _wait_until_gone(child_pid)


def test_probe_drains_large_unlined_streams_and_parses_result(monkeypatch, tmp_path):
    snippet = """
import json
import sys
sys.stdout.write('x' * (1024 * 1024))
sys.stdout.flush()
sys.stderr.write('y' * (1024 * 1024))
sys.stderr.flush()
print('TOKEN_PLACE_RUNTIME_PROBE_RESULT ' + json.dumps({'backend':'cuda','gpu_offload_supported':True,'detected_device':'cuda','interpreter':sys.executable,'prefix':sys.prefix,'llama_module_path':'/tmp/llama_cpp/__init__.py'}), flush=True)
"""
    monkeypatch.setattr(desktop_runtime_setup, '_PROBE_SNIPPET', snippet)

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

    assert probe.backend == 'cuda'
    assert probe.gpu_offload_supported is True
    assert probe.error is None


def test_probe_parses_large_result_frame_beyond_diagnostic_tail(monkeypatch, tmp_path):
    large_path = '/safe/' + ('a' * (desktop_runtime_setup.INSTALL_LOG_TAIL_MAX_CHARS + 100))
    snippet = f"""
import json
import sys
print('TOKEN_PLACE_RUNTIME_PROBE_RESULT ' + json.dumps({{'backend':'metal','gpu_offload_supported':True,'detected_device':'metal','interpreter':sys.executable,'prefix':sys.prefix,'llama_module_path':{large_path!r}}}), flush=True)
"""
    monkeypatch.setattr(desktop_runtime_setup, '_PROBE_SNIPPET', snippet)

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

    assert probe.backend == 'metal'
    assert probe.llama_module_path == large_path
    assert len(probe.llama_module_path) > desktop_runtime_setup.INSTALL_LOG_TAIL_MAX_CHARS


def test_probe_timeout_before_result_terminates_child(monkeypatch, tmp_path):
    marker = tmp_path / 'timeout_pid.txt'
    snippet = f"""
import os
import time
with open({str(marker)!r}, 'w', encoding='utf-8') as handle:
    handle.write(str(os.getpid()))
    handle.flush()
time.sleep(60)
"""
    monkeypatch.setattr(desktop_runtime_setup, '_PROBE_SNIPPET', snippet)
    times = iter([0.0, 31.0])
    monkeypatch.setattr(desktop_runtime_setup.time, 'monotonic', lambda: next(times, 31.0))

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path, heartbeat=lambda _extra: None)

    assert probe.error == 'desktop_runtime_probe_timeout_after_30s'


def test_probe_cancellation_terminates_real_child(monkeypatch, tmp_path):
    marker = tmp_path / 'cancel_pid.txt'
    snippet = f"""
import os
import time
with open({str(marker)!r}, 'w', encoding='utf-8') as handle:
    handle.write(str(os.getpid()))
    handle.flush()
time.sleep(60)
"""
    monkeypatch.setattr(desktop_runtime_setup, '_PROBE_SNIPPET', snippet)

    def cancel_after_child_started():
        return marker.exists()

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path, cancellation_predicate=cancel_after_child_started)

    child_pid = int(marker.read_text())
    assert probe.error == 'desktop_runtime_probe_cancelled'
    assert _wait_until_gone(child_pid)


def test_actual_probe_snippet_with_fake_llama_cpp_package(monkeypatch, tmp_path):
    target = tmp_path / 'managed_site'
    package = target / 'llama_cpp'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text(
        "__version__ = '0.3.32+local'\n"
        "GGML_METAL = True\n"
        "def llama_supports_gpu_offload():\n    return True\n"
        "LLAMA_ROPE_SCALING_TYPE_YARN = 2\n"
        "GGML_TYPE_Q8_0 = 8\n"
        "GGML_TYPE_Q4_0 = 4\n"
        "GGML_TYPE_F16 = 16\n"
        "class Llama:\n"
        "    def __init__(self, rope_scaling_type=None, rope_freq_scale=None, yarn_orig_ctx=None, **kwargs):\n"
        "        pass\n",
        encoding='utf-8',
    )
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', str(target))

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

    assert probe.backend == 'metal'
    assert probe.gpu_offload_supported is True
    assert probe.llama_cpp_python_version == '0.3.32+local'
    assert probe.yarn_rope_supported is True
    assert probe.error is None


def test_probe_result_frame_validation_failures_return_safe_errors(monkeypatch, tmp_path):
    class ProbeProcess:
        pid = 65432
        returncode = None

        def __init__(self, stdout: bytes):
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(b'')

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    cases = [
        (
            desktop_runtime_setup.PROBE_RESULT_PREFIX + b'["not-an-object"]\n',
            'desktop_runtime_probe_result_not_object',
        ),
        (
            desktop_runtime_setup.PROBE_RESULT_PREFIX + b'{not-json}\n',
            'desktop_runtime_probe_result_malformed',
        ),
        (
            desktop_runtime_setup.PROBE_RESULT_PREFIX + b'{' + (b'"x"' * desktop_runtime_setup.PROBE_RESULT_MAX_BYTES),
            'desktop_runtime_probe_result_oversized',
        ),
    ]

    for stdout, expected_error in cases:
        proc = ProbeProcess(stdout)
        monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_args, **_kwargs: proc)

        probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

        assert probe.error == expected_error
        assert probe.backend == 'missing'


def test_probe_reader_failures_are_safe_and_close_streams(monkeypatch, tmp_path):
    class FailingStream:
        def __init__(self):
            self.closed = False

        def read(self, _size=-1):
            raise OSError('reader exploded with /tmp/secret/path')

        def close(self):
            self.closed = True
            raise OSError('close failed')

    class ProbeProcess:
        pid = 65433
        returncode = None

        def __init__(self):
            self.stdout = FailingStream()
            self.stderr = io.BytesIO(b'')

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    proc = ProbeProcess()
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_args, **_kwargs: proc)

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

    assert probe.error == 'desktop_runtime_probe_reader_failed:OSError'
    assert proc.stdout.closed is True
    assert '/tmp/secret/path' not in probe.error


def test_probe_heartbeat_failure_terminates_process_tree(monkeypatch, tmp_path):
    class ProbeProcess:
        pid = 65434
        returncode = None
        stdout = io.BytesIO(b'')
        stderr = io.BytesIO(b'')

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -15
            return -15

    proc = ProbeProcess()
    terminated = []
    times = iter([0.0, 0.0, 5.1])
    monkeypatch.setattr(desktop_runtime_setup.time, 'monotonic', lambda: next(times, 5.2))
    monkeypatch.setattr(desktop_runtime_setup.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'Popen', lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(desktop_runtime_setup, '_terminate_process_tree', lambda process: terminated.append(process.pid))

    def failing_heartbeat(_extra):
        raise RuntimeError('heartbeat sink failed')

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path, heartbeat=failing_heartbeat)

    assert probe.error == 'desktop_runtime_probe_heartbeat_failed:RuntimeError'
    assert terminated == [65434]


def test_probe_result_payload_carries_private_identity_but_public_result_redacts(monkeypatch, tmp_path):
    support = {name: True for name in desktop_runtime_setup.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    module_path = tmp_path / 'site-packages' / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir(parents=True)
    module_path.write_text('# mock')
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='metal', gpu_offload_supported=True, detected_device='metal',
        interpreter=sys.executable, prefix=sys.prefix, llama_module_path=str(module_path),
        llama_cpp_python_version='0.3.32', yarn_rope_supported=True,
        yarn_resolver_source='top_level_enum', constructor_kwarg_support=support,
        constructor_signature_inspectable=True, qwen_64k_yarn_support='supported', yarn_enum_value=2,
    )
    payload = desktop_runtime_setup._probe_result_payload(probe)
    identity = payload['llama_module_identity']
    assert identity.startswith('sha256:') and len(identity) == 71
    assert payload['llama_module_path_present'] is True
    assert 'llama_module_path' not in payload

    monkeypatch.setattr(desktop_runtime_setup, '_ensure_desktop_llama_runtime_impl', lambda *_, **__: dict(payload, selected_backend='metal', runtime_action='metal_already_supported'))
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=tmp_path, context_tier='64k-full')
    assert 'llama_module_identity' not in result
    assert str(module_path) not in json.dumps(result)
    private_env = json.loads(os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV])
    assert private_env['llama_module_identity'] == identity
    assert str(module_path) not in os.environ[desktop_runtime_setup.RUNTIME_PROBE_ENV]


def test_llama_module_identity_canonicalizes_symlink_dotdot(tmp_path):
    real = tmp_path / 'real' / 'llama_cpp' / '__init__.py'
    real.parent.mkdir(parents=True)
    real.write_text('# mock')
    link_dir = tmp_path / 'link'
    link_dir.symlink_to(real.parent.parent, target_is_directory=True)
    via_link = link_dir / 'llama_cpp' / '..' / 'llama_cpp' / '__init__.py'
    assert desktop_runtime_setup.llama_module_identity_from_path(real) == desktop_runtime_setup.llama_module_identity_from_path(via_link)
    other = tmp_path / 'other' / 'llama_cpp' / '__init__.py'
    other.parent.mkdir(parents=True)
    other.write_text('# other')
    assert desktop_runtime_setup.llama_module_identity_from_path(real) != desktop_runtime_setup.llama_module_identity_from_path(other)


def test_llama_module_identity_rejects_raw_path_sentinels():
    assert desktop_runtime_setup.llama_module_identity_from_path('unknown') is None
    assert desktop_runtime_setup.llama_module_identity_from_path('missing') is None
    assert desktop_runtime_setup.llama_module_identity_from_path('') is None
    assert desktop_runtime_setup._canonical_llama_module_identity_input(None) is None
    assert desktop_runtime_setup._canonical_llama_module_identity_input('  UNKNOWN  ') is None


def test_shared_llama_module_identity_helper_requires_string_identity(tmp_path):
    from utils.llm import llama_module_identity as shared_identity

    module_path = tmp_path / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir()
    module_path.write_text('# mock')

    identity = shared_identity.llama_module_identity_from_path(module_path)
    assert shared_identity.valid_llama_module_identity(identity) == identity
    assert shared_identity.valid_llama_module_identity(identity.upper()) is None
    assert shared_identity.valid_llama_module_identity(123) is None
    assert shared_identity.llama_module_identity_supplied(identity) is True
    assert shared_identity.llama_module_identity_supplied('  ') is False
    assert shared_identity.llama_module_identity_supplied(123) is False


def test_shared_llama_module_identity_helper_fallback_paths(monkeypatch, tmp_path):
    from utils.llm import llama_module_identity as shared_identity

    module_path = tmp_path / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir()
    module_path.write_text('# mock')

    monkeypatch.setattr(shared_identity.os.path, 'abspath', lambda _path: (_ for _ in ()).throw(OSError('blocked')))
    canonical = shared_identity.canonical_llama_module_identity_input(module_path)
    assert canonical.endswith('/llama_cpp/__init__.py')
    assert shared_identity.canonical_llama_module_identity_input(None) is None


def test_shared_llama_module_identity_helper_edge_paths(monkeypatch):
    from utils.llm import llama_module_identity as shared_identity

    assert shared_identity.strip_windows_extended_path_prefix(
        r'\\?\UNC\server\share\llama_cpp\__init__.py'
    ) == r'\\server\share\llama_cpp\__init__.py'
    assert shared_identity.strip_windows_extended_path_prefix(
        r'\\?\C:\runtime\llama_cpp\__init__.py'
    ) == r'C:\runtime\llama_cpp\__init__.py'
    assert shared_identity.llama_module_identity_from_path('unknown') is None
    assert shared_identity.llama_module_identity_from_path('missing') is None

    class UnstringablePath:
        def __str__(self):
            raise ValueError('blocked')

    assert shared_identity.canonical_llama_module_identity_input(UnstringablePath()) is None

    good = 'sha256:' + 'b' * 64
    assert shared_identity.valid_llama_module_identity(f' {good} ') == good


def test_shared_llama_module_identity_helper_fail_closed_normalization_paths(monkeypatch, tmp_path):
    from utils.llm import llama_module_identity as shared_identity

    module_path = tmp_path / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir()
    module_path.write_text('# mock', encoding='utf-8')

    identity = shared_identity.llama_module_identity_from_path(module_path)
    assert identity is not None
    assert shared_identity.valid_llama_module_identity(identity) == identity
    assert shared_identity.llama_module_identity_supplied(identity) is True

    with monkeypatch.context() as path_errors:
        path_errors.setattr(
            shared_identity.os.path,
            'abspath',
            lambda _path: (_ for _ in ()).throw(OSError('primary blocked')),
        )
        path_errors.setattr(
            shared_identity.os.path,
            'normpath',
            lambda _path: (_ for _ in ()).throw(ValueError('fallback blocked')),
        )

        assert shared_identity.canonical_llama_module_identity_input(module_path) is None
        assert shared_identity.llama_module_identity_from_path(module_path) is None
    assert shared_identity.valid_llama_module_identity(object()) is None
    assert shared_identity.llama_module_identity_supplied(object()) is False


def test_llama_module_identity_windows_normalization_is_deterministic():
    base = r'C:\Users\Alice\AppData\Local\token.place\runtime\Lib\site-packages\llama_cpp\__init__.py'
    prefixed = r'\\?\C:\Users\Alice\AppData\Local\token.place\runtime\Lib\site-packages\llama_cpp\..\llama_cpp\__init__.py'
    mixed = r'c:/users/alice/appdata/local/token.place/runtime/lib/site-packages/LLAMA_CPP/__init__.py'
    assert desktop_runtime_setup.llama_module_identity_from_path(base) == desktop_runtime_setup.llama_module_identity_from_path(prefixed)
    assert desktop_runtime_setup.llama_module_identity_from_path(base) == desktop_runtime_setup.llama_module_identity_from_path(mixed)


def test_packaged_identity_fallback_matches_shared_helper_in_subprocess(tmp_path) -> None:
    resources_root = tmp_path / 'token.place desktop.app' / 'Contents' / 'Resources'
    python_dir = resources_root / 'python'
    python_dir.mkdir(parents=True)
    for name in ('desktop_runtime_setup.py', 'desktop_gpu_packaging.py'):
        source = PYTHON_MODULE_DIR / name
        (python_dir / name).write_text(source.read_text(encoding='utf-8'), encoding='utf-8')

    real = tmp_path / 'real' / 'llama_cpp' / '__init__.py'
    real.parent.mkdir(parents=True)
    real.write_text('# mock')
    link_root = tmp_path / 'link-root'
    link_root.symlink_to(real.parent.parent, target_is_directory=True)
    via_dotdot = link_root / 'llama_cpp' / '..' / 'llama_cpp' / '__init__.py'
    other = tmp_path / 'other' / 'llama_cpp' / '__init__.py'
    other.parent.mkdir(parents=True)
    other.write_text('# other')
    cases = {
        'posix_real': str(real),
        'posix_dotdot_symlink': str(via_dotdot),
        'windows_extended': r'\\?\C:\Users\Alice\AppData\Local\token.place\runtime\Lib\site-packages\llama_cpp\__init__.py',
        'windows_mixed_case': r'c:/users/alice/appdata/local/token.place/runtime/lib/site-packages/LLAMA_CPP/__init__.py',
        'other': str(other),
        'unknown': 'unknown',
        'missing': 'missing',
        'empty': '',
    }
    code = (
        'import json, desktop_runtime_setup as d; '
        f'cases = {cases!r}; '
        'print(json.dumps({k: d.llama_module_identity_from_path(v) for k, v in cases.items()} | {'
        '"valid_good": d._valid_llama_module_identity("sha256:" + "a" * 64), '
        '"valid_bad": d._valid_llama_module_identity("sha256:" + "g" * 64), '
        '"valid_non_string": d._valid_llama_module_identity(123)}))'
    )
    result = subprocess.run(
        [sys.executable, '-B', '-c', code],
        check=False,
        capture_output=True,
        text=True,
        env={'PYTHONPATH': str(python_dir), 'PATH': os.environ.get('PATH', '')},
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    fallback = json.loads(result.stdout)

    from utils.llm import llama_module_identity as shared_identity

    shared = {k: desktop_runtime_setup.llama_module_identity_from_path(v) for k, v in cases.items()}
    assert fallback == {
        **shared,
        'valid_good': 'sha256:' + 'a' * 64,
        'valid_bad': None,
        'valid_non_string': None,
    }
    assert fallback['posix_real'] == fallback['posix_dotdot_symlink']
    assert fallback['windows_extended'] == fallback['windows_mixed_case']
    assert fallback['posix_real'] != fallback['other']


def test_packaged_identity_inline_fallback_is_covered_without_utils(
    monkeypatch, tmp_path
) -> None:
    original_is_file = Path.is_file

    def packaged_helper_absent(path: Path) -> bool:
        if str(path).replace('\\', '/').endswith('/utils/llm/llama_module_identity.py'):
            return False
        return original_is_file(path)

    monkeypatch.setattr(Path, 'is_file', packaged_helper_absent)

    module_name = 'desktop_runtime_setup_inline_identity_fallback_test'
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    real = tmp_path / 'real' / 'llama_cpp' / '__init__.py'
    real.parent.mkdir(parents=True)
    real.write_text('# mock', encoding='utf-8')
    link_root = tmp_path / 'linked'
    link_root.symlink_to(real.parent.parent, target_is_directory=True)
    via_dotdot = link_root / 'llama_cpp' / '..' / 'llama_cpp' / '__init__.py'
    windows_prefixed = (
        r'\\?\C:\Users\Alice\AppData\Local\token.place\runtime\Lib'
        r'\site-packages\llama_cpp\__init__.py'
    )
    windows_mixed = (
        'c:/users/alice/appdata/local/token.place/runtime/lib/site-packages/'
        'LLAMA_CPP/__init__.py'
    )

    assert module.llama_module_identity_from_path(
        real
    ) == module.llama_module_identity_from_path(via_dotdot)
    assert module.llama_module_identity_from_path(
        windows_prefixed
    ) == module.llama_module_identity_from_path(windows_mixed)
    assert module.llama_module_identity_from_path('unknown') is None
    assert module.llama_module_identity_from_path('missing') is None
    assert module.llama_module_identity_from_path('') is None
    good = 'sha256:' + 'a' * 64
    assert module._valid_llama_module_identity(good) == good
    assert module._valid_llama_module_identity('sha256:' + 'A' * 64) is None
    assert module._valid_llama_module_identity(123) is None
