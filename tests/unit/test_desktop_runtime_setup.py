import importlib.util
import json
import sys
from pathlib import Path

PYTHON_MODULE_DIR = (
    Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python'
)
if str(PYTHON_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_MODULE_DIR))

MODULE_PATH = (
    PYTHON_MODULE_DIR / 'desktop_runtime_setup.py'
)
SPEC = importlib.util.spec_from_file_location('desktop_runtime_setup', MODULE_PATH)
desktop_runtime_setup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
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


def test_runtime_bootstrap_uses_existing_gpu_runtime(monkeypatch):
    probe = {'count': 0}

    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ['-c', cmd[2]]:
            probe['count'] += 1
            return _Result(
                stdout=json.dumps(
                    {
                        'backend': 'cuda',
                        'gpu_offload_supported': True,
                        'detected_device': 'cuda',
                        'error': None,
                    }
                )
            )
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'
    assert probe['count'] == 1


def test_runtime_bootstrap_falls_back_to_cpu_after_gpu_attempt(monkeypatch):
    state = {'probe_calls': 0, 'pip_calls': []}
    Plan = desktop_runtime_setup.LlamaCppInstallPlan

    def fake_plans(**_kwargs):
        return [
            Plan(
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
            Plan(
                platform='win32',
                backend='cpu',
                package_spec='llama-cpp-python',
                cmake_args=None,
                force_cmake=False,
                index_url='https://pypi.org/simple',
                extra_index_url=None,
                only_binary=True,
                no_binary=False,
            ),
        ]

    def fake_run(cmd, **kwargs):
        if cmd[1] == '-c':
            state['probe_calls'] += 1
            payload = {
                'backend': 'cpu',
                'gpu_offload_supported': False,
                'detected_device': 'cpu',
                'error': None,
            }
            return _Result(stdout=json.dumps(payload))

        state['pip_calls'].append(cmd)
        return _Result(returncode=0)

    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', fake_plans)
    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', fake_run)

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())
    assert result['selected_backend'] == 'cpu'
    assert result['runtime_action'] == 'installed_cpu_fallback'
    assert 'GPU runtime unavailable' in result['fallback_reason']
    assert len(state['pip_calls']) == 2
