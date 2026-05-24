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

def _default_models_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "token.place" / "models"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "token.place" / "models"
        return Path.home() / "AppData" / "Roaming" / "token.place" / "models"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "token.place" / "models"
    return Path.home() / ".local" / "share" / "token.place" / "models"


def _fallback_model_metadata() -> Dict[str, Any]:
    models_dir = _default_models_dir()
    models_dir_override = os.environ.get("TOKEN_PLACE_MODELS_DIR")
    if models_dir_override:
        models_dir = Path(models_dir_override)

    canonical_family_url = os.environ.get(
        "TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL",
        "https://huggingface.co/meta-llama/Meta-Llama-3-8B",
    )
    filename = os.environ.get(
        "TOKEN_PLACE_DEFAULT_MODEL_FILENAME",
        "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    )
    url = os.environ.get(
        "TOKEN_PLACE_DEFAULT_MODEL_URL",
        "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    )
    resolved_model_path = models_dir / filename
    exists = resolved_model_path.exists()
    size_bytes = resolved_model_path.stat().st_size if exists else None

    return {
        "canonical_family_url": canonical_family_url,
        "filename": filename,
        "url": url,
        "models_dir": str(models_dir),
        "resolved_model_path": str(resolved_model_path),
        "exists": exists,
        "size_bytes": size_bytes,
    }




def _response(ok: bool, *, payload: Dict[str, Any] | None = None, error: str = "") -> int:
    data: Dict[str, Any] = {"ok": ok}
    if payload is not None:
        data["payload"] = payload
    if error:
        data["error"] = error
    print(json.dumps(data))
    return 0 if ok else 1


_INSPECT_OPTIONAL_IMPORTS = {"psutil", "urllib3", "requests", "dotenv"}


def _is_inspect_optional_missing(exc: ModuleNotFoundError) -> bool:
    missing_name = getattr(exc, "name", None)
    if missing_name in _INSPECT_OPTIONAL_IMPORTS:
        return True
    message = str(exc)
    return any(f"No module named '{name}'" in message for name in _INSPECT_OPTIONAL_IMPORTS)


def _get_model_manager(*, allow_inspect_fallback: bool = False):
    try:
        from utils.llm.model_manager import get_model_manager
    except ModuleNotFoundError as exc:
        if allow_inspect_fallback and _is_inspect_optional_missing(exc):
            return None, {"ok": True, "payload": _fallback_model_metadata()}
        return None, {
            "ok": False,
            "error": f"Missing Python dependency for model downloads ({exc}).",
        }

    return get_model_manager(), None


def inspect_model() -> int:
    manager, error_status = _get_model_manager(allow_inspect_fallback=True)
    if error_status is not None:
        return _response(**error_status)
    return _response(True, payload=manager.get_model_artifact_metadata())


def download_model() -> int:
    manager, error_status = _get_model_manager()
    if error_status is not None:
        return _response(**error_status)

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
