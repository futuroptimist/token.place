#!/usr/bin/env python3
"""Desktop bridge for shared Python model metadata and downloads."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(ok: bool, *, payload: Dict[str, Any] | None = None, error: str = "") -> int:
    data: Dict[str, Any] = {"ok": ok}
    if payload is not None:
        data["payload"] = payload
    if error:
        data["error"] = error
    print(json.dumps(data))
    return 0 if ok else 1


def inspect_model() -> int:
    config_schema = _load_module(
        "token_place_config_schema",
        REPO_ROOT / "utils" / "config_schema.py",
    )
    path_handling = _load_module(
        "token_place_path_handling",
        REPO_ROOT / "utils" / "path_handling.py",
    )

    model_cfg = config_schema.DEFAULT_CONFIG["model"]
    filename = model_cfg["filename"]
    url = model_cfg["url"]
    canonical = model_cfg.get(
        "canonical_family_url",
        "https://huggingface.co/meta-llama/Meta-Llama-3-8B",
    )
    models_dir = str(path_handling.get_models_dir())
    model_path = str(Path(models_dir) / filename)
    exists = os.path.exists(model_path)
    return _response(
        True,
        payload={
            "canonical_family_url": canonical,
            "filename": filename,
            "url": url,
            "models_dir": models_dir,
            "resolved_model_path": model_path,
            "exists": exists,
            "size_bytes": os.path.getsize(model_path) if exists else None,
        },
    )


def download_model() -> int:
    try:
        from utils.llm.model_manager import get_model_manager
    except ModuleNotFoundError as exc:
        return _response(
            False,
            error=(
                "Missing Python dependency for model downloads "
                f"({exc}). Run `pip install -r requirements.txt`."
            ),
        )

    manager = get_model_manager()
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
