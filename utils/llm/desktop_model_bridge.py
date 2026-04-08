"""Desktop-facing bridge for shared model metadata and downloads."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict

from utils.llm.model_manager import get_model_manager

CANONICAL_MODEL_FAMILY_URL = "https://huggingface.co/meta-llama/Meta-Llama-3-8B"


def model_metadata() -> Dict[str, Any]:
    """Return model metadata used by the shared runtime configuration."""
    manager = get_model_manager()
    return {
        "canonical_model_family_url": CANONICAL_MODEL_FAMILY_URL,
        "artifact_filename": manager.file_name,
        "artifact_url": manager.url,
        "resolved_model_path": manager.model_path,
        "models_dir": manager.models_dir,
    }


def ensure_downloaded() -> Dict[str, Any]:
    """Ensure the configured runtime GGUF is present and return download details."""
    manager = get_model_manager()
    existed_before = bool(manager.model_path) and os.path.exists(manager.model_path)
    if not manager.download_model_if_needed():
        raise RuntimeError(
            "Failed to download model artifact "
            f"{manager.file_name} from {manager.url}. "
            "Check network access and disk space, then retry."
        )

    return {
        "resolved_model_path": manager.model_path,
        "artifact_filename": manager.file_name,
        "artifact_url": manager.url,
        "status": "already_present" if existed_before else "downloaded",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Desktop bridge for token.place model management")
    parser.add_argument("action", choices=["metadata", "download"])
    args = parser.parse_args()

    try:
        payload = model_metadata() if args.action == "metadata" else ensure_downloaded()
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    print(json.dumps({"ok": True, "data": payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
