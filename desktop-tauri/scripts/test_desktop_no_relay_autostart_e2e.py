#!/usr/bin/env python3
"""Best-effort lifecycle guard: desktop launch/close must not leave relay.py on 5010."""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MACOS_DEBUG_APP = REPO_ROOT / "desktop-tauri" / "src-tauri" / "target" / "debug" / "bundle" / "macos" / "token.place desktop.app" / "Contents" / "MacOS" / "token.place desktop"


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return (result.stdout or "") + (result.stderr or "")


def _assert_no_localhost_5010_listener() -> None:
    output = _run(["lsof", "-nP", "-iTCP:5010", "-sTCP:LISTEN"])
    assert "relay.py" not in output, output


def main() -> int:
    if platform.system() != "Darwin":
        print("skip: lifecycle e2e targets macOS only")
        return 0
    if not MACOS_DEBUG_APP.exists():
        print(f"skip: debug desktop binary not found at {MACOS_DEBUG_APP}")
        return 0

    _assert_no_localhost_5010_listener()
    proc = subprocess.Popen([str(MACOS_DEBUG_APP)], cwd=REPO_ROOT)  # noqa: S603
    try:
        time.sleep(5)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    time.sleep(1)
    _assert_no_localhost_5010_listener()
    relay_ps = _run(["ps", "aux"])
    assert "relay.py" not in relay_ps, relay_ps
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
