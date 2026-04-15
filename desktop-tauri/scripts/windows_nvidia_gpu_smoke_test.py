#!/usr/bin/env python3
"""Manual Windows + NVIDIA smoke test for desktop sidecar GPU runtime viability."""

from __future__ import annotations

import argparse
import json
import os
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
    for entry in (str(repo_root), str(python_root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)

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

        _require(payload['backend_available'] == 'cuda', 'backend_available is not cuda')
        _require(payload['backend_used'] == 'cuda', 'backend_used is not cuda')
        _require(int(payload.get('offloaded_layers') or 0) > 0, 'offloaded_layers must be > 0')
        kv_cache = str(payload.get('kv_cache_device') or '').lower()
        _require(kv_cache not in {'', 'cpu'}, 'kv_cache_device indicates CPU-only execution')
        return 0
    except Exception as exc:
        return _fail(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
