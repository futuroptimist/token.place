import importlib.util
import json
import os
import sys
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _default_desktop_arch(monkeypatch):
    """Keep win32 platform simulations independent from the host CPU architecture."""

    monkeypatch.setattr(desktop_runtime_setup.platform_module, 'machine', lambda: 'AMD64')


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
    assert recorded['llama_module_path'] == result['llama_module_path']


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
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=REPO_ROOT)

    assert result['selected_backend'] == 'metal'
    assert result['runtime_action'] == 'installed_metal_reexec'
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
    assert result['llama_module_path'].endswith('llama_cpp/__init__.py')
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
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_install)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=runtime_root)

    assert result['runtime_action'] == 'installed_metal_reexec'
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
    assert 'llama_module_path=' in result['fallback_reason']
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

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['cwd'] = kwargs['cwd']
        captured['env'] = kwargs['env']
        return _Result()

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

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
        called['guard'] = env.get(desktop_runtime_setup.REEXEC_GUARD_ENV)

    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', fake_execve)
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})

    assert called['prog'] == sys.executable
    assert called['guard'] == '1'


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

    def fake_run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)

    ok, _ = desktop_runtime_setup._windows_cuda_source_repair(requirements_path, dependency_target)

    assert ok is True
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

    def fake_run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
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

    def fake_run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
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

    def fake_run(cmd, **kwargs):
        captured['env'] = kwargs['env']
        return _Result()

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

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

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _Result())

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

    def _raise(*_args, **_kwargs):
        raise RuntimeError('subprocess unavailable')

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', _raise)

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'subprocess unavailable'


def test_probe_uses_return_code_when_stderr_is_empty(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 9
        stdout = ''
        stderr = ''

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _Result())

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

    def _fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = kwargs.get('env', {})
        return _Result()

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', _fake_run)
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

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _Result())

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


def test_run_pip_install_success_failure_and_timeout(monkeypatch):
    class _OkResult:
        returncode = 0
        stdout = 'ok output'
        stderr = ''

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _OkResult())
    ok, output = desktop_runtime_setup._run_pip_install(['python'], {})
    assert ok is True
    assert 'returncode=0' in output
    assert 'stdout_tail=ok output' in output

    class _FailResult:
        returncode = 1
        stdout = 'fallback stdout'
        stderr = 'real stderr'

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _FailResult())
    ok, output = desktop_runtime_setup._run_pip_install(['python'], {})
    assert ok is False
    assert 'returncode=1' in output
    assert 'stdout_tail=fallback stdout' in output
    assert 'stderr_tail=real stderr' in output

    def _timeout(*_args, **_kwargs):
        raise desktop_runtime_setup.subprocess.TimeoutExpired(cmd='pip', timeout=12)

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', _timeout)
    ok, output = desktop_runtime_setup._run_pip_install(['python'], {}, timeout_seconds=12)
    assert ok is False
    assert 'pip install timed out after 12s' in output
    assert 'stdout_tail=empty' in output
    assert 'stderr_tail=empty' in output


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

    desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert captured['cmd'][:6] == [
        sys.executable,
        '-m',
        'pip',
        'install',
        '--disable-pip-version-check',
        '--force-reinstall',
    ]
    assert '--target' in captured['cmd']


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
        return False, 'install failed: boom'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', _capture_run)

    result = desktop_runtime_setup.ensure_desktop_python_dependencies(repo_root=tmp_path)

    assert result['ok'] == 'false'
    assert result['action'] == 'install_failed'
    assert result['detail'] == 'install failed: boom'
    assert '--target' in captured['cmd']
    target_idx = captured['cmd'].index('--target') + 1
    assert captured['cmd'][target_idx] == str(tmp_path / '.token_place_desktop_site')


def test_ensure_desktop_python_dependencies_reports_post_install_missing(monkeypatch, tmp_path):
    requirements = tmp_path / 'requirements_desktop_runtime.txt'
    requirements.write_text('psutil\nrequests\npython-dotenv\ncryptography\n', encoding='utf-8')
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_runtime_root', lambda **_: tmp_path)
    monkeypatch.setattr(desktop_runtime_setup, '_resolve_desktop_requirements_path', lambda _root: requirements)
    sequence = iter([None, None, None, None, object(), object(), object(), None])
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


def test_resolve_desktop_dependency_target_prefers_writable_runtime_target(monkeypatch, tmp_path):
    runtime_root = tmp_path / 'runtime'
    home_dir = tmp_path / 'home'
    home_dir.mkdir()
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

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'


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

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['cmake_args'] == '-DGGML_CUDA=on'
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_CUDA=on'
    assert '-DGGML_METAL=on' not in ' '.join(captured['cmd'])
    assert '--target' in captured['cmd']
    assert captured['cmd'][captured['cmd'].index('--target') + 1] == str(dependency_target)
    assert result['install_command_summary'].startswith('python -m pip install')


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
    assert 'module=C:/Python/Lib/site-packages/llama_cpp/__init__.py' in result['fallback_reason']
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

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *_, **__: Result())

    probe = desktop_runtime_setup._probe_llama_runtime(runtime_root=tmp_path)

    assert probe.constructor_kwarg_support == {'rope_scaling_type': True}
