#!/usr/bin/env python3
"""Manual desktop runtime verification for llama-cpp backend wiring."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description='Verify desktop llama runtime wiring')
    parser.add_argument('--mode', default='auto')
    parser.add_argument('--model', default='')
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    python_root = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    if str(python_root) not in sys.path:
        sys.path.insert(0, str(python_root))

    from path_bootstrap import ensure_runtime_import_paths

    ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)
    repo_llama_shim = str((repo_root / 'llama_cpp.py').resolve())

    from utils.llm.model_manager import detect_llama_runtime_capabilities
    from desktop_runtime_setup import ensure_desktop_llama_runtime

    runtime_setup = ensure_desktop_llama_runtime(args.mode, repo_root=repo_root)
    runtime = detect_llama_runtime_capabilities()
    payload = {
        'backend': runtime.get('backend', 'missing'),
        'gpu_offload_supported': runtime.get('gpu_offload_supported', False),
        'detected_device': runtime.get('detected_device', 'none'),
        'interpreter': runtime.get('interpreter', sys.executable),
        'prefix': runtime.get('prefix', sys.prefix),
        'llama_module_path': runtime.get('llama_module_path', 'missing'),
        'error': runtime.get('error'),
        'runtime_setup': runtime_setup,
    }
    if str(payload['llama_module_path']).lower() == repo_llama_shim.lower():
        payload['error'] = (
            'llama_cpp resolved to repo-local shim '
            f'({payload["llama_module_path"]}); expected installed llama-cpp-python module '
            'under site-packages'
        )

    try:
        from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
        from utils.llm.model_manager import get_model_manager

        manager = get_model_manager()
        if args.model:
            manager.model_path = args.model
        apply_compute_mode(manager, args.mode)
        pre_init = compute_mode_diagnostics(manager)
        payload['compute_runtime_pre_init'] = {
            'requested': pre_init.get('requested_mode'),
            'effective': pre_init.get('effective_mode'),
            'backend_available': pre_init.get('backend_available'),
            'backend_used': pre_init.get('backend_used'),
            'device_backend': pre_init.get('backend_used'),
            'device_name': 'unreported',
            'offloaded_layers': pre_init.get('n_gpu_layers'),
            'kv_cache': 'unknown_pre_init',
            'fallback_reason': pre_init.get('fallback_reason'),
            'interpreter': payload['interpreter'],
            'llama_module_path': payload['llama_module_path'],
        }

        if args.model and os.path.exists(args.model):
            manager.get_llm_instance()
            post_init = compute_mode_diagnostics(manager)
            payload['compute_runtime_post_init'] = {
                'requested': post_init.get('requested_mode'),
                'effective': post_init.get('effective_mode'),
                'backend_available': post_init.get('backend_available'),
                'backend_used': post_init.get('backend_used'),
                'device_backend': post_init.get('device_backend'),
                'device_name': post_init.get('device_name'),
                'offloaded_layers': post_init.get('offloaded_layers'),
                'kv_cache': post_init.get('kv_cache_device'),
                'fallback_reason': post_init.get('fallback_reason'),
                'interpreter': payload['interpreter'],
                'llama_module_path': payload['llama_module_path'],
            }
        else:
            payload['compute_runtime_post_init'] = (
                'skipped (provide --model <gguf> to initialize Llama)'
            )
    except ModuleNotFoundError as exc:
        payload['compute_runtime_pre_init'] = f'skipped (missing dependency: {exc})'
        payload['compute_runtime_post_init'] = 'skipped'

    print(json.dumps(payload, indent=2))
    if str(payload['llama_module_path']).lower() == repo_llama_shim.lower():
        return 1
    if runtime_setup.get('runtime_action') == 'shadowed_repo_llama_cpp':
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
