#!/usr/bin/env python3
"""Manual Windows/NVIDIA smoke test for desktop sidecar GPU runtime path."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _fail(message: str, payload: dict) -> int:
    payload["result"] = "fail"
    payload["failure_reason"] = message
    print(json.dumps(payload, indent=2))
    return 1


def _assert_truthy(payload: dict, key: str, value: object) -> str | None:
    if value:
        return None
    return f"missing/empty required field: {key}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Windows/NVIDIA desktop sidecar runtime bootstrap + GPU diagnostics"
    )
    parser.add_argument("--model", required=True, help="Path to a local GGUF model")
    parser.add_argument("--mode", default="auto", choices=["auto", "gpu", "hybrid"])
    args = parser.parse_args()

    model_path = Path(args.model).expanduser()
    if not model_path.is_file():
        print(
            json.dumps(
                {"result": "fail", "failure_reason": f"model path not found: {model_path}"},
                indent=2,
            )
        )
        return 1

    repo_root = Path(__file__).resolve().parents[2]
    python_dir = repo_root / "desktop-tauri" / "src-tauri" / "python"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(python_dir) not in sys.path:
        sys.path.insert(0, str(python_dir))

    from desktop_runtime_setup import ensure_desktop_llama_runtime, maybe_reexec_for_runtime_refresh
    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import get_model_manager

    runtime_setup = ensure_desktop_llama_runtime(args.mode)
    maybe_reexec_for_runtime_refresh(runtime_setup)

    manager = get_model_manager()
    manager.model_path = str(model_path)
    apply_compute_mode(manager, args.mode)
    pre = compute_mode_diagnostics(manager)

    llm = manager.get_llm_instance()
    if llm is None:
        payload = {
            "result": "fail",
            "failure_reason": "unable to initialize llama runtime",
            "runtime_setup": runtime_setup,
            "pre_init": pre,
        }
        print(json.dumps(payload, indent=2))
        return 1

    post = compute_mode_diagnostics(manager)

    payload = {
        "result": "pass",
        "mode": args.mode,
        "authoritative_interpreter": runtime_setup.get("interpreter", sys.executable),
        "authoritative_llama_module_path": runtime_setup.get("llama_module_path", "missing"),
        "runtime_setup": runtime_setup,
        "pre_init": pre,
        "post_init": post,
    }

    for key in ("authoritative_interpreter", "authoritative_llama_module_path"):
        error = _assert_truthy(payload, key, payload.get(key))
        if error:
            return _fail(error, payload)

    if post.get("backend_available") != "cuda":
        return _fail(
            f"expected backend_available=cuda, got {post.get('backend_available')}",
            payload,
        )
    if post.get("backend_used") != "cuda":
        return _fail(
            f"expected backend_used=cuda, got {post.get('backend_used')}",
            payload,
        )

    offloaded_layers = post.get("offloaded_layers")
    if offloaded_layers is None:
        offloaded_layers = post.get("n_gpu_layers")
    if not isinstance(offloaded_layers, int) or offloaded_layers <= 0:
        return _fail(f"expected offloaded_layers > 0, got {offloaded_layers!r}", payload)

    kv_cache = str(post.get("kv_cache_device") or post.get("kv_cache") or "").lower()
    if "cpu" in kv_cache and "cuda" not in kv_cache and "gpu" not in kv_cache:
        return _fail(f"expected non-CPU KV cache placement, got {kv_cache!r}", payload)

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
