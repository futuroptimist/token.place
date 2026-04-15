import importlib.util
import sys
from pathlib import Path


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


class _Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_skip_runtime_bootstrap_for_cpu_mode():
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('cpu')
    assert result['runtime_action'] == 'skipped'
    assert result['selected_backend'] == 'cpu'


def test_windows_auto_mode_attempts_runtime_repair_by_default(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='missing',
        error=None,
    )
    pip_calls = []
    probe_calls = {'count': 0}

    def fake_probe():
        probe_calls['count'] += 1
        if probe_calls['count'] > 1:
            return desktop_runtime_setup.RuntimeProbe(
                backend='cuda',
                gpu_offload_supported=True,
                detected_device='nvidia',
                python_executable=sys.executable,
                python_prefix=sys.prefix,
                llama_cpp_path='site-packages/llama_cpp/__init__.py',
                error=None,
            )
        return probe

    def fake_run(cmd, **_kwargs):
        pip_calls.append(cmd)
        return _Result(returncode=0)

    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', fake_probe)
    monkeypatch.setattr(
        desktop_runtime_setup,
        'llama_cpp_install_plan_fallbacks',
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        desktop_runtime_setup,
        'sys',
        type('S', (), {'platform': 'win32', 'executable': sys.executable, 'prefix': sys.prefix}),
    )
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    assert result['runtime_action'] == 'installed_cuda_active'
    assert result['selected_backend'] == 'cuda'
    assert pip_calls
    assert pip_calls[0][0:4] == [sys.executable, '-m', 'pip', 'install']
    assert '--force-reinstall' in pip_calls[0]
    assert '--verbose' in pip_calls[0]


def test_runtime_bootstrap_returns_reexec_required_when_repair_needs_restart(monkeypatch):
    state = {'pip_calls': []}
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='missing',
        error='cpu-only runtime',
    )
    plan = desktop_runtime_setup.LlamaCppInstallPlan(
        platform='win32',
        backend='cuda',
        package_spec='llama-cpp-python',
        cmake_args=None,
        force_cmake=False,
        index_url='https://example.invalid/cuda',
        extra_index_url=None,
        only_binary=True,
        no_binary=False,
    )

    probe_calls = {'count': 0}

    def fake_probe():
        probe_calls['count'] += 1
        return probe

    def fake_run(cmd, **_kwargs):
        state['pip_calls'].append(cmd)
        return _Result(returncode=0)

    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', fake_probe)
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [plan])
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)
    monkeypatch.setattr(
        desktop_runtime_setup,
        'sys',
        type('S', (), {'platform': 'linux', 'executable': sys.executable, 'prefix': sys.prefix}),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())
    assert result['runtime_action'] == 'installed_cuda_reexec_required'
    assert result['selected_backend'] == 'cuda'
    assert 'restarting sidecar' in result['fallback_reason']
    assert len(state['pip_calls']) == 1


def test_runtime_bootstrap_returns_already_supported_when_gpu_runtime_is_present(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cuda',
        gpu_offload_supported=True,
        detected_device='nvidia',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='site-packages/llama_cpp/__init__.py',
        error=None,
    )
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu')

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'
    assert result['detected_device'] == 'nvidia'


def test_runtime_bootstrap_can_be_disabled_explicitly(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='missing',
        error='cpu runtime',
    )
    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '0')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert result['runtime_action'] == 'bootstrap_disabled'
    assert result['selected_backend'] == 'cpu'
    assert desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV in result['fallback_reason']


def test_runtime_bootstrap_uses_unpinned_fallback_plans_when_requirements_missing(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='missing',
        error='probe says cpu',
    )
    calls = []

    def fake_plan_fallbacks(**_kwargs):
        raise FileNotFoundError('requirements.txt missing')

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(returncode=0)

    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', fake_plan_fallbacks)
    monkeypatch.setattr(
        desktop_runtime_setup,
        'sys',
        type('S', (), {'platform': 'linux', 'executable': sys.executable, 'prefix': sys.prefix}),
    )
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'installed_cpu_fallback'
    assert result['selected_backend'] == 'cpu'
    assert calls


def test_runtime_bootstrap_reports_timeout_and_install_errors(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='missing',
        error=None,
    )
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cuda',
            package_spec='llama-cpp-python',
            cmake_args=None,
            force_cmake=False,
            index_url='https://example.invalid/cuda',
            extra_index_url=None,
            only_binary=True,
            no_binary=False,
        ),
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cpu',
            package_spec='llama-cpp-python',
            cmake_args=None,
            force_cmake=False,
            index_url='https://example.invalid/cpu',
            extra_index_url=None,
            only_binary=True,
            no_binary=False,
        ),
    ]
    state = {'calls': 0}

    def fake_run(_cmd, **_kwargs):
        state['calls'] += 1
        if state['calls'] == 1:
            raise desktop_runtime_setup.subprocess.TimeoutExpired(cmd='pip', timeout=1)
        return _Result(returncode=1, stderr='wheel not found')

    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert result['selected_backend'] == 'cpu'
    assert result['fallback_reason'] == 'wheel not found'


def test_fallback_unpinned_plans_cover_win_darwin_and_other_platforms():
    win_plans = desktop_runtime_setup._fallback_unpinned_plans('win32')
    darwin_plans = desktop_runtime_setup._fallback_unpinned_plans('darwin')
    linux_plans = desktop_runtime_setup._fallback_unpinned_plans('linux')

    assert [plan.backend for plan in win_plans] == ['cuda', 'cpu']
    assert [plan.backend for plan in darwin_plans] == ['metal', 'metal']
    assert [plan.backend for plan in linux_plans] == ['cpu']


def test_windows_prioritized_plans_include_source_repair_first():
    source_and_fallbacks = desktop_runtime_setup._prioritized_repair_plans([], 'win32')

    source = source_and_fallbacks[0]
    assert source.backend == 'cuda'
    assert source.force_cmake is True
    assert source.cmake_args == '-DGGML_CUDA=on'
    assert '--force-reinstall' in source.pip_install_args()
    assert '--verbose' in source.pip_install_args()
