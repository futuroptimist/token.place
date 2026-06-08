"""Process-level regression tests for the canonical ``server.py`` compute node."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from api.v1.encryption import EncryptionManager


REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_ok(url: str, *, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=0.5)
            if response.status_code == 200:
                return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {url}: {last_error}")


def _wait_for_compute_count(relay_url: str, expected: int, *, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{relay_url}/relay/diagnostics", timeout=1)
            response.raise_for_status()
            payload = response.json()
            last_payload = payload
            if payload.get("total_api_v1_registered_compute_nodes") == expected:
                return payload
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(
        f"Timed out waiting for {expected} compute nodes; last diagnostics={last_payload}"
    )


def _terminate_process(process: subprocess.Popen[str], *, timeout: float = 5.0) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            return process.communicate(timeout=timeout)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            return process.communicate(timeout=timeout)[0]
    return process.communicate(timeout=timeout)[0]


def _start_relay_process(relay_port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_PLACE_ENV": "testing",
            "ENVIRONMENT": "test",
            "USE_MOCK_LLM": "1",
            "CONTENT_MODERATION_MODE": "off",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "2",
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": "2",
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "8",
            "TOKENPLACE_PENDING_REQUEST_TTL_SECONDS": "8",
            "TOKENPLACE_TERMINAL_REQUEST_TTL_SECONDS": "8",
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "relay.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(relay_port),
            "--use_mock_llm",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _start_server_py_process(relay_url: str, server_port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_PLACE_ENV": "testing",
            "ENVIRONMENT": "test",
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_MAX_POLL_FAILURES": "200",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "2",
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": "2",
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "server.py",
            "--relay_url",
            relay_url,
            "--server_host",
            "127.0.0.1",
            "--server_port",
            str(server_port),
            "--use_mock_llm",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _encrypt_messages(messages: list[dict], relay_public_key: str) -> tuple[EncryptionManager, dict]:
    browser_crypto = EncryptionManager()
    encrypted = browser_crypto.encrypt_message(messages, relay_public_key)
    assert encrypted is not None
    return browser_crypto, {
        "ciphertext": encrypted["ciphertext"],
        "cipherkey": encrypted["cipherkey"],
        "iv": encrypted["iv"],
    }


def _decrypt_completion(browser_crypto: EncryptionManager, response_body: dict) -> dict:
    encrypted = response_body["data"]
    decrypted = browser_crypto.decrypt_message(
        {
            "ciphertext": base64.b64decode(encrypted["ciphertext"]),
            "iv": base64.b64decode(encrypted["iv"]),
        },
        base64.b64decode(encrypted["cipherkey"]),
    )
    assert decrypted is not None
    return json.loads(decrypted.decode("utf-8"))


@pytest.mark.skipif(
    os.environ.get("RUN_RELAY_REGISTRATION_TESTS") != "1",
    reason="process-level relay/server.py e2e test is opt-in; set RUN_RELAY_REGISTRATION_TESTS=1",
)
def test_server_py_cli_registers_counts_and_completes_api_v1_e2ee_work():
    relay_port = _free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"
    relay_process = _start_relay_process(relay_port)
    server_processes: list[subprocess.Popen[str]] = []
    process_logs: list[str] = []

    try:
        _wait_for_http_ok(f"{relay_url}/healthz", timeout=10)
        initial = _wait_for_compute_count(relay_url, 0, timeout=5)
        assert initial["registered_compute_nodes"] == []

        first_server = _start_server_py_process(relay_url, _free_port())
        server_processes.append(first_server)
        first_count = _wait_for_compute_count(relay_url, 1, timeout=10)
        assert len(first_count["api_v1_registered_compute_nodes"]) == 1

        relay_public_key = requests.get(f"{relay_url}/api/v1/public-key", timeout=2).json()[
            "public_key"
        ]
        browser_crypto, encrypted_messages = _encrypt_messages(
            [{"role": "user", "content": "hello from server.py cli e2e"}],
            relay_public_key,
        )
        response = requests.post(
            f"{relay_url}/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                "client_public_key": browser_crypto.public_key_b64,
                "messages": encrypted_messages,
                "metadata": {
                    "inference_target": "desktop_bridge_api_v1_e2ee",
                    "relay_path": "api_v1_e2ee",
                },
            },
            timeout=12,
        )
        assert response.status_code == 200, response.text
        completion = _decrypt_completion(browser_crypto, response.json())
        assert completion["choices"][0]["message"]["content"] == (
            "Mock Response: The capital of France is Paris."
        )

        second_server = _start_server_py_process(relay_url, _free_port())
        server_processes.append(second_server)
        second_count = _wait_for_compute_count(relay_url, 2, timeout=10)
        assert len(second_count["api_v1_registered_compute_nodes"]) == 2

        process_logs.append(_terminate_process(server_processes.pop(), timeout=5))
        _wait_for_compute_count(relay_url, 1, timeout=10)
        process_logs.append(_terminate_process(server_processes.pop(), timeout=5))
        _wait_for_compute_count(relay_url, 0, timeout=10)
    finally:
        for process in reversed(server_processes):
            process_logs.append(_terminate_process(process, timeout=5))
        relay_output = _terminate_process(relay_process, timeout=5)
        if relay_process.returncode not in (0, -15) and relay_process.returncode is not None:
            pytest.fail(f"relay.py exited with {relay_process.returncode}:\n{relay_output}")
        for output in process_logs:
            if "Traceback" in output:
                pytest.fail(f"server.py emitted traceback:\n{output}")
