"""E2E coverage for repository-root server.py as a headless compute node."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from api.v1.compute_provider import DistributedApiV1ComputeProvider


REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.1) -> Any:
    deadline = time.time() + timeout
    last_value: Any = None
    while time.time() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    return last_value


def _wait_for_http_ok(url: str, *, timeout: float = 10.0) -> None:
    def _ready() -> bool:
        try:
            response = requests.get(url, timeout=1.0)
        except requests.RequestException:
            return False
        return response.status_code == 200

    assert _wait_until(_ready, timeout=timeout), f"Timed out waiting for {url}"


def _diagnostics(base_url: str) -> dict[str, Any]:
    response = requests.get(f"{base_url}/relay/diagnostics", timeout=2.0)
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _api_v1_count(base_url: str) -> int:
    return int(_diagnostics(base_url).get("total_api_v1_registered_compute_nodes", -1))


def _wait_for_api_v1_count(base_url: str, expected: int, *, timeout: float = 10.0) -> None:
    def _matches() -> bool:
        try:
            return _api_v1_count(base_url) == expected
        except (requests.RequestException, AssertionError, ValueError):
            return False

    assert _wait_until(_matches, timeout=timeout), (
        f"Timed out waiting for {expected} API v1 compute nodes; "
        f"last diagnostics={_diagnostics(base_url)}"
    )


def _process_output(process: subprocess.Popen[str]) -> str:
    stdout = ""
    stderr = ""
    if process.stdout is not None:
        try:
            stdout = process.stdout.read() or ""
        except Exception:
            stdout = "<stdout unavailable>"
    if process.stderr is not None:
        try:
            stderr = process.stderr.read() or ""
        except Exception:
            stderr = "<stderr unavailable>"
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _start_relay(relay_port: int, env: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "relay.py", "--host", "127.0.0.1", "--port", str(relay_port), "--use_mock_llm"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_http_ok(f"http://127.0.0.1:{relay_port}/", timeout=10.0)
    except Exception:
        _terminate_process(process)
        raise AssertionError(f"relay.py failed to start\n{_process_output(process)}")
    return process


def _start_server(relay_port: int, server_port: int, env: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "server.py",
            "--relay_url",
            f"http://127.0.0.1:{relay_port}",
            "--server_host",
            "127.0.0.1",
            "--server_port",
            str(server_port),
            "--use_mock_llm",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_http_ok(f"http://127.0.0.1:{server_port}/health", timeout=10.0)
    except Exception:
        _terminate_process(process)
        raise AssertionError(f"server.py failed to start\n{_process_output(process)}")
    return process


@pytest.mark.integration
def test_server_py_cli_registers_counts_processes_api_v1_encrypted_work():
    if os.environ.get("RUN_RELAY_REGISTRATION_TESTS", "0") != "1":
        pytest.skip(
            "server.py CLI compute-node e2e is process-heavy; set "
            "RUN_RELAY_REGISTRATION_TESTS=1 to enable."
        )

    relay_port = _free_port()
    server_port_one = _free_port()
    server_port_two = _free_port()
    relay_base_url = f"http://127.0.0.1:{relay_port}"

    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "TOKEN_PLACE_ENV": "testing",
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_MAX_POLL_FAILURES": "4",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "2",
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": "2",
            "TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS": "2",
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "8",
        }
    )

    relay_process: subprocess.Popen[str] | None = None
    server_one: subprocess.Popen[str] | None = None
    server_two: subprocess.Popen[str] | None = None

    try:
        relay_process = _start_relay(relay_port, env)
        _wait_for_api_v1_count(relay_base_url, 0, timeout=3.0)

        server_one = _start_server(relay_port, server_port_one, env)
        _wait_for_api_v1_count(relay_base_url, 1, timeout=10.0)

        provider = DistributedApiV1ComputeProvider(base_url=relay_base_url, timeout_seconds=8.0)
        assistant_message = provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "What is the capital of France?"}],
            options={"stream": False},
        )
        assert assistant_message["role"] == "assistant"
        assert "Mock Response" in assistant_message["content"]
        assert "Paris" in assistant_message["content"]

        server_two = _start_server(relay_port, server_port_two, env)
        _wait_for_api_v1_count(relay_base_url, 2, timeout=10.0)

        _terminate_process(server_one)
        server_one = None
        _wait_for_api_v1_count(relay_base_url, 1, timeout=10.0)

        _terminate_process(server_two)
        server_two = None
        _wait_for_api_v1_count(relay_base_url, 0, timeout=10.0)
    finally:
        for process in (server_two, server_one, relay_process):
            if process is not None:
                _terminate_process(process)
