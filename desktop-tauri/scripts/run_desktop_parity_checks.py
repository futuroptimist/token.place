#!/usr/bin/env python3
"""Run the shared desktop operator parity validation suite.

This wrapper is intentionally thin: it groups the existing focused validation
scripts behind one evergreen entry point so CI and release docs do not grow
separate Windows/macOS checklists.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent


def _run(script_name: str, *, env: dict[str, str] | None = None) -> None:
    command = [sys.executable, str(SCRIPT_DIR / script_name)]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    print(f"desktop parity check: running {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=REPO_ROOT, env=merged_env, check=True)


def _scripts_for_profile(profile: str) -> Iterable[tuple[str, dict[str, str] | None]]:
    if profile == "inspect-only":
        yield "test_packaged_operator_e2e.py", {"TOKEN_PLACE_INSPECT_ONLY": "1"}
        return

    yield "test_packaged_operator_e2e.py", None
    yield "test_desktop_relay_operator_parity_e2e.py", None

    if profile in {"ci-macos", "local-macos"}:
        yield "test_desktop_no_relay_autostart_e2e.py", {
            "TOKENPLACE_REQUIRE_NO_RELAY_E2E": "1"
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=(
            "ci-windows",
            "ci-macos",
            "local-windows",
            "local-macos",
            "local-cpu",
            "inspect-only",
        ),
        default="local-cpu",
        help=(
            "Validation profile. Windows/macOS profiles share packaged-resource and API v1 relay parity checks; "
            "macOS profiles also require the no-relay lifecycle e2e. Hardware CUDA/Metal smoke checks remain manual "
            "because CI runners do not provide release-class GPUs."
        ),
    )
    args = parser.parse_args()

    for script_name, env in _scripts_for_profile(args.profile):
        _run(script_name, env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
