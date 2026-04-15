#!/usr/bin/env python3
"""Manual desktop runtime verification helper for llama-cpp backend diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__)


def _runtime_probe() -> dict[str, object]:
    payload: dict[str, object] = {
        "sys.executable": sys.executable,
        "sys.prefix": sys.prefix,
        "llama_cpp.__file__": "missing",
        "GGML_USE_CUDA": False,
        "GGML_USE_METAL": False,
        "llama_supports_gpu_offload()": False,
        "probe_error": None,
    }
    try:
        import llama_cpp
    except Exception as exc:  # pragma: no cover - runtime environment handling
        payload["probe_error"] = str(exc)
        return payload

    payload["llama_cpp.__file__"] = str(getattr(llama_cpp, "__file__", "unknown"))
    payload["GGML_USE_CUDA"] = bool(getattr(llama_cpp, "GGML_USE_CUDA", False))
    payload["GGML_USE_METAL"] = bool(getattr(llama_cpp, "GGML_USE_METAL", False))
    supports_gpu = getattr(llama_cpp, "llama_supports_gpu_offload", None)
    if callable(supports_gpu):
        try:
            payload["llama_supports_gpu_offload()"] = bool(supports_gpu())
        except Exception as exc:  # pragma: no cover - runtime environment handling
            payload["probe_error"] = str(exc)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify desktop llama-cpp runtime wiring")
    parser.add_argument("--model", default="", help="Optional GGUF path for ModelManager init")
    parser.add_argument("--mode", default="auto", help="Requested compute mode")
    args = parser.parse_args()

    payload = _runtime_probe()
    print(json.dumps(payload, indent=2, sort_keys=True))

    if not args.model:
        return 0

    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import get_model_manager

    manager = get_model_manager()
    manager.model_path = args.model
    apply_compute_mode(manager, args.mode)
    llm = manager.get_llm_instance()
    diagnostics = compute_mode_diagnostics(manager)
    print(
        json.dumps(
            {
                "llm_initialized": llm is not None,
                "diagnostics": diagnostics,
                "model_path": args.model if args.model else Path(manager.model_path).name,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if llm is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
