#!/usr/bin/env python3
"""Focused deterministic desktop worker soak/fault-injection regression.

This wrapper keeps the local reproduction command stable for the desktop
operator parity workflow while the actual fake-worker assertions live beside the
model-manager lifecycle unit tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_NODE = "tests/unit/test_model_manager.py::test_desktop_long_lived_worker_soak_and_fault_injection"


def main() -> int:
    command = [sys.executable, "-m", "pytest", "-q", TEST_NODE]
    print("$ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=REPO_ROOT).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
