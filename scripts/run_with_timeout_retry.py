#!/usr/bin/env python3
"""Run a command in isolated process groups with bounded cleanup and retries."""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import time


def _enable_subreaper() -> None:
    # Adopt orphaned grandchildren so that a privileged child cannot remain a zombie.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _group_exists(pgid: int) -> bool:
    _reap()
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    # GitHub-hosted runners provide passwordless sudo. Using it here is essential:
    # Playwright's runner-owned parent starts root-owned apt/dpkg descendants.
    if os.geteuid() == 0:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        return
    sudo = shutil.which("sudo")
    if sudo is None:
        raise RuntimeError("sudo is required to clean up privileged descendants")
    result = subprocess.run(
        [sudo, "--non-interactive", "kill", f"-{sig.value}", "--", f"-{pgid}"],
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"sudo kill exited with status {result.returncode}")


def _clean_group(pgid: int, grace: float) -> None:
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _group_exists(pgid):
        _signal_group(pgid, signal.SIGKILL)
    while _group_exists(pgid):
        time.sleep(0.02)


def _run_attempt(command: list[str], timeout: float, grace: float) -> int:
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    timed_out = process.poll() is None
    if timed_out or _group_exists(process.pid):
        _clean_group(process.pid, grace)
    process.wait()
    return 124 if timed_out else process.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--grace", type=float, default=15)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--delay", type=float, default=15)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.attempts < 1:
        parser.error("a command and at least one attempt are required")

    _enable_subreaper()
    for attempt in range(1, args.attempts + 1):
        status = _run_attempt(command, args.timeout, args.grace)
        if status == 0:
            return 0
        if attempt == args.attempts:
            print(f"command timed out or failed after {attempt} attempts", file=sys.stderr)
            return 1
        print(f"attempt {attempt} timed out or failed; retrying in {args.delay:g}s...", file=sys.stderr)
        time.sleep(args.delay)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
