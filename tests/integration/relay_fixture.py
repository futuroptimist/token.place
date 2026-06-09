"""Shared relay subprocess fixtures for integration tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
READY_PATHS = ("/api/v1/health", "/v1/health", "/v1/models")
POLL_INTERVAL_SECONDS = 0.25
REQUEST_TIMEOUT_SECONDS = 0.75
OUTPUT_TAIL_CHARS = 4000


@dataclass
class RelayReadinessDiagnostics:
    """Diagnostic context captured while waiting for relay readiness."""

    last_url: str | None = None
    last_status: int | None = None
    last_body_excerpt: str | None = None
    last_exception: str | None = None
    early_exit_returncode: int | None = None

    def format(self) -> str:
        return (
            "readiness diagnostics:\n"
            f"  last_url={self.last_url!r}\n"
            f"  last_status={self.last_status!r}\n"
            f"  last_body_excerpt={self.last_body_excerpt!r}\n"
            f"  last_exception={self.last_exception!r}\n"
            f"  early_exit_returncode={self.early_exit_returncode!r}"
        )


def _allocate_loopback_port() -> int:
    """Reserve and release an ephemeral loopback port for the relay fixture."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _relay_start_timeout_seconds() -> float:
    override = os.environ.get("TOKENPLACE_TEST_RELAY_START_TIMEOUT_SECONDS")
    if override:
        try:
            timeout = float(override)
        except ValueError:
            timeout = 0
        if timeout > 0:
            return timeout
    return 60.0 if sys.platform == "darwin" else 45.0


def _tail_file(file_obj: TextIO, limit: int = OUTPUT_TAIL_CHARS) -> str:
    file_obj.flush()
    with open(file_obj.name, encoding="utf-8", errors="replace") as handle:
        data = handle.read()
    return data[-limit:]


def _format_relay_failure(
    message: str,
    proc: subprocess.Popen[str],
    stdout_file: TextIO,
    stderr_file: TextIO,
    diagnostics: RelayReadinessDiagnostics,
) -> str:
    return (
        f"{message}\n"
        f"returncode={proc.poll()}\n"
        f"still_running={proc.poll() is None}\n"
        f"{diagnostics.format()}\n"
        f"STDOUT tail:\n{_tail_file(stdout_file)}\n"
        f"STDERR tail:\n{_tail_file(stderr_file)}"
    )


def _wait_for_relay_ready(
    base_url: str,
    proc: subprocess.Popen[str],
    stdout_file: TextIO,
    stderr_file: TextIO,
    description: str,
) -> None:
    diagnostics = RelayReadinessDiagnostics()
    deadline = time.monotonic() + _relay_start_timeout_seconds()

    while time.monotonic() < deadline:
        returncode = proc.poll()
        if returncode is not None:
            diagnostics.early_exit_returncode = returncode
            raise RuntimeError(
                _format_relay_failure(
                    f"relay exited before becoming healthy for {description}",
                    proc,
                    stdout_file,
                    stderr_file,
                    diagnostics,
                )
            )

        for path in READY_PATHS:
            url = f"{base_url}{path}"
            diagnostics.last_url = url
            try:
                response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                diagnostics.last_exception = f"{type(exc).__name__}: {exc}"
                diagnostics.last_status = None
                diagnostics.last_body_excerpt = None
                continue

            diagnostics.last_exception = None
            diagnostics.last_status = response.status_code
            diagnostics.last_body_excerpt = response.text[:500]
            if response.status_code == 200:
                return

        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        _format_relay_failure(
            f"relay was still running but did not become healthy for {description}",
            proc,
            stdout_file,
            stderr_file,
            diagnostics,
        )
    )


@contextmanager
def start_relay_with_mock(description: str) -> Iterator[str]:
    """Start relay.py in mock-LLM mode and yield its local base URL."""

    port = _allocate_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"
    cmd = [
        sys.executable,
        "relay.py",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--use_mock_llm",
    ]

    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                cmd,
                cwd=REPO_ROOT,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )
            try:
                _wait_for_relay_ready(
                    base_url, proc, stdout_file, stderr_file, description
                )
                yield base_url
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
