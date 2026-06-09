import requests

from tests.integration.relay_fixture import start_relay_with_mock


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
    with start_relay_with_mock("DSPACE compatibility test") as base_url:
        response = _post_dspace_chat(base_url, _base_dspace_payload())

        assert response.status_code == 200, response.text
        data = response.json()

        assert data["model"] == "gpt-5-chat-latest"
        assert data["choices"], (
            "Expected at least one choice in the completion response"
        )
        message = data["choices"][0]["message"]
        assert message["role"] == "assistant"
        assert isinstance(message["content"], str) and message["content"].strip()


def test_dspace_receives_usage_metrics():
    """DSPACE relies on usage counters to show token consumption to users."""

    with start_relay_with_mock("DSPACE compatibility test") as base_url:
        payload = _base_dspace_payload()
        payload["metadata"] = {"client": "dspace", "conversation_id": "demo"}

        response = _post_dspace_chat(base_url, payload)

        assert response.status_code == 200, response.text
        data = response.json()

        usage = data.get("usage")
        assert isinstance(usage, dict), (
            "Usage payload missing from chat completion response"
        )

        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

        assert isinstance(prompt_tokens, int) and prompt_tokens >= 0
        assert isinstance(completion_tokens, int) and completion_tokens >= 0
        assert (
            isinstance(total_tokens, int)
            and total_tokens == prompt_tokens + completion_tokens
        )


def test_dspace_metadata_round_trip():
    """Metadata should round-trip so DSPACE can track the active conversation."""

    with start_relay_with_mock("DSPACE compatibility test") as base_url:
        payload = _base_dspace_payload()
        payload["metadata"] = {"client": "dspace", "conversation_id": "conv-42"}

        response = _post_dspace_chat(base_url, payload)

        assert response.status_code == 200, response.text
        body = response.json()

        assert body.get("metadata") == payload["metadata"], (
            "Chat completion should echo request metadata for caller correlation"
        )
