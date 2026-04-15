#!/usr/bin/env python3
"""Manual desktop helper: print authoritative llama-cpp runtime details."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__)

from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
from utils.llm.model_manager import detect_llama_runtime_capabilities, get_model_manager


def main() -> int:
    mode = "auto"
    if len(sys.argv) > 1:
        mode = sys.argv[1]

    payload = detect_llama_runtime_capabilities()
    print(f"sys.executable={sys.executable}")
    print(f"sys.prefix={sys.prefix}")
    print(f"llama_cpp.__file__={payload.get('llama_cpp_module_path') or 'missing'}")
    print(f"GGML backend marker={payload.get('backend')}")
    print(f"llama_supports_gpu_offload={payload.get('gpu_offload_supported')}")

    manager = get_model_manager()
    apply_compute_mode(manager, mode)
    manager.last_compute_diagnostics = manager._resolve_compute_plan()  # noqa: SLF001
    print(f"model_manager.compute_mode={json.dumps(compute_mode_diagnostics(manager), sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
