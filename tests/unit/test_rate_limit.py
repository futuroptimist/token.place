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


def _make_rate_limited_app_with_operational_routes():
    app = Flask(__name__)
    init_app(app)

    app.add_url_rule("/healthz", "healthz", lambda: {"status": "ok"})
    app.add_url_rule("/livez", "livez", lambda: {"status": "alive"})
    app.add_url_rule(
        "/relay/diagnostics",
        "relay_diagnostics",
        lambda: {"status": "ok"},
    )
    app.add_url_rule(
        "/api/v1/relay/servers/register",
        "api_v1_relay_servers_register",
        lambda: {"registered": True},
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/v1/relay/servers/poll",
        "api_v1_relay_servers_poll",
        lambda: {"message": "No requests available"},
        methods=["POST"],
    )
    return app


@patch.dict(os.environ, {"API_RATE_LIMIT": "60/hour"}, clear=True)
def test_healthz_exempt_from_staging_default_rate_limit_after_many_calls():
    """Staging default 60/hour must not exhaust Kubernetes readiness checks."""
    app = _make_rate_limited_app_with_operational_routes()

    with app.test_client() as client:
        responses = [client.get("/healthz") for _ in range(120)]

    assert all(response.status_code == 200 for response in responses)
    assert not any(response.status_code == 429 for response in responses)


@patch.dict(os.environ, {"API_RATE_LIMIT": "60/hour"}, clear=True)
def test_kubernetes_probe_cadence_cannot_exhaust_healthz_quota():
    """A 10s kube-probe cadence produces 360 hits/hour and should remain ready."""
    app = _make_rate_limited_app_with_operational_routes()

    with app.test_client() as client:
        for _ in range(360):
            response = client.get(
                "/healthz",
                headers={"User-Agent": "kube-probe/1.29", "Accept": "*/*"},
            )
            assert response.status_code == 200


@patch.dict(os.environ, {"API_RATE_LIMIT": "60/hour"}, clear=True)
def test_operational_routes_are_exempt_from_public_api_quota():
    """Liveness, metrics, and relay diagnostics should not consume public API quota."""
    app = _make_rate_limited_app_with_operational_routes()

    with app.test_client() as client:
        for path in ("/livez", "/metrics", "/relay/diagnostics"):
            responses = [client.get(path) for _ in range(75)]
            assert all(response.status_code != 429 for response in responses), path


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/minute"}, clear=True)
def test_public_chat_completion_route_still_uses_default_rate_limit():
    """Public chat completions should keep OpenAI-style rate limiting."""
    app = _make_rate_limited_app_with_operational_routes()

    with app.test_client() as client:
        first_response = client.post("/api/v1/chat/completions", json={})
        limited_response = client.post("/api/v1/chat/completions", json={})

    assert first_response.status_code != 429
    assert limited_response.status_code == 429
    payload = limited_response.get_json()
    assert payload["error"]["code"] == "rate_limit_exceeded"


@patch.dict(os.environ, {"API_RATE_LIMIT": "60/hour"}, clear=True)
def test_api_v1_compute_node_register_and_poll_exempt_from_public_quota():
    """Compute-node heartbeat control-plane routes must not inherit 60/hour."""
    app = _make_rate_limited_app_with_operational_routes()

    with app.test_client() as client:
        register_responses = [
            client.post(
                "/api/v1/relay/servers/register", json={"server_public_key": "node"}
            )
            for _ in range(75)
        ]
        poll_responses = [
            client.post(
                "/api/v1/relay/servers/poll", json={"server_public_key": "node"}
            )
            for _ in range(75)
        ]

    assert all(response.status_code == 200 for response in register_responses)
    assert all(response.status_code == 200 for response in poll_responses)
