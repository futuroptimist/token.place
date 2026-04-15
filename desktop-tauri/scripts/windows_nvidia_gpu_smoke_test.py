#!/usr/bin/env python3
"""Manual Windows/NVIDIA smoke test for desktop sidecar GPU viability."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description='Validate desktop GPU runtime on Windows/NVIDIA')
    parser.add_argument('--model', required=True, help='Path to a GGUF model to initialize')
    parser.add_argument('--mode', default='auto', choices=['auto', 'gpu', 'hybrid'])
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    python_dir = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(python_dir) not in sys.path:
        sys.path.insert(0, str(python_dir))

    os.environ.setdefault('TOKEN_PLACE_DESKTOP_RUNTIME_REEXECED', '1')

    from desktop_runtime_setup import ensure_desktop_llama_runtime
    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import get_model_manager

    runtime_setup = ensure_desktop_llama_runtime(args.mode)
    manager = get_model_manager()
    manager.model_path = args.model
    apply_compute_mode(manager, args.mode)

    pre_init = compute_mode_diagnostics(manager)
    llm = manager.get_llm_instance()
    _assert(llm is not None, 'failed to initialize llama runtime for smoke test model')
    post_init = compute_mode_diagnostics(manager)

    offloaded_layers = post_init.get('offloaded_layers', post_init.get('n_gpu_layers', 0))
    kv_cache_device = post_init.get('kv_cache_device') or post_init.get('kv_cache')

    report = {
        'runtime_setup': runtime_setup,
        'pre_init': pre_init,
        'post_init': post_init,
        'authoritative_interpreter': runtime_setup.get('interpreter', sys.executable),
        'authoritative_llama_module_path': runtime_setup.get('llama_module_path', 'missing'),
        'offloaded_layers': offloaded_layers,
        'kv_cache_device': kv_cache_device,
    }
    print(json.dumps(report, indent=2))

    _assert(sys.platform.startswith('win'), 'this smoke test is Windows-only')
    _assert('nvidia' in (runtime_setup.get('detected_device', '')).lower(), 'detected_device != nvidia')
    _assert(
        runtime_setup.get('interpreter') not in (None, '', 'missing'),
        'authoritative interpreter path missing',
    )
    _assert(
        runtime_setup.get('llama_module_path') not in (None, '', 'missing'),
        'authoritative llama_cpp module path missing',
    )
    _assert(pre_init.get('backend_available') == 'cuda', 'backend_available != cuda')
    _assert(post_init.get('backend_used') == 'cuda', 'backend_used != cuda')
    _assert(isinstance(offloaded_layers, int) and offloaded_layers > 0, 'offloaded_layers must be > 0')
    _assert(kv_cache_device not in (None, 'cpu'), 'kv_cache device indicates CPU-only path')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f'GPU smoke test failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
