#!/usr/bin/env python3
"""Manual Windows + NVIDIA smoke test for desktop sidecar GPU runtime viability."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _fail(message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _offloaded_layer_count(value: Any) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == 'all_supported_layers':
            return 1
    return int(value or 0)


def _load_compute_runtime_diagnostics(model_path: str, mode: str) -> dict[str, Any]:
    from desktop_runtime_setup import ensure_desktop_llama_runtime
    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import get_model_manager

    runtime_setup = ensure_desktop_llama_runtime(mode)

    manager = get_model_manager()
    manager.model_path = model_path
    apply_compute_mode(manager, mode)
    manager.get_llm_instance()
    diagnostics = compute_mode_diagnostics(manager)
    diagnostics["runtime_setup"] = runtime_setup
    return diagnostics


def _is_repo_llama_shim(module_path: str, repo_root: Path) -> bool:
    if not module_path:
        return False
    return str(Path(module_path).resolve()).lower() == str((repo_root / 'llama_cpp.py').resolve()).lower()


def _run_bridge_oneshot(model_path: str, mode: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    repo_root = _repo_root()
    python_root = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    bridge_path = python_root / 'compute_node_bridge.py'
    env = os.environ.copy()
    existing_pythonpath = env.get('PYTHONPATH', '')
    entries = [str(repo_root), str(python_root)]
    if existing_pythonpath:
        entries.append(existing_pythonpath)
    env['PYTHONPATH'] = os.pathsep.join(entries)

    completed = subprocess.run(
        [
            sys.executable,
            str(bridge_path),
            '--model',
            model_path,
            '--mode',
            mode,
            '--relay-url',
            'https://token.place',
        ],
        input='{"type":"cancel"}\n',
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(repo_root),
        env=env,
        check=False,
    )
    events = [
        json.loads(line)
        for line in (completed.stdout or '').splitlines()
        if line.strip().startswith('{')
    ]
    started = next((event for event in events if event.get('type') == 'started'), {})
    return started, events, (completed.stderr or '').strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Validate desktop sidecar GPU path on a Windows NVIDIA machine'
    )
    parser.add_argument('--model', required=True, help='Path to GGUF model used by desktop sidecars')
    parser.add_argument('--mode', default='auto', choices=['auto', 'gpu', 'hybrid'])
    args = parser.parse_args()

    if not sys.platform.startswith('win'):
        return _fail('this smoke test is only valid on Windows')
    if not os.path.exists(args.model):
        return _fail(f'model path does not exist: {args.model}')

    repo_root = _repo_root()
    python_root = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    if str(python_root) not in sys.path:
        sys.path.insert(0, str(python_root))
    from path_bootstrap import ensure_runtime_import_paths

    ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)

    try:
        diagnostics = _load_compute_runtime_diagnostics(args.model, args.mode)
        runtime_setup = diagnostics.get('runtime_setup', {})
        payload = {
            'interpreter': runtime_setup.get('interpreter', sys.executable),
            'llama_module_path': runtime_setup.get('llama_module_path', 'missing'),
            'backend_available': diagnostics.get('backend_available'),
            'backend_used': diagnostics.get('backend_used'),
            'offloaded_layers': diagnostics.get('offloaded_layers'),
            'kv_cache_device': diagnostics.get('kv_cache_device'),
            'fallback_reason': diagnostics.get('fallback_reason'),
        }
        print(json.dumps(payload, indent=2))

        _require(
            not _is_repo_llama_shim(str(payload.get('llama_module_path') or ''), repo_root),
            'llama_module_path resolved to repo-local llama_cpp.py shim; expected site-packages package',
        )
        _require(str(payload.get('interpreter') or '').strip() != '', 'interpreter is missing')
        _require(
            runtime_setup.get('runtime_action') != 'shadowed_repo_llama_cpp',
            'runtime_action reported shadowed_repo_llama_cpp',
        )
        _require(payload['backend_available'] == 'cuda', 'backend_available is not cuda')
        _require(payload['backend_used'] == 'cuda', 'backend_used is not cuda')
        _require(_offloaded_layer_count(payload.get('offloaded_layers')) > 0, 'offloaded_layers must be > 0')
        kv_cache = str(payload.get('kv_cache_device') or '').lower()
        _require(kv_cache not in {'', 'cpu'}, 'kv_cache_device indicates CPU-only execution')

        started, bridge_events, bridge_stderr = _run_bridge_oneshot(args.model, args.mode)
        print(
            json.dumps(
                {
                    'bridge_started': started,
                    'bridge_event_count': len(bridge_events),
                    'bridge_stderr_tail': bridge_stderr[-240:],
                },
                indent=2,
            )
        )
        _require(bool(started), 'compute_node_bridge.py did not emit a started event')
        _require(
            not _is_repo_llama_shim(str(started.get('llama_module_path') or ''), repo_root),
            'bridge started.llama_module_path resolved to repo-local llama_cpp.py shim',
        )
        _require(str(started.get('interpreter') or '').strip() != '', 'bridge started.interpreter is missing')
        _require(started.get('backend_available') == 'cuda', 'bridge started.backend_available is not cuda')
        _require(started.get('backend_used') == 'cuda', 'bridge started.backend_used is not cuda')
        _require(
            _offloaded_layer_count(started.get('offloaded_layers')) > 0,
            'bridge started.offloaded_layers must be > 0',
        )
        bridge_kv_cache = str(started.get('kv_cache_device') or '').lower()
        _require(
            bridge_kv_cache not in {'', 'cpu'},
            'bridge started.kv_cache_device indicates CPU-only execution',
        )
        return 0
    except Exception as exc:
        return _fail(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
