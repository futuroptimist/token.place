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
    try:
        from utils.llm.model_manager import get_model_manager
    except ModuleNotFoundError as exc:
        return None, exc

    return get_model_manager(), None


def _fallback_model_metadata() -> Dict[str, Any]:
    filename = 'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'
    url = (
        'https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/'
        'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'
    )
    canonical_family_url = 'https://huggingface.co/meta-llama/Meta-Llama-3-8B'
    models_dir = Path(os.environ.get('TOKEN_PLACE_MODELS_DIR', str(Path.home() / '.token-place' / 'models')))
    resolved_model_path = models_dir / filename
    return {
        'canonical_family_url': canonical_family_url,
        'filename': filename,
        'url': url,
        'models_dir': str(models_dir),
        'resolved_model_path': str(resolved_model_path),
        'exists': resolved_model_path.exists(),
        'size_bytes': resolved_model_path.stat().st_size if resolved_model_path.exists() else None,
        'dependency_status': 'missing_optional_download_dependencies',
    }


def inspect_model() -> int:
    manager, manager_error = _get_model_manager()
    if manager_error is not None:
        if isinstance(manager_error, ModuleNotFoundError):
            return _response(True, payload=_fallback_model_metadata())
        return _response(False, error=f"Model bridge failure: {manager_error}")
    return _response(True, payload=manager.get_model_artifact_metadata())


def download_model() -> int:
    manager, manager_error = _get_model_manager()
    if manager_error is not None:
        if isinstance(manager_error, ModuleNotFoundError):
            return _response(
                False,
                error=(
                    "Missing Python dependency for model downloads "
                    f"({manager_error}). Local model download is unavailable in this runtime."
                ),
            )
        return _response(False, error=f"Model bridge failure: {manager_error}")

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
