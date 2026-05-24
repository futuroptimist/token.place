#!/usr/bin/env python3
"""Desktop bridge for shared Python model metadata and downloads."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


if __package__ in (None, ""):
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__)


def _response(ok: bool, *, payload: Dict[str, Any] | None = None, error: str = "") -> int:
    data: Dict[str, Any] = {"ok": ok}
    if payload is not None:
        data["payload"] = payload
    if error:
        data["error"] = error
    print(json.dumps(data))
    return 0 if ok else 1


def _get_model_manager():
    if os.getenv("TOKEN_PLACE_DESKTOP_TEST_MISSING_MODEL_DEPS") == "1":
        return None, _response(
            False,
            error="Missing Python dependency for model downloads (No module named 'psutil').",
        )
    try:
        from utils.llm.model_manager import get_model_manager
    except ModuleNotFoundError as exc:
        return None, _response(
            False,
            error=(
                "Missing Python dependency for model downloads "
                f"({exc}). Run `pip install -r requirements.txt`."
            ),
        )

    return get_model_manager(), None


def _inspect_without_model_manager(missing_dependency: str) -> int:
    from config import get_config

    cfg = get_config()
    filename = cfg.get("model.filename")
    url = cfg.get("model.url")
    canonical_family_url = cfg.get("model.canonical_family_url")
    models_dir = Path(str(cfg.get("paths.models_dir")))
    resolved_model_path = models_dir / filename

    payload: Dict[str, Any] = {
        "canonical_family_url": canonical_family_url,
        "filename": filename,
        "url": url,
        "models_dir": str(models_dir),
        "resolved_model_path": str(resolved_model_path),
        "exists": resolved_model_path.exists(),
        "size_bytes": resolved_model_path.stat().st_size if resolved_model_path.exists() else None,
        "downloads_available": False,
        "downloads_unavailable_reason": (
            "Optional model download dependencies are unavailable in this runtime "
            f"({missing_dependency}). Download controls remain disabled until runtime "
            "dependencies are provisioned."
        ),
    }
    return _response(True, payload=payload)


def inspect_model() -> int:
    manager, error_status = _get_model_manager()
    if error_status is not None:
        try:
            from utils.llm.model_manager import get_model_manager
        except ModuleNotFoundError as exc:
            return _inspect_without_model_manager(str(exc))
        return error_status
    return _response(True, payload=manager.get_model_artifact_metadata())


def download_model() -> int:
    manager, error_status = _get_model_manager()
    if error_status is not None:
        return error_status

    if not manager.download_model_if_needed():
        return _response(
            False,
            error=(
                "Download failed. Verify network access to Hugging Face and check that "
                "the models directory is writable."
            ),
        )
    return _response(True, payload=manager.get_model_artifact_metadata())


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop model bridge")
    parser.add_argument("action", choices=["inspect", "download"])
    args = parser.parse_args()

    try:
        if args.action == "inspect":
            return inspect_model()
        return download_model()
    except Exception as exc:  # pragma: no cover - defensive bridge error handling
        return _response(False, error=f"Model bridge failure: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
