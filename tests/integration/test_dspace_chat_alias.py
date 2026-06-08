import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]


def _allocate_loopback_port() -> int:
    """Reserve and release an ephemeral loopback port for the relay fixture."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _captured_process_output(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    return stdout or "", stderr or ""


def _format_relay_failure(message: str, proc: subprocess.Popen[str]) -> str:
    stdout, stderr = _captured_process_output(proc)
    return (
        f"{message}\n"
        f"returncode={proc.returncode}\n"
        f"STDOUT:\n{stdout[-4000:]}\n"
        f"STDERR:\n{stderr[-4000:]}"
    )


@contextmanager
def start_relay_with_mock() -> Iterator[str]:
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
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(10):
            returncode = proc.poll()
            if returncode is not None:
                raise RuntimeError(
                    _format_relay_failure(
                        "relay exited before becoming healthy for DSPACE compatibility test",
                        proc,
                    )
                )
            try:
                response = requests.get(f"{base_url}/v1/health", timeout=1)
                if response.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            proc.terminate()
            raise RuntimeError(
                _format_relay_failure(
                    "relay failed to start for DSPACE compatibility test", proc
                )
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


def _post_dspace_chat(base_url: str, payload: dict[str, object]) -> requests.Response:
    """Helper to send a DSPACE-style chat completion request."""

    return requests.post(
        f"{base_url}/api/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        timeout=10,
    )


def _base_dspace_payload() -> dict[str, object]:
    return {
        "model": "gpt-5-chat-latest",
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant embedded inside DSPACE.",
            },
            {
                "role": "user",
                "content": "Hello there!",
            },
        ],
    }


def test_dspace_can_request_gpt5_alias():
    with start_relay_with_mock() as base_url:
        response = _post_dspace_chat(base_url, _base_dspace_payload())

        assert response.status_code == 200, response.text
        data = response.json()

        assert data["model"] == "gpt-5-chat-latest"
        assert data["choices"], "Expected at least one choice in the completion response"
        message = data["choices"][0]["message"]
        assert message["role"] == "assistant"
        assert isinstance(message["content"], str) and message["content"].strip()


def test_dspace_receives_usage_metrics():
    """DSPACE relies on usage counters to show token consumption to users."""

    with start_relay_with_mock() as base_url:
        payload = _base_dspace_payload()
        payload["metadata"] = {"client": "dspace", "conversation_id": "demo"}

        response = _post_dspace_chat(base_url, payload)

        assert response.status_code == 200, response.text
        data = response.json()

        usage = data.get("usage")
        assert isinstance(usage, dict), "Usage payload missing from chat completion response"

        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

        assert isinstance(prompt_tokens, int) and prompt_tokens >= 0
        assert isinstance(completion_tokens, int) and completion_tokens >= 0
        assert isinstance(total_tokens, int) and total_tokens == prompt_tokens + completion_tokens


def test_dspace_metadata_round_trip():
    """Metadata should round-trip so DSPACE can track the active conversation."""

    with start_relay_with_mock() as base_url:
        payload = _base_dspace_payload()
        payload["metadata"] = {"client": "dspace", "conversation_id": "conv-42"}

        response = _post_dspace_chat(base_url, payload)

        assert response.status_code == 200, response.text
        body = response.json()

        assert body.get("metadata") == payload["metadata"], (
            "Chat completion should echo request metadata for caller correlation"
        )
