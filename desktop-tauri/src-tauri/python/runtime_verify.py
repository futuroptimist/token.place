#!/usr/bin/env python3
"""Manual runtime verification helper for desktop llama.cpp GPU diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__)


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop runtime verification")
    parser.add_argument("--model", default="", help="Optional GGUF path to force model initialization")
    parser.add_argument("--mode", default="auto", help="Compute mode used for optional model init")
    args = parser.parse_args()

    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import detect_llama_runtime_capabilities, get_model_manager

    runtime = detect_llama_runtime_capabilities()
    payload = {
        "python_executable": sys.executable,
        "python_prefix": sys.prefix,
        "llama_cpp_path": runtime.get("llama_cpp_path"),
        "backend": runtime.get("backend"),
        "detected_device": runtime.get("detected_device"),
        "gpu_offload_supported": runtime.get("gpu_offload_supported"),
        "error": runtime.get("error"),
    }

    if args.model:
        manager = get_model_manager()
        manager.model_path = args.model
        apply_compute_mode(manager, args.mode)
        manager.get_llm_instance()
        payload["post_init_diagnostics"] = compute_mode_diagnostics(manager)

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
