#!/usr/bin/env python3
"""Manual smoke test: print desktop llama.cpp backend diagnostics."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.llm.model_manager import get_model_manager

    parser = argparse.ArgumentParser(description="Verify desktop llama.cpp backend selection")
    parser.add_argument("--mode", default="auto", help="compute mode: auto|gpu|hybrid|cpu")
    parser.add_argument(
        "--model",
        default=None,
        help="optional GGUF path to force model init and verify real runtime selection",
    )
    args = parser.parse_args()

    manager = get_model_manager()
    if args.model:
        manager.model_path = args.model
    apply_compute_mode(manager, args.mode)

    diagnostics_before = compute_mode_diagnostics(manager)
    payload = {"pre_init": diagnostics_before}

    if args.model:
        llm = manager.get_llm_instance()
        payload["initialized"] = bool(llm is not None)
        payload["post_init"] = compute_mode_diagnostics(manager)

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
