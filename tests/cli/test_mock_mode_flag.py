"""Integration tests for the relay CLI mock mode flag."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
def test_cli_mock_flag_enables_mock_llm() -> None:
    project_root = Path(__file__).resolve().parents[2]
    port = _find_free_port()

    cmd = [
        sys.executable,
        "-m",
        "relay",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--use_mock_llm",
    ]

    env = os.environ.copy()
    env.pop("USE_MOCK_LLM", None)
    python_path_parts = [str(project_root)]
    if env.get("PYTHONPATH"):
        python_path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_parts)

    process = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    stdout_chunks: list[str] = []
    failure_message: str | None = None
    try:
        deadline = time.time() + 60
        health_checked = False
        while time.time() < deadline:
            if process.poll() is not None:
                failure_message = (
                    f"relay process exited early with code {process.returncode}"
                )
                break

            connection: http.client.HTTPConnection | None = None
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                payload = response.read().decode("utf-8")
            except (OSError, http.client.HTTPException):
                time.sleep(0.5)
                continue
            finally:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()

            if response.status in (200, 503):
                data = json.loads(payload)
                if data.get("status") in {"ok", "degraded"}:
                    health_checked = True
                    break

            time.sleep(0.5)

        if not health_checked:
            failure_message = "relay health check never succeeded with --use_mock_llm"

    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)

        try:
            stdout_data, _ = process.communicate(timeout=15)
            if stdout_data:
                stdout_chunks.append(stdout_data)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_data, _ = process.communicate(timeout=5)
            if stdout_data:
                stdout_chunks.append(stdout_data)

    combined_output = "".join(stdout_chunks)
    if failure_message:
        pytest.fail(f"{failure_message}:\n{combined_output}")
    assert "mock.llm.enabled" in combined_output, combined_output
    assert "USE_MOCK_LLM=True" in combined_output, combined_output
