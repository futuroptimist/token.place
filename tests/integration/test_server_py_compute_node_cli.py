"""E2E coverage for repository-root ``server.py`` as a headless compute node."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import pytest
import requests

from encrypt import decrypt, encrypt, generate_keys


REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_RELAY_REGISTRATION_TESTS") != "1",
    reason=(
        "server.py CLI relay registration e2e is process-heavy; "
        "set RUN_RELAY_REGISTRATION_TESTS=1 to enable"
    ),
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 15.0,
    interval: float = 0.1,
    description: str = "condition",
) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # pragma: no cover - failure diagnostics path
            last_error = exc
        time.sleep(interval)
    if last_error is not None:
        raise AssertionError(f"Timed out waiting for {description}: {last_error}") from last_error
    raise AssertionError(f"Timed out waiting for {description}")


def _base_env(relay_url: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_PLACE_ENV": "testing",
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_MAX_POLL_FAILURES": "12",
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.2",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "2",
            "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS": "2",
            "TOKENPLACE_PENDING_REQUEST_TTL_SECONDS": "5",
            "TOKENPLACE_TERMINAL_REQUEST_TTL_SECONDS": "5",
            "TOKENPLACE_API_V1_IN_FLIGHT_TTL_SECONDS": "5",
            "CONTENT_MODERATION_MODE": "off",
        }
    )
    if relay_url is not None:
        env.update(
            {
                "TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED": "1",
                "TOKENPLACE_API_V1_COMPUTE_PROVIDER": "distributed",
                "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK": "0",
                "TOKENPLACE_DISTRIBUTED_COMPUTE_URL": relay_url,
                "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "10",
                "TOKENPLACE_RELAY_URL": relay_url,
            }
        )
    return env


def _start_process(
    command: list[str], *, env: dict[str, str], log_prefix: str
) -> tuple[
    subprocess.Popen[str],
    tempfile.NamedTemporaryFile,
    tempfile.NamedTemporaryFile,
]:
    stdout = tempfile.NamedTemporaryFile("w+", prefix=f"{log_prefix}-stdout-", suffix=".log")
    stderr = tempfile.NamedTemporaryFile("w+", prefix=f"{log_prefix}-stderr-", suffix=".log")
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    return proc, stdout, stderr


def _process_logs(
    stdout: tempfile.NamedTemporaryFile, stderr: tempfile.NamedTemporaryFile
) -> str:
    logs: list[str] = []
    for label, handle in (("stdout", stdout), ("stderr", stderr)):
        try:
            handle.flush()
            handle.seek(0)
            content = handle.read()
        except Exception as exc:  # pragma: no cover - failure diagnostics path
            content = f"<failed to read {label}: {exc}>"
        logs.append(f"--- {label} ---\n{content}")
    return "\n".join(logs)


def _assert_process_running(
    proc: subprocess.Popen[str], stdout, stderr, label: str
) -> None:
    if proc.poll() is not None:
        raise AssertionError(
            f"{label} exited early with code {proc.returncode}\n{_process_logs(stdout, stderr)}"
        )


def _stop_process(
    proc: subprocess.Popen[str], *, graceful_signal: signal.Signals = signal.SIGINT
) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(graceful_signal)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _diagnostics(base_url: str) -> dict:
    response = requests.get(f"{base_url}/relay/diagnostics", timeout=3)
    response.raise_for_status()
    return response.json()


def _api_v1_count(base_url: str) -> int:
    payload = _diagnostics(base_url)
    return int(payload.get("total_api_v1_registered_compute_nodes", -1))


def _wait_for_count(base_url: str, expected: int, *, timeout: float = 15.0) -> None:
    _wait_until(
        lambda: _api_v1_count(base_url) == expected,
        timeout=timeout,
        description=f"relay API v1 compute-node count == {expected}",
    )


def _encrypted_chat_completion(base_url: str) -> dict:
    public_key_response = requests.get(f"{base_url}/api/v1/public-key", timeout=3)
    public_key_response.raise_for_status()
    relay_public_key_b64 = public_key_response.json()["public_key"]

    client_private_key, client_public_key = generate_keys()
    client_public_key_b64 = base64.b64encode(client_public_key).decode("utf-8")
    messages = [{"role": "user", "content": "ping from server.py cli e2e"}]
    ciphertext_dict, cipherkey, iv = encrypt(
        json.dumps(messages).encode("utf-8"),
        base64.b64decode(relay_public_key_b64),
        use_pkcs1v15=True,
    )

    response = requests.post(
        f"{base_url}/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": client_public_key_b64,
            "messages": {
                "ciphertext": base64.b64encode(ciphertext_dict["ciphertext"]).decode(
                    "utf-8"
                ),
                "cipherkey": base64.b64encode(cipherkey).decode("utf-8"),
                "iv": base64.b64encode(iv).decode("utf-8"),
            },
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        },
        timeout=12,
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"

    response_body = response.json()
    assert response_body["encrypted"] is True
    encrypted = response_body["data"]
    decrypted = decrypt(
        {
            "ciphertext": base64.b64decode(encrypted["ciphertext"]),
            "iv": base64.b64decode(encrypted["iv"]),
        },
        base64.b64decode(encrypted["cipherkey"]),
        client_private_key,
    )
    return json.loads(decrypted.decode("utf-8"))


def test_compute_node_cli_server_py_registers_counts_and_completes_api_v1_work():
    relay_port = _free_port()
    server_port_one = _free_port()
    server_port_two = _free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"

    processes: list[
        tuple[
            subprocess.Popen[str],
            tempfile.NamedTemporaryFile,
            tempfile.NamedTemporaryFile,
            str,
        ]
    ] = []
    try:
        relay_proc, relay_stdout, relay_stderr = _start_process(
            [
                sys.executable,
                "relay.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(relay_port),
                "--use_mock_llm",
            ],
            env=_base_env(relay_url),
            log_prefix="relay-py",
        )
        processes.append((relay_proc, relay_stdout, relay_stderr, "relay.py"))
        _wait_until(
            lambda: requests.get(f"{relay_url}/healthz", timeout=1).status_code == 200,
            timeout=15,
            description="relay.py healthz",
        )
        _assert_process_running(relay_proc, relay_stdout, relay_stderr, "relay.py")
        _wait_for_count(relay_url, 0, timeout=5)

        server_one, server_one_stdout, server_one_stderr = _start_process(
            [
                sys.executable,
                "server.py",
                "--relay_url",
                relay_url,
                "--server_host",
                "127.0.0.1",
                "--server_port",
                str(server_port_one),
                "--use_mock_llm",
            ],
            env=_base_env(relay_url),
            log_prefix="server-py-one",
        )
        processes.append((server_one, server_one_stdout, server_one_stderr, "server.py one"))
        _wait_until(
            lambda: requests.get(
                f"http://127.0.0.1:{server_port_one}/health", timeout=1
            ).status_code
            == 200,
            timeout=15,
            description="first server.py health",
        )
        _wait_for_count(relay_url, 1)

        completion = _encrypted_chat_completion(relay_url)
        assert completion["object"] == "chat.completion"
        assert completion["choices"][0]["message"]["role"] == "assistant"
        assert "Mock Response" in completion["choices"][0]["message"]["content"]

        server_two, server_two_stdout, server_two_stderr = _start_process(
            [
                sys.executable,
                "server.py",
                "--relay_url",
                relay_url,
                "--server_host",
                "127.0.0.1",
                "--server_port",
                str(server_port_two),
                "--use_mock_llm",
            ],
            env=_base_env(relay_url),
            log_prefix="server-py-two",
        )
        processes.append((server_two, server_two_stdout, server_two_stderr, "server.py two"))
        _wait_until(
            lambda: requests.get(
                f"http://127.0.0.1:{server_port_two}/health", timeout=1
            ).status_code
            == 200,
            timeout=15,
            description="second server.py health",
        )
        _wait_for_count(relay_url, 2)

        _stop_process(server_two)
        _wait_until(
            lambda: _api_v1_count(relay_url) <= 1,
            timeout=8,
            description="relay count shrinks after second server.py stops",
        )
        _stop_process(server_one)
        _wait_for_count(relay_url, 0, timeout=8)
    finally:
        for proc, stdout, stderr, _label in reversed(processes):
            _stop_process(proc)
            stdout.close()
            stderr.close()
