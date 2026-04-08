#!/usr/bin/env python3
"""Desktop bridge for shared token.place model management logic."""
from __future__ import annotations

import json
import sys
from typing import Any, Dict

from utils.llm.model_manager import get_model_manager

CANONICAL_MODEL_FAMILY_URL = "https://huggingface.co/meta-llama/Meta-Llama-3-8B"


def _info_payload() -> Dict[str, Any]:
    manager = get_model_manager()
    return {
        "canonical_model_family_url": CANONICAL_MODEL_FAMILY_URL,
        "artifact": {
            "filename": manager.file_name,
            "url": manager.url,
            "models_dir": manager.models_dir,
            "resolved_model_path": manager.model_path,
        },
    }


def _download_payload() -> Dict[str, Any]:
    manager = get_model_manager()
    ok = manager.download_model_if_needed()
    if not ok:
        return {
            "ok": False,
            "error": (
                "Model download failed. Verify network access, disk space, and write "
                f"permissions for {manager.models_dir}."
            ),
            "artifact": _info_payload()["artifact"],
        }
    return {
        "ok": True,
        "artifact": _info_payload()["artifact"],
    }


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"info", "download"}:
        sys.stderr.write("Usage: model_bridge.py [info|download]\n")
        return 2

    try:
        command = sys.argv[1]
        payload = _info_payload() if command == "info" else _download_payload()
        sys.stdout.write(json.dumps(payload))
        return 0
    except Exception as exc:  # pragma: no cover - defensive bridge guard
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Python model bridge failed: {exc}",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
