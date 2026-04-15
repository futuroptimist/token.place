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
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from utils.llm.model_manager import detect_llama_runtime_capabilities

    payload = {
        'sys.executable': sys.executable,
        'sys.prefix': sys.prefix,
    }

    runtime = detect_llama_runtime_capabilities()
    payload['llama_cpp.__file__'] = runtime.get('llama_module_path', 'missing')
    payload['GGML_USE_CUDA'] = runtime.get('backend') == 'cuda'
    payload['GGML_USE_METAL'] = runtime.get('backend') == 'metal'
    payload['llama_supports_gpu_offload'] = runtime.get('gpu_offload_supported', False)

    try:
        from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
        from utils.llm.model_manager import get_model_manager

        manager = get_model_manager()
        if args.model:
            manager.model_path = args.model
        apply_compute_mode(manager, args.mode)
        payload['compute_plan_pre_init'] = compute_mode_diagnostics(manager)

        if args.model and os.path.exists(args.model):
            manager.get_llm_instance()
            payload['compute_plan_post_init'] = compute_mode_diagnostics(manager)
        else:
            payload['compute_plan_post_init'] = 'skipped (provide --model <gguf> to initialize Llama)'
    except ModuleNotFoundError as exc:
        payload['compute_plan_pre_init'] = f'skipped (missing dependency: {exc})'
        payload['compute_plan_post_init'] = 'skipped'

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
