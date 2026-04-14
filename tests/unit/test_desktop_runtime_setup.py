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


def test_probe_only_runtime_path_does_not_install_without_explicit_flag(monkeypatch):
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
        error=None,
    )
    pip_calls = []

    def fake_run(*_args, **_kwargs):
        pip_calls.append(True)
        return _Result(returncode=0)

    monkeypatch.delenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    assert result['runtime_action'] == 'probe_only'
    assert result['selected_backend'] == 'cpu'
    assert desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV in result['fallback_reason']
    assert pip_calls == []


def test_runtime_bootstrap_explicitly_enabled_installs_and_requires_restart(monkeypatch):
    state = {'pip_calls': []}
    probe = desktop_runtime_setup.RuntimeProbe(
        backend='cpu',
        gpu_offload_supported=False,
        detected_device='cpu',
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

    def fake_run(cmd, **_kwargs):
        state['pip_calls'].append(cmd)
        return _Result(returncode=0)

    monkeypatch.setenv(desktop_runtime_setup.ENABLE_BOOTSTRAP_ENV, '1')
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: probe)
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [plan])
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())
    assert result['runtime_action'] == 'installed_cuda_restart_required'
    assert result['selected_backend'] == 'cuda'
    assert 'restart sidecar' in result['fallback_reason']
    assert len(state['pip_calls']) == 1
