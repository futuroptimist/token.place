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
RELAY_SERVER_LEASE_SECONDS = "120"
REGISTRATION_COUNT_TIMEOUT_SECONDS = 8.0


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


def _terminate_process(
    proc: subprocess.Popen[str], *, label: str, fail_on_sigkill: bool = False
) -> bool:
    if proc.poll() is not None:
        return False
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=8)
        return False
    except subprocess.TimeoutExpired:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        output = _process_output(proc)
        message = f"{label} required SIGKILL during shutdown:\n{output}"
        if fail_on_sigkill:
            raise AssertionError(message)
        return True


def _process_output(proc: subprocess.Popen[str]) -> str:
    log_path = getattr(proc, "_tokenplace_log_path", None)
    if isinstance(log_path, Path) and log_path.exists():
        return log_path.read_text(encoding="utf-8", errors="replace")
    if proc.stdout:
        return proc.stdout.read()
    return ""


def _assert_process_still_running(proc: subprocess.Popen[str], label: str) -> None:
    if proc.poll() is None:
        return
    stdout = _process_output(proc)
    raise AssertionError(f"{label} exited early with {proc.returncode}:\n{stdout}")


def _bind_failure(output: str) -> bool:
    return "address already in use" in output.lower() or "errno 98" in output.lower()


def _start_process_with_log(
    args: list[str], *, env: dict[str, str], log_path: Path
) -> subprocess.Popen[str]:
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            args,
            cwd=REPO_ROOT,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        log_handle.close()
    setattr(proc, "_tokenplace_log_path", log_path)
    return proc


def _start_relay(tmp_path: Path) -> tuple[subprocess.Popen[str], str]:
    last_error: Exception | None = None
    for attempt in range(5):
        relay_port = _free_port()
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
                "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": RELAY_SERVER_LEASE_SECONDS,
                "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": RELAY_SERVER_LEASE_SECONDS,
                "TOKENPLACE_MAX_POLL_FAILURES": "3",
                "TOKENPLACE_LOG_LEVEL": "WARNING",
                "PYTHONUNBUFFERED": "1",
            }
        )
        proc = _start_process_with_log(
            [
                sys.executable,
                "relay.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(relay_port),
                "--use_mock_llm",
            ],
            env=env,
            log_path=tmp_path / f"relay-{attempt}.log",
        )
        try:
            _wait_for_json(f"{relay_url}/relay/diagnostics", timeout=10)
            _assert_process_still_running(proc, "relay.py")
            return proc, relay_url
        except AssertionError as exc:
            last_error = exc
            output = _process_output(proc)
            _terminate_process(proc, label="relay.py")
            if not _bind_failure(output):
                raise
    raise AssertionError(f"relay.py failed to bind after retries: {last_error}")


def _server_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_PLACE_ENV": "testing",
            "ENVIRONMENT": "testing",
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_MAX_POLL_FAILURES": "3",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.1",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": RELAY_SERVER_LEASE_SECONDS,
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": RELAY_SERVER_LEASE_SECONDS,
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
    return env


def _start_server(
    relay_url: str, server_port: int, log_path: Path
) -> subprocess.Popen[str]:
    return _start_process_with_log(
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
        env=_server_env(),
        log_path=log_path,
    )


def _start_server_and_wait_for_count(
    relay_url: str, expected_count: int, tmp_path: Path, label: str
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(5):
        proc = _start_server(
            relay_url,
            _free_port(),
            tmp_path / f"server-{label}-{attempt}.log",
        )
        try:
            diagnostics = _wait_for_count(relay_url, expected_count, timeout=10)
            _assert_process_still_running(proc, label)
            return proc, diagnostics
        except AssertionError as exc:
            last_error = exc
            output = _process_output(proc)
            _terminate_process(proc, label=label, fail_on_sigkill=True)
            if not _bind_failure(output):
                raise
    raise AssertionError(f"{label} failed to bind after retries: {last_error}")


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
def test_server_py_compute_node_cli_registers_scales_and_handles_api_v1_e2ee_work(
    tmp_path: Path,
):
    relay_url = ""
    relay_proc: subprocess.Popen[str] | None = None
    server_proc_a: subprocess.Popen[str] | None = None
    server_proc_b: subprocess.Popen[str] | None = None

    try:
        relay_proc, relay_url = _start_relay(tmp_path)
        initial = _wait_for_count(relay_url, 0, timeout=5)
        assert initial["total_registered_compute_nodes"] == 0

        server_proc_a, diagnostics_one = _start_server_and_wait_for_count(
            relay_url, 1, tmp_path, "server.py A"
        )
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
        assert (
            response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"]
            == "distributed"
        )
        assert (
            response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"]
            == "distributed_relay_e2ee"
        )
        assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"
        completion = _decrypt_chat_completion(browser_crypto, response.json())
        assert completion["object"] == "chat.completion"
        content = completion["choices"][0]["message"]["content"]
        assert "Mock Response" in content

        server_proc_b, diagnostics_two = _start_server_and_wait_for_count(
            relay_url, 2, tmp_path, "server.py B"
        )
        assert diagnostics_two["total_registered_compute_nodes"] == 2

    finally:
        original_exception = sys.exc_info()[0]
        if original_exception is None:
            shutdown_failures: list[str] = []
            if server_proc_b is not None:
                if _terminate_process(server_proc_b, label="server.py B"):
                    shutdown_failures.append("server.py B required SIGKILL")
                _wait_for_count(
                    relay_url, 1, timeout=REGISTRATION_COUNT_TIMEOUT_SECONDS
                )
            if server_proc_a is not None:
                if _terminate_process(server_proc_a, label="server.py A"):
                    shutdown_failures.append("server.py A required SIGKILL")
                _wait_for_count(
                    relay_url, 0, timeout=REGISTRATION_COUNT_TIMEOUT_SECONDS
                )
            if shutdown_failures:
                raise AssertionError("; ".join(shutdown_failures))
        else:
            for proc, label in (
                (server_proc_b, "server.py B"),
                (server_proc_a, "server.py A"),
            ):
                if proc is not None:
                    _terminate_process(proc, label=label)
        if relay_proc is not None:
            _terminate_process(relay_proc, label="relay.py")
