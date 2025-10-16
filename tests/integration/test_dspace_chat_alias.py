import os
import time
import subprocess
import requests
import sys
from contextlib import contextmanager

API_PORT = 5056
BASE_URL = f"http://localhost:{API_PORT}"


@contextmanager
def start_relay_with_mock():
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"
    cmd = [sys.executable, "relay.py", "--port", str(API_PORT)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(10):
            try:
                response = requests.get(f"{BASE_URL}/v1/health", timeout=1)
                if response.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("relay failed to start for DSPACE compatibility test")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _post_dspace_chat(payload: dict[str, object]) -> requests.Response:
    """Helper to send a DSPACE-style chat completion request."""

    return requests.post(
        f"{BASE_URL}/api/v1/chat/completions",
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
    with start_relay_with_mock():
        response = _post_dspace_chat(_base_dspace_payload())

        assert response.status_code == 200, response.text
        data = response.json()

        assert data["model"] == "gpt-5-chat-latest"
        assert data["choices"], "Expected at least one choice in the completion response"
        message = data["choices"][0]["message"]
        assert message["role"] == "assistant"
        assert isinstance(message["content"], str) and message["content"].strip()


def test_dspace_receives_usage_metrics():
    """DSPACE relies on usage counters to show token consumption to users."""

    with start_relay_with_mock():
        payload = _base_dspace_payload()
        payload["metadata"] = {"client": "dspace", "conversation_id": "demo"}

        response = _post_dspace_chat(payload)

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
