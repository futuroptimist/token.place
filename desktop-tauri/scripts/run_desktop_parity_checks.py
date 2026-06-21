#!/usr/bin/env python3
"""Shared entry point for desktop operator parity validation.

The default mode runs local-only, deterministic checks that exercise packaged
resource resolution plus the API v1 E2EE relay lifecycle with mock LLM. Hardware
CUDA/Metal and staging round-robin validation remain manual release gates and are
documented in docs/desktop_parity_validation.md.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent


def _run(label: str, command: list[str], *, env: dict[str, str] | None = None) -> int:
    print(f"\n==> {label}", flush=True)
    print("$ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env)  # noqa: S603
    if completed.returncode != 0:
        print(f"{label} failed with exit code {completed.returncode}", file=sys.stderr)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run shared desktop parity checks")
    parser.add_argument(
        "--skip-packaged",
        action="store_true",
        help="Skip packaged resource/dependency isolation e2e",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Run packaged resource inspection only before relay parity",
    )
    args = parser.parse_args()

    if not args.skip_packaged:
        env = os.environ.copy()
        if args.inspect_only:
            env["TOKEN_PLACE_INSPECT_ONLY"] = "1"
        code = _run(
            "packaged resource and dependency isolation e2e",
            [sys.executable, str(SCRIPT_DIR / "test_packaged_operator_e2e.py")],
            env=env,
        )
        if code != 0:
            return code

    code = _run(
        "desktop API v1 E2EE relay operator parity e2e",
        [sys.executable, str(SCRIPT_DIR / "test_desktop_relay_operator_parity_e2e.py")],
    )
    if code != 0:
        return code

    return _run(
        "deterministic long-lived worker soak and fault-injection guardrail",
        [sys.executable, "-m", "pytest", "-q", "tests/unit/test_long_lived_worker_soak.py"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
