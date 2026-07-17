"""Contract tests for the Windows NVIDIA desktop smoke-test helper."""

from __future__ import annotations

import argparse
import json
import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'scripts'
    / 'windows_nvidia_gpu_smoke_test.py'
)
SPEC = importlib.util.spec_from_file_location('windows_nvidia_gpu_smoke_test', MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None, (
    f'Could not load {MODULE_PATH} — ensure desktop-tauri/scripts/ is present in the repo'
)
windows_gpu_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(windows_gpu_smoke)


def _make_repo_root_with_bootstrap(tmp_path: Path) -> Path:
    repo_root = tmp_path / 'repo'
    python_root = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    python_root.mkdir(parents=True)
    (python_root / 'path_bootstrap.py').write_text(
        'def ensure_runtime_import_paths(*_args, **_kwargs):\n'
        '    return None\n',
        encoding='utf-8',
    )
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    return repo_root


def test_main_accepts_runtime_reexec_and_cuda_bridge_started(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = _make_repo_root_with_bootstrap(tmp_path)

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='auto', context_tier='64k-full'),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)
    monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', lambda *_args: {
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 40,
        'kv_cache_device': 'cuda',
        'fallback_reason': '',
        'runtime_setup': {
            'runtime_action': 'installed_cuda_reexec',
            'interpreter': 'C:/Python312/python.exe',
        },
    })
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', lambda *_args: (
        {
            'type': 'started',
            'backend_available': 'cuda',
            'backend_used': 'cuda',
            'offloaded_layers': 32,
            'kv_cache_device': 'cuda',
            'interpreter': 'C:/Python312/python.exe',
            'llama_repo_stub_imported': False,
            'warm_load_state': 'ready',
            'registered': True,
            'context_tier': '64k-full',
        },
        [{'type': 'started'}],
        '',
    ))

    status = windows_gpu_smoke.main()
    assert status == 0


def test_main_fails_when_bridge_reports_cpu_fallback(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = _make_repo_root_with_bootstrap(tmp_path)

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='gpu', context_tier='64k-full'),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)
    monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', lambda *_args: {
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 16,
        'kv_cache_device': 'cuda',
        'fallback_reason': '',
        'runtime_setup': {
            'runtime_action': 'already_supported',
            'interpreter': 'C:/Python312/python.exe',
        },
    })
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', lambda *_args: (
        {
            'type': 'started',
            'backend_available': 'cpu',
            'backend_used': 'cpu',
            'offloaded_layers': 0,
            'kv_cache_device': 'cpu',
            'interpreter': 'C:/Python312/python.exe',
            'llama_repo_stub_imported': False,
            'warm_load_state': 'ready',
            'registered': True,
            'context_tier': '64k-full',
        },
        [{'type': 'started'}],
        '',
    ))

    status = windows_gpu_smoke.main()
    assert status == 1

class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, stdout_text: str, stderr_text: str = '') -> None:
        import io

        self.stdin = _FakeStdin()
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = None
        self.pid = 12345
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


def test_run_bridge_ignores_provisioning_started_and_cancels_after_ready(monkeypatch, tmp_path):
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    created: list[_FakeProcess] = []
    provisioning = {
        'type': 'started',
        'running': True,
        'registered': False,
        'worker_state': 'provisioning',
        'backend_available': 'pending',
        'backend_used': 'pending',
        'context_tier': '64k-full',
    }
    ready = {
        'type': 'started',
        'running': True,
        'registered': True,
        'worker_state': 'ready',
        'warm_load_state': 'ready',
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 40,
        'kv_cache_device': 'cuda',
        'llama_repo_stub_imported': False,
        'context_tier': '64k-full',
    }

    def fake_popen(*_args, **_kwargs):
        proc = _FakeProcess('\n'.join([json.dumps(provisioning), json.dumps(ready), '']))
        created.append(proc)
        return proc

    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', fake_popen)

    started, events, _stderr = windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full')

    assert started == ready
    assert events == [provisioning, ready]
    assert created[0].stdin.writes == ['{"type":"cancel"}\n']
    assert created[0].stdin.closed is True


def test_run_bridge_fails_and_reaps_on_provisioning_then_error(monkeypatch, tmp_path):
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    created: list[_FakeProcess] = []
    provisioning = {'type': 'started', 'worker_state': 'provisioning', 'backend_available': 'pending'}
    error = {'type': 'error', 'message': 'boom'}

    def fake_popen(*_args, **_kwargs):
        proc = _FakeProcess('\n'.join([json.dumps(provisioning), json.dumps(error), '']))
        created.append(proc)
        return proc

    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(windows_gpu_smoke, '_terminate_process_tree', lambda proc: proc.terminate())

    try:
        windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full')
    except RuntimeError as exc:
        assert 'bridge validation failed' in str(exc)
    else:
        raise AssertionError('expected bridge validation failure')
    assert created[0].terminated is True
    assert created[0].stdin.writes == []


def test_main_forwards_context_tier_to_runtime_and_bridge(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    calls: dict[str, tuple[str, str, str] | tuple[str, str, str]] = {}

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='gpu', context_tier='64k-full'),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)

    def fake_diagnostics(model, mode, context_tier):
        calls['diagnostics'] = (model, mode, context_tier)
        return {
            'backend_available': 'cuda',
            'backend_used': 'cuda',
            'offloaded_layers': 16,
            'kv_cache_device': 'cuda',
            'runtime_setup': {'runtime_action': 'already_supported', 'interpreter': 'python'},
        }

    def fake_bridge(model, mode, context_tier):
        calls['bridge'] = (model, mode, context_tier)
        return ({
            'type': 'started',
            'registered': True,
            'warm_load_state': 'ready',
            'backend_available': 'cuda',
            'backend_used': 'cuda',
            'offloaded_layers': 16,
            'kv_cache_device': 'cuda',
            'llama_repo_stub_imported': False,
            'context_tier': context_tier,
            'interpreter': 'python',
        }, [], '')

    monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', fake_diagnostics)
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', fake_bridge)

    assert windows_gpu_smoke.main() == 0
    assert calls['diagnostics'] == (str(model_path), 'gpu', '64k-full')
    assert calls['bridge'] == (str(model_path), 'gpu', '64k-full')
