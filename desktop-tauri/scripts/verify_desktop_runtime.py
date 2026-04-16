#!/usr/bin/env python3
"""Manual desktop runtime verification for llama-cpp backend wiring."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _is_repo_local_llama_shim(module_path: str, repo_root: Path) -> bool:
    try:
        return Path(module_path).resolve() == (repo_root / 'llama_cpp.py').resolve()
    except Exception:
        return False


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

    from desktop_runtime_setup import ensure_desktop_llama_runtime
    from utils.llm.model_manager import detect_llama_runtime_capabilities

    runtime_setup = ensure_desktop_llama_runtime(args.mode, repo_root=repo_root)
    runtime = detect_llama_runtime_capabilities()
    payload = {
        'backend': runtime_setup.get('selected_backend', runtime.get('backend', 'missing')),
        'gpu_offload_supported': runtime.get('gpu_offload_supported', False),
        'detected_device': runtime_setup.get('detected_device', runtime.get('detected_device', 'none')),
        'interpreter': runtime_setup.get('interpreter', runtime.get('interpreter', sys.executable)),
        'prefix': runtime_setup.get('prefix', runtime.get('prefix', sys.prefix)),
        'llama_module_path': runtime_setup.get(
            'llama_module_path',
            runtime.get('llama_module_path', 'missing'),
        ),
        'runtime_action': runtime_setup.get('runtime_action', 'unknown'),
        'fallback_reason': runtime_setup.get('fallback_reason', ''),
        'error': runtime_setup.get('fallback_reason') or runtime.get('error'),
    }

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

    if _is_repo_local_llama_shim(payload['llama_module_path'], repo_root):
        payload['error'] = (
            'llama_cpp import shadowed by repo-local shim; expected installed '
            'llama-cpp-python package path under site-packages'
        )
        payload['shadowing_detected'] = True
    else:
        payload['shadowing_detected'] = False

    print(json.dumps(payload, indent=2))
    return 1 if payload['shadowing_detected'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
