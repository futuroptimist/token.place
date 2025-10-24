
"""Unit tests for API rate limiting."""

import os
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


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/minute"})
def test_rate_limit_error_uses_openai_style_schema():
    """Rate limited responses should present a structured JSON error payload."""
    app = Flask(__name__)
    init_app(app)

    with app.test_client() as client:
        client.get("/api/v1/models")  # Warm up quota
        limited_response = client.get("/api/v1/models")

    assert limited_response.status_code == 429
    # Flask-Limiter sends HTML by default; ensure we standardise on JSON.
    payload = limited_response.get_json()
    assert payload is not None, "Expected JSON payload for rate limit errors"
    assert payload.get("error", {}).get("type") == "rate_limit_error"
    message = payload["error"].get("message", "")
    assert "rate limit" in message.lower()
    assert limited_response.headers.get("Retry-After") is not None
