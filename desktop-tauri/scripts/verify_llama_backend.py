#!/usr/bin/env python3
"""Smoke-test helper to print effective llama.cpp backend/offload diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON_RUNTIME_DIR = SCRIPT_DIR.parent / "src-tauri" / "python"
if str(PYTHON_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_RUNTIME_DIR))

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify desktop llama.cpp backend selection")
    parser.add_argument("--model", required=True, help="Path to GGUF model")
    parser.add_argument("--mode", default="auto", help="compute mode (auto/cpu/gpu/hybrid)")
    args = parser.parse_args()

    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import get_model_manager

    manager = get_model_manager()
    manager.model_path = str(Path(args.model).expanduser())
    apply_compute_mode(manager, args.mode)
    llm = manager.get_llm_instance()
    diagnostics = compute_mode_diagnostics(manager)

    output = {
        "initialized": llm is not None,
        "diagnostics": diagnostics,
    }
    print(json.dumps(output))
    return 0 if llm is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
