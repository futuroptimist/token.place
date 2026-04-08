"""Desktop bridge for shared model metadata and downloads.

This module keeps desktop-tauri aligned with server.py by delegating model
selection and download behavior to :mod:`utils.llm.model_manager`.
"""

from __future__ import annotations

import argparse
import json
import sys

from utils.llm.model_manager import get_model_manager


def _run_metadata() -> int:
    manager = get_model_manager()
    print(json.dumps(manager.artifact_metadata()))
    return 0


def _run_download() -> int:
    manager = get_model_manager()
    if not manager.download_model_if_needed():
        raise RuntimeError(
            "Unable to download the configured model artifact. "
            f"Check connectivity and URL: {manager.url}"
        )
    print(json.dumps(manager.artifact_metadata()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="token.place desktop model bridge")
    parser.add_argument("action", choices=["metadata", "download"])
    args = parser.parse_args(argv)

    if args.action == "metadata":
        return _run_metadata()
    if args.action == "download":
        return _run_download()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
