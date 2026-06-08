"""Process-level e2e coverage for the canonical server.py compute-node CLI."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from api.v1.encryption import EncryptionManager

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_json(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
        except Exception as exc:  # pragma: no cover - diagnostic path only
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for JSON from {url}: {last_error}")


def _wait_for_count(
    base_url: str, expected: int, *, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        last_payload = _wait_for_json(f"{base_url}/relay/diagnostics", timeout=2)
        if last_payload.get("total_api_v1_registered_compute_nodes") == expected:
            return last_payload
        time.sleep(0.1)
    raise AssertionError(
        f"expected {expected} API v1 compute nodes; last diagnostics={last_payload}"
    )


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _assert_process_still_running(proc: subprocess.Popen[str], label: str) -> None:
    if proc.poll() is None:
        return
    stdout = proc.stdout.read() if proc.stdout else ""
    raise AssertionError(f"{label} exited early with {proc.returncode}:\n{stdout}")


def _start_relay(relay_port: int) -> subprocess.Popen[str]:
    relay_url = f"http://127.0.0.1:{relay_port}"
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_PLACE_ENV": "testing",
            "ENVIRONMENT": "testing",
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER": "distributed",
            "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK": "0",
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL": relay_url,
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "10",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.1",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "2",
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": "2",
            "TOKENPLACE_MAX_POLL_FAILURES": "3",
            "TOKENPLACE_LOG_LEVEL": "WARNING",
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen(
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
    _wait_for_json(f"{relay_url}/relay/diagnostics", timeout=10)
    _assert_process_still_running(proc, "relay.py")
    return proc


def _start_server(relay_url: str, server_port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_PLACE_ENV": "testing",
            "ENVIRONMENT": "testing",
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_MAX_POLL_FAILURES": "3",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.1",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "2",
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": "2",
            "PYTHONUNBUFFERED": "1",
        }
    )
    for key in (
        "TOKENPLACE_RELAY_URL",
        "TOKEN_PLACE_RELAY_URL",
        "TOKENPLACE_RELAY_BASE_URL",
        "TOKEN_PLACE_RELAY_BASE_URL",
        "TOKENPLACE_RELAY_UPSTREAM_URL",
        "TOKEN_PLACE_RELAY_UPSTREAM_URL",
        "RELAY_URL",
        "TOKENPLACE_RELAY_PORT",
        "TOKEN_PLACE_RELAY_PORT",
        "RELAY_PORT",
    ):
        env.pop(key, None)

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


def _encrypt_messages_for_relay(
    relay_url: str, browser_crypto: EncryptionManager, messages: list[dict[str, str]]
) -> dict[str, str]:
    public_key_response = requests.get(f"{relay_url}/api/v1/public-key", timeout=5)
    assert public_key_response.status_code == 200, public_key_response.text
    relay_public_key = public_key_response.json()["public_key"]
    encrypted = browser_crypto.encrypt_message(messages, relay_public_key)
    assert encrypted is not None
    return {
        "ciphertext": encrypted["ciphertext"],
        "cipherkey": encrypted["cipherkey"],
        "iv": encrypted["iv"],
    }


def _decrypt_chat_completion(
    browser_crypto: EncryptionManager, response_body: dict[str, Any]
) -> dict[str, Any]:
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


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_RELAY_REGISTRATION_TESTS") != "1",
    reason="set RUN_RELAY_REGISTRATION_TESTS=1 to launch relay.py and server.py subprocesses",
)
def test_server_py_compute_node_cli_registers_scales_and_handles_api_v1_e2ee_work():
    relay_port = _free_port()
    server_port_a = _free_port()
    server_port_b = _free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"
    relay_proc: subprocess.Popen[str] | None = None
    server_proc_a: subprocess.Popen[str] | None = None
    server_proc_b: subprocess.Popen[str] | None = None

    try:
        relay_proc = _start_relay(relay_port)
        initial = _wait_for_count(relay_url, 0, timeout=5)
        assert initial["total_registered_compute_nodes"] == 0

        server_proc_a = _start_server(relay_url, server_port_a)
        diagnostics_one = _wait_for_count(relay_url, 1, timeout=10)
        _assert_process_still_running(server_proc_a, "server.py A")
        assert diagnostics_one["total_registered_compute_nodes"] == 1

        browser_crypto = EncryptionManager()
        user_text = "ping from server.py compute node"
        response = requests.post(
            f"{relay_url}/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                "client_public_key": browser_crypto.public_key_b64,
                "messages": _encrypt_messages_for_relay(
                    relay_url,
                    browser_crypto,
                    [{"role": "user", "content": user_text}],
                ),
                "metadata": {
                    "inference_target": "desktop_bridge_api_v1_e2ee",
                    "relay_path": "api_v1_e2ee",
                },
            },
            timeout=15,
        )
        assert response.status_code == 200, response.text
        completion = _decrypt_chat_completion(browser_crypto, response.json())
        assert completion["object"] == "chat.completion"
        content = completion["choices"][0]["message"]["content"]
        assert "Mock Response" in content

        server_proc_b = _start_server(relay_url, server_port_b)
        diagnostics_two = _wait_for_count(relay_url, 2, timeout=10)
        _assert_process_still_running(server_proc_b, "server.py B")
        assert diagnostics_two["total_registered_compute_nodes"] == 2

    finally:
        for proc in (server_proc_b, server_proc_a):
            if proc is not None:
                _terminate_process(proc)
        if relay_proc is not None:
            try:
                _wait_for_count(relay_url, 0, timeout=8)
            finally:
                _terminate_process(relay_proc)
