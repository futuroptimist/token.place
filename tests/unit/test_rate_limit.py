
"""Unit tests for API rate limiting."""

import os
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from api import init_app


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/minute"})
def test_exceeding_api_rate_limit_returns_429():
    """Second rapid request should hit the rate limit."""
    app = Flask(__name__)
    init_app(app)

    with app.test_client() as client:
        assert client.get("/api/v1/models").status_code == 200
        assert client.get("/api/v1/models").status_code == 429


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/minute"}, clear=True)
def test_rate_limit_uses_openai_style_error_payload():
    """Rate limit responses should be JSON with Retry-After metadata."""
    app = Flask(__name__)
    init_app(app)

    with app.test_client() as client:
        assert client.get("/api/v1/models").status_code == 200
        response = client.get("/api/v1/models")

    assert response.status_code == 429
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit()

    payload = response.get_json()
    assert payload is not None
    assert payload["error"]["type"] == "rate_limit_error"
    assert payload["error"]["code"] == "rate_limit_exceeded"
    assert "rate limit exceeded" in payload["error"]["message"].lower()


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "100/minute", "API_STREAM_RATE_LIMIT": "1/minute"},
    clear=True,
)
def test_streaming_chat_completion_requests_are_rate_limited(monkeypatch):
    """Streaming chat completions should have a tighter dedicated rate limit."""

    app = Flask(__name__)
    init_app(app)

    monkeypatch.setattr(
        "api.v2.routes.get_model_instance",
        lambda model_id: object(),
    )
    monkeypatch.setattr(
        "api.v2.routes.generate_response",
        lambda model_id, messages, **kwargs: [
            *messages,
            {"role": "assistant", "content": "Hello from token.place"},
        ],
    )
    monkeypatch.setattr(
        "api.v2.routes.evaluate_messages_for_policy",
        lambda messages: SimpleNamespace(allowed=True),
    )

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }

    with app.test_client() as client:
        first_response = client.post(
            "/api/v2/chat/completions",
            json=payload,
            headers={"Accept": "text/event-stream"},
        )
        assert first_response.status_code == 200

        limited_response = client.post(
            "/api/v2/chat/completions",
            json=payload,
            headers={"Accept": "text/event-stream"},
        )

    assert limited_response.status_code == 429
    body = limited_response.get_json()
    assert body is not None
    assert body["error"]["code"] == "rate_limit_exceeded"
