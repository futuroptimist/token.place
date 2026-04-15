#!/usr/bin/env python3
"""Manual Windows/NVIDIA smoke test for desktop operator GPU startup."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Verify desktop operator startup uses CUDA on Windows/NVIDIA.'
    )
    parser.add_argument('--model', required=True, help='Path to local GGUF model file.')
    parser.add_argument('--relay-url', default='https://token.place')
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    python_dir = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(python_dir) not in sys.path:
        sys.path.insert(0, str(python_dir))

    from desktop_runtime_setup import ensure_desktop_llama_runtime
    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import detect_llama_runtime_capabilities, get_model_manager

    runtime_setup = ensure_desktop_llama_runtime('auto', repo_root=repo_root)
    runtime_probe = detect_llama_runtime_capabilities()

    _assert(sys.platform.startswith('win'), 'This smoke test is intended for Windows hosts.')
    _assert(os.path.exists(args.model), f'Model file not found: {args.model}')
    _assert(
        runtime_probe.get('llama_module_path') not in (None, '', 'missing'),
        f"llama_cpp module path missing ({runtime_probe.get('llama_module_path')})",
    )

    manager = get_model_manager()
    manager.model_path = args.model
    apply_compute_mode(manager, 'auto')
    manager.get_llm_instance()
    diagnostics = compute_mode_diagnostics(manager)

    _assert(
        diagnostics.get('backend_available') == 'cuda',
        f"backend_available must be cuda, got {diagnostics.get('backend_available')}",
    )
    _assert(
        diagnostics.get('backend_used') == 'cuda',
        f"backend_used must be cuda, got {diagnostics.get('backend_used')}",
    )
    offloaded_layers = diagnostics.get('offloaded_layers', diagnostics.get('n_gpu_layers', 0))
    _assert(
        isinstance(offloaded_layers, int) and offloaded_layers != 0,
        f'offloaded_layers must be non-zero, got {offloaded_layers}',
    )
    kv_cache_device = str(diagnostics.get('kv_cache_device', '')).lower()
    _assert(
        kv_cache_device not in {'cpu', 'host', 'none', ''},
        f'kv_cache_device must not be CPU-only, got {diagnostics.get("kv_cache_device")}',
    )

    bridge_cmd = [
        sys.executable,
        str(python_dir / 'compute_node_bridge.py'),
        '--model',
        args.model,
        '--mode',
        'auto',
        '--relay-url',
        args.relay_url,
    ]
    env = os.environ.copy()
    env['TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP'] = '1'
    proc = subprocess.Popen(
        bridge_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(repo_root),
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc.stdin.write('{"type":"cancel"}\n')
    proc.stdin.flush()
    proc.stdin.close()
    stdout, stderr = proc.communicate(timeout=45)
    _assert(proc.returncode == 0, f'compute_node_bridge exited {proc.returncode}: {stderr}')

    started = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get('type') == 'started':
            started = payload
            break

    _assert(started is not None, 'compute_node_bridge did not emit a started event')
    _assert(
        started.get('backend_available') == 'cuda',
        f"bridge started.backend_available must be cuda, got {started.get('backend_available')}",
    )
    _assert(
        started.get('backend_used') == 'cuda',
        f"bridge started.backend_used must be cuda, got {started.get('backend_used')}",
    )
    _assert(
        str(started.get('kv_cache_device', '')).lower() not in {'cpu', 'host', 'none', ''},
        f"bridge started.kv_cache_device must not be CPU-only, got {started.get('kv_cache_device')}",
    )

    result = {
        'runtime_setup': runtime_setup,
        'runtime_probe': runtime_probe,
        'compute_mode_diagnostics': diagnostics,
        'bridge_started_event': started,
        'interpreter': sys.executable,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
