#!/usr/bin/env python3
"""macOS lifecycle guard: desktop app must not autostart or retain relay."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_APP = REPO_ROOT / "desktop-tauri" / "src-tauri" / "target" / "debug" / "token.place desktop.app"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _has_process_matching(pattern: str) -> bool:
    proc = subprocess.run(["ps", "ax"], capture_output=True, text=True, check=False)
    return proc.returncode == 0 and pattern in proc.stdout


def _assert_no_relay_port_listener() -> None:
    lsof = shutil.which("lsof")
    if not lsof:
        assert not _port_in_use(5010), "127.0.0.1:5010 is listening after app shutdown"
        return
    probe = subprocess.run(
        [lsof, "-nP", "-iTCP:5010", "-sTCP:LISTEN"], capture_output=True, text=True, check=False
    )
    assert probe.returncode != 0 or "LISTEN" not in probe.stdout, probe.stdout


def main() -> int:
    require_no_relay_e2e = os.getenv("TOKENPLACE_REQUIRE_NO_RELAY_E2E") == "1"

    if platform.system() != "Darwin":
        message = "desktop no-relay lifecycle e2e is macOS-only"
        if require_no_relay_e2e:
            raise AssertionError(message)
        print(f"SKIP: {message}")
        return 0

    if not DESKTOP_APP.exists():
        message = f"desktop app binary not found: {DESKTOP_APP}"
        if require_no_relay_e2e:
            raise AssertionError(message)
        print(f"SKIP: {message}")
        return 0

    env = os.environ.copy()
    env["TOKENPLACE_DESKTOP_SKIP_AUTOSTART"] = "1"
    env["TOKENPLACE_DESKTOP_DISABLE_OPERATOR_AUTOSTART"] = "1"

    with tempfile.TemporaryDirectory(prefix="token-place-no-relay-home-") as home:
        env["HOME"] = home
        app = subprocess.Popen(
            ["open", "-W", str(DESKTOP_APP)],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(2)
        subprocess.run(["osascript", "-e", 'tell application "token.place desktop" to quit'], check=False)
        app.wait(timeout=60)

    assert not _has_process_matching("relay.py"), "relay.py process exists after desktop shutdown"
    _assert_no_relay_port_listener()
    assert not _has_process_matching("python relay.py"), "detached python relay.py remains after shutdown"
    assert not _has_process_matching("launchctl.*relay"), "launchctl relay job remains after shutdown"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
