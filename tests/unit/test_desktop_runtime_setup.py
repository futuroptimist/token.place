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


def test_windows_auto_runtime_repair_attempts_install_by_default(monkeypatch):
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

    def fake_run(*_args, **_kwargs):
        pip_calls.append(True)
        return _Result(returncode=0)

    monkeypatch.setattr(desktop_runtime_setup.sys, 'platform', 'win32')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)
    monkeypatch.setattr(
        desktop_runtime_setup,
        'llama_cpp_install_plan_fallbacks',
        lambda **_kwargs: [
            desktop_runtime_setup.LlamaCppInstallPlan(
                platform='win32',
                backend='cpu',
                package_spec='llama-cpp-python',
                cmake_args=None,
                force_cmake=False,
                index_url='https://pypi.org/simple',
                extra_index_url=None,
                only_binary=True,
                no_binary=False,
            )
        ],
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    assert result['runtime_action'] == 'installed_cpu_fallback'
    assert result['selected_backend'] == 'cpu'
    assert len(pip_calls) >= 1


def test_runtime_bootstrap_installs_cuda_without_restart_limbo(monkeypatch):
    state = {'pip_calls': []}
    probes = iter([
        desktop_runtime_setup.RuntimeProbe(
            backend='cpu',
            gpu_offload_supported=False,
            detected_device='cpu',
            python_executable=sys.executable,
            python_prefix=sys.prefix,
            llama_cpp_path='missing',
            error='cpu-only runtime',
        ),
        desktop_runtime_setup.RuntimeProbe(
            backend='cuda',
            gpu_offload_supported=True,
            detected_device='cuda',
            python_executable=sys.executable,
            python_prefix=sys.prefix,
            llama_cpp_path='/tmp/site-packages/llama_cpp/__init__.py',
            error=None,
        ),
    ])
    plan = desktop_runtime_setup.LlamaCppInstallPlan(
        platform='win32',
        backend='cuda',
        package_spec='llama-cpp-python',
        cmake_args='-DGGML_CUDA=on',
        force_cmake=True,
        index_url=None,
        extra_index_url=None,
        only_binary=False,
        no_binary=True,
    )

    def fake_run(cmd, **_kwargs):
        state['pip_calls'].append(cmd)
        return _Result(returncode=0)

    monkeypatch.setattr(desktop_runtime_setup.sys, 'platform', 'win32')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: next(probes))
    monkeypatch.setattr(desktop_runtime_setup, '_windows_source_repair_plans', lambda _path: [plan])
    monkeypatch.setattr(
        desktop_runtime_setup,
        'llama_cpp_install_plan_fallbacks',
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())
    assert result['runtime_action'] == 'installed_cuda'
    assert result['selected_backend'] == 'cuda'
    assert result['fallback_reason'] == ''
    assert result['llama_cpp_path'].endswith('__init__.py')
    assert len(state['pip_calls']) == 1


def test_runtime_bootstrap_returns_already_supported_when_gpu_runtime_is_present(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cuda',
        gpu_offload_supported=True,
        detected_device='nvidia',
        python_executable=sys.executable,
        python_prefix=sys.prefix,
        llama_cpp_path='/tmp/llama_cpp.py',
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
        error='cpu-only runtime',
    )
    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    assert result['runtime_action'] == 'auto_repair_disabled'
    assert result['selected_backend'] == 'cpu'


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

    monkeypatch.setattr(desktop_runtime_setup.sys, 'platform', 'linux')
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


def test_runtime_probe_uses_same_interpreter_for_probe(monkeypatch):
    class _ProbeResult:
        def __init__(self):
            self.returncode = 0
            self.stdout = (
                '{"backend":"cpu","gpu_offload_supported":false,"detected_device":"cpu",'
                f'"python_executable":"{sys.executable}","python_prefix":"{sys.prefix}",'
                '"llama_cpp_path":"missing","error":null}'
            )
            self.stderr = ''

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _ProbeResult()

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)
    probe = desktop_runtime_setup._probe_llama_runtime()

    assert calls
    assert calls[0][0] == sys.executable
    assert probe.python_executable == sys.executable
