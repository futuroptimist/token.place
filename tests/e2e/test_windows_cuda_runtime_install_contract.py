"""E2E-style regression for Windows CUDA runtime install fallback order."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DESKTOP_PYTHON = ROOT / 'desktop-tauri' / 'src-tauri' / 'python'
if str(DESKTOP_PYTHON) not in sys.path:
    sys.path.insert(0, str(DESKTOP_PYTHON))

MODULE_PATH = DESKTOP_PYTHON / 'desktop_runtime_setup.py'
SPEC = importlib.util.spec_from_file_location('desktop_runtime_setup_e2e', MODULE_PATH)
assert SPEC and SPEC.loader
runtime_setup = importlib.util.module_from_spec(SPEC)
sys.modules['desktop_runtime_setup_e2e'] = runtime_setup
SPEC.loader.exec_module(runtime_setup)


class _SysStub:
    platform = 'win32'
    executable = sys.executable
    prefix = sys.prefix
    argv = ['python']


def _probe(*, backend='cpu', gpu=False, device='cpu', error=None):
    return runtime_setup.RuntimeProbe(
        backend=backend,
        gpu_offload_supported=gpu,
        detected_device=device,
        interpreter=sys.executable,
        prefix=sys.prefix,
        llama_module_path='C:/Python/Lib/site-packages/llama_cpp/__init__.py',
        error=error,
    )


def test_windows_runtime_install_walks_cuda_channels_before_cpu(monkeypatch):
    monkeypatch.setattr(runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(runtime_setup, '_should_attempt_source_repair', lambda: (False, 'skip'))
    monkeypatch.setattr(
        runtime_setup,
        'llama_cpp_install_plan_fallbacks',
        lambda **kwargs: runtime_setup._fallback_unpinned_plans(kwargs['platform']),
    )

    probes = iter([_probe(), _probe(backend='cuda', gpu=True, device='cuda')])
    monkeypatch.setattr(runtime_setup, '_probe_llama_runtime', lambda: next(probes))

    attempted_indexes: list[str] = []

    def fake_run(cmd, _env, **_kwargs):
        package_spec = cmd[-1]
        if package_spec != 'llama-cpp-python':
            return False, 'pinned unavailable in packaged layout'

        if '--index-url' in cmd:
            idx = cmd[cmd.index('--index-url') + 1]
            attempted_indexes.append(idx)
            if idx == runtime_setup.CUDA_WHEEL_INDEXES[1]:
                return True, 'installed cuda wheel from secondary channel'

        return False, 'no matching distribution found'

    monkeypatch.setattr(runtime_setup, '_run_pip_install', fake_run)

    result = runtime_setup.ensure_desktop_llama_runtime('gpu', repo_root=ROOT)

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['selected_backend'] == 'cuda'
    assert attempted_indexes[:2] == list(runtime_setup.CUDA_WHEEL_INDEXES[:2])
