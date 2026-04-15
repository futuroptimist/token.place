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


def test_windows_auto_mode_repairs_cpu_runtime_without_opt_in_flag(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        error='cpu-only runtime',
    )
    pip_calls = []

    def fake_run(cmd, **_kwargs):
        pip_calls.append(cmd)
        return _Result(returncode=0, stdout='ok')

    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.delenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(desktop_runtime_setup.sys, 'platform', 'win32')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime_from_subprocess',
        lambda _exe: desktop_runtime_setup.RuntimeProbe(
            backend='cuda', gpu_offload_supported=True, detected_device='nvidia', error=None
        ),
    )
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'installed_cuda_restart_required'
    assert result['selected_backend'] == 'cuda'
    assert len(pip_calls) == 1
    joined = ' '.join(pip_calls[0])
    assert '--force-reinstall' in joined
    assert '--verbose' in joined


def test_runtime_bootstrap_returns_already_supported_when_gpu_runtime_is_present(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cuda',
        gpu_offload_supported=True,
        detected_device='nvidia',
        error=None,
    )
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu')

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'
    assert result['detected_device'] == 'nvidia'


def test_runtime_bootstrap_respects_disable_flag(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        error='missing CUDA',
    )
    pip_calls = []

    def fake_run(*_args, **_kwargs):
        pip_calls.append(True)
        return _Result(returncode=0)

    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(desktop_runtime_setup.sys, 'platform', 'win32')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    assert result['runtime_action'] == 'probe_only'
    assert pip_calls == []


def test_runtime_bootstrap_reports_timeout_and_install_errors(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        error=None,
    )
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cuda',
            package_spec='llama-cpp-python',
            cmake_args='-DGGML_CUDA=on',
            force_cmake=True,
            index_url=None,
            extra_index_url=None,
            only_binary=False,
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

    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup, '_preferred_install_plans', lambda **_kwargs: plans)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert result['selected_backend'] == 'cpu'
    assert result['fallback_reason'] == 'wheel not found'


def test_runtime_bootstrap_uses_same_interpreter_for_pip(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        error='cpu runtime',
    )
    plan = desktop_runtime_setup.LlamaCppInstallPlan(
        platform='win32',
        backend='cuda',
        package_spec='llama-cpp-python',
        cmake_args='-DGGML_CUDA=on',
        force_cmake=True,
        index_url=None,
        extra_index_url=None,
        only_binary=False,
        no_binary=False,
    )
    call_cmds = []

    def fake_run(cmd, **_kwargs):
        call_cmds.append(cmd)
        return _Result(returncode=0)

    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup, '_preferred_install_plans', lambda **_kwargs: [plan])
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime_from_subprocess',
        lambda _exe: desktop_runtime_setup.RuntimeProbe(
            backend='cuda', gpu_offload_supported=True, detected_device='nvidia', error=None
        ),
    )
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'installed_cuda_restart_required'
    assert call_cmds
    assert call_cmds[0][0] == desktop_runtime_setup.sys.executable


def test_fallback_unpinned_plans_cover_win_darwin_and_other_platforms():
    win_plans = desktop_runtime_setup._fallback_unpinned_plans('win32')
    darwin_plans = desktop_runtime_setup._fallback_unpinned_plans('darwin')
    linux_plans = desktop_runtime_setup._fallback_unpinned_plans('linux')

    assert [plan.backend for plan in win_plans] == ['cuda', 'cpu']
    assert [plan.backend for plan in darwin_plans] == ['metal', 'metal']
    assert [plan.backend for plan in linux_plans] == ['cpu']
