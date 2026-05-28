"""Unit tests for API rate limiting."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


@patch.dict(os.environ, {"TOKEN_PLACE_ENV": "production"}, clear=True)
def test_production_without_rate_limit_storage_uri_still_initializes_limiter():
    """Production should still boot with Flask-Limiter's default in-memory backend."""

    app = Flask(__name__)
    limiter_instance = MagicMock()

    with patch("api.Limiter", return_value=limiter_instance) as limiter_cls:
        limiter = init_app(app)

    assert limiter is limiter_instance
    assert "storage_uri" not in limiter_cls.call_args.kwargs


@patch.dict(
    os.environ,
    {
        "TOKEN_PLACE_ENV": "production",
        "TOKENPLACE_RATE_LIMIT_STORAGE_URI": "memcached://127.0.0.1:11211",
    },
    clear=True,
)
def test_production_with_rate_limit_storage_uri_uses_explicit_backend():
    """Optional storage URI should be passed through when configured."""

    app = Flask(__name__)
    limiter_instance = MagicMock()

    with patch("api.Limiter", return_value=limiter_instance) as limiter_cls:
        limiter = init_app(app)

    assert limiter is limiter_instance
    assert limiter_cls.call_args.kwargs["storage_uri"] == "memcached://127.0.0.1:11211"


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "60/hour", "API_DAILY_QUOTA": "10000/day"},
    clear=True,
)
def test_operational_endpoints_are_exempt_from_staging_default_rate_limit():
    """Staging health, metrics, and diagnostics routes must not consume API quota."""

    app = Flask(__name__)
    init_app(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/livez")
    def livez():
        return {"status": "alive"}

    @app.get("/relay/diagnostics")
    def relay_diagnostics():
        return {"registered_compute_nodes": []}

    with app.test_client() as client:
        for path in ("/healthz", "/livez", "/metrics", "/relay/diagnostics"):
            responses = [client.get(path) for _ in range(65)]
            assert all(response.status_code != 429 for response in responses), path


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "60/hour", "API_DAILY_QUOTA": "10000/day"},
    clear=True,
)
def test_public_chat_completion_route_still_uses_staging_default_rate_limit():
    """Public API traffic should still receive OpenAI-style 429s after quota exhaustion."""

    app = Flask(__name__)
    init_app(app)
    payload = {}

    with app.test_client() as client:
        for _ in range(60):
            response = client.post("/api/v1/chat/completions", json=payload)
            assert response.status_code != 429
        limited_response = client.post("/api/v1/chat/completions", json=payload)

    assert limited_response.status_code == 429
    body = limited_response.get_json()
    assert body is not None
    assert body["error"]["code"] == "rate_limit_exceeded"


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "60/hour", "API_DAILY_QUOTA": "10000/day"},
    clear=True,
)
def test_api_v1_relay_control_plane_routes_are_exempt_from_public_api_quota():
    """Compute-node heartbeat/control-plane routes should not inherit public user limits."""

    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def register():
        return {"next_ping_in_x_seconds": 10}

    @app.post("/api/v1/relay/servers/poll")
    def poll():
        return {"message": "No requests available"}

    @app.get("/api/v1/relay/servers/next")
    def next_server():
        return {"server_public_key": "test"}

    @app.post("/api/v1/relay/responses")
    def responses():
        return {"message": "Response received and queued for client"}

    @app.post("/api/v1/relay/responses/retrieve")
    def responses_retrieve():
        return {"status": "pending"}, 202

    with app.test_client() as client:
        route_calls = (
            ("post", "/api/v1/relay/servers/register"),
            ("post", "/api/v1/relay/servers/poll"),
            ("get", "/api/v1/relay/servers/next"),
            ("post", "/api/v1/relay/responses"),
            ("post", "/api/v1/relay/responses/retrieve"),
        )
        for method, path in route_calls:
            responses = [
                getattr(client, method)(path, json={})
                for _ in range(65)
            ]
            assert all(response.status_code != 429 for response in responses), path
