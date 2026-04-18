"""Contract tests for the Windows NVIDIA desktop smoke-test helper."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'scripts'
    / 'windows_nvidia_gpu_smoke_test.py'
)
SPEC = importlib.util.spec_from_file_location('windows_nvidia_gpu_smoke_test', MODULE_PATH)
windows_gpu_smoke = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(windows_gpu_smoke)


def test_main_accepts_runtime_reexec_and_cuda_bridge_started(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='auto'),
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
            'llama_module_path': 'C:/Python312/Lib/site-packages/llama_cpp/__init__.py',
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
            'llama_module_path': 'C:/Python312/Lib/site-packages/llama_cpp/__init__.py',
        },
        [{'type': 'started'}],
        '',
    ))

    status = windows_gpu_smoke.main()
    assert status == 0


def test_main_fails_when_bridge_reports_cpu_fallback(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='gpu'),
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
            'llama_module_path': 'C:/Python312/Lib/site-packages/llama_cpp/__init__.py',
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
            'llama_module_path': 'C:/Python312/Lib/site-packages/llama_cpp/__init__.py',
        },
        [{'type': 'started'}],
        '',
    ))

    status = windows_gpu_smoke.main()
    assert status == 1
