"""Unit tests for API rate limiting."""

import os
import sys
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
    {"API_RATE_LIMIT": "60/hour", "API_DAILY_QUOTA": "1000/day"},
    clear=True,
)
def test_staging_healthz_is_exempt_from_default_public_rate_limit():
    """Staging's default 60/hour quota must not block readiness probes."""
    app = Flask(__name__)
    init_app(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    with app.test_client() as client:
        responses = [client.get("/healthz") for _ in range(125)]

    assert {response.status_code for response in responses} == {200}


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "60/hour", "API_DAILY_QUOTA": "1000/day"},
    clear=True,
)
def test_kubernetes_probe_cadence_cannot_exhaust_healthz_quota():
    """A kube-probe every 10 seconds exceeds 60/hour but /healthz stays ready."""
    app = Flask(__name__)
    init_app(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    with app.test_client() as client:
        # Simulate a little over one hour of 10-second readiness probes.
        responses = [
            client.get("/healthz", headers={"User-Agent": "kube-probe/1.29"})
            for _ in range(361)
        ]

    assert {response.status_code for response in responses} == {200}


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "1000/day"},
    clear=True,
)
def test_operational_routes_are_exempt_from_public_rate_limit():
    """Operational endpoints should not consume or inherit the user API quota."""
    app = Flask(__name__)
    init_app(app)

    @app.get("/livez")
    def livez():
        return {"status": "alive"}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/relay/diagnostics")
    def relay_diagnostics():
        return {"status": "ok"}

    with app.test_client() as client:
        for path in ("/livez", "/healthz", "/metrics", "/relay/diagnostics"):
            statuses = [client.get(path).status_code for _ in range(3)]
            assert statuses == [200, 200, 200]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "60/hour",
        "API_DAILY_QUOTA": "1000/day",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_authenticated_api_v1_relay_heartbeat_routes_do_not_inherit_public_quota(monkeypatch):
    """Authenticated compute-node heartbeats must not consume user API quota."""
    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(SERVER_REGISTRATION_TOKENS=["relay-token"]),
    )
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    @app.post("/api/v1/relay/servers/poll")
    def relay_servers_poll():
        return {"status": "polling"}

    with app.test_client() as client:
        for path in ("/api/v1/relay/servers/register", "/api/v1/relay/servers/poll"):
            responses = [
                client.post(
                    path,
                    json={"server_public_key": "server"},
                    headers={"X-Relay-Server-Token": "relay-token"},
                )
                for _ in range(65)
            ]
            assert {response.status_code for response in responses} == {200}


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "60/hour", "API_DAILY_QUOTA": "1000/day"},
    clear=True,
)
def test_api_v1_relay_client_read_routes_do_not_inherit_public_quota():
    """Client discovery/retrieval polling should not consume user API quota."""
    app = Flask(__name__)
    init_app(app)

    @app.get("/api/v1/relay/servers/next")
    def relay_servers_next():
        return {"server_public_key": "server"}

    @app.post("/api/v1/relay/responses/retrieve")
    def relay_responses_retrieve():
        return {"status": "pending"}, 202

    with app.test_client() as client:
        next_responses = [client.get("/api/v1/relay/servers/next") for _ in range(65)]
        retrieve_responses = [
            client.post(
                "/api/v1/relay/responses/retrieve",
                json={"client_public_key": "client", "request_id": "request"},
            )
            for _ in range(65)
        ]

    assert {response.status_code for response in next_responses} == {200}
    assert {response.status_code for response in retrieve_responses} == {202}


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "2/hour",
        "API_DAILY_QUOTA": "1000/day",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "rotated-token",
    },
    clear=True,
)
def test_authenticated_relay_exemption_uses_loaded_relay_token_snapshot(monkeypatch):
    """Limiter auth should not drift from relay.py's active token snapshot."""
    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(SERVER_REGISTRATION_TOKENS=["active-token"]),
    )
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    with app.test_client() as client:
        rotated_responses = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": f"server-{index}"},
                headers={"X-Relay-Server-Token": "rotated-token"},
            )
            for index in range(3)
        ]
        active_responses = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": f"active-server-{index}"},
                headers={"X-Relay-Server-Token": "active-token"},
            )
            for index in range(3)
        ]

    assert [response.status_code for response in rotated_responses] == [200, 200, 429]
    assert {response.status_code for response in active_responses} == {200}


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "2/hour", "API_DAILY_QUOTA": "1000/day"},
    clear=True,
)
def test_unauthenticated_api_v1_relay_mutations_keep_public_rate_limit():
    """Anonymous relay mutations should retain quota protection when no token exists."""
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    with app.test_client() as client:
        assert (
            client.post(
                "/api/v1/relay/servers/register", json={"server_public_key": "server-1"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/relay/servers/register", json={"server_public_key": "server-2"}
            ).status_code
            == 200
        )
        response = client.post(
            "/api/v1/relay/servers/register", json={"server_public_key": "server-3"}
        )

    assert response.status_code == 429
    body = response.get_json()
    assert body is not None
    assert body["error"]["code"] == "rate_limit_exceeded"


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "2/hour", "API_DAILY_QUOTA": "1000/day"},
    clear=True,
)
def test_public_chat_completion_route_still_uses_public_rate_limit():
    """Public chat traffic should still receive OpenAI-style 429 responses."""
    app = Flask(__name__)
    init_app(app)

    with app.test_client() as client:
        assert client.post("/api/v1/chat/completions", json={}).status_code != 429
        assert client.post("/api/v1/chat/completions", json={}).status_code != 429
        response = client.post("/api/v1/chat/completions", json={})

    assert response.status_code == 429
    body = response.get_json()
    assert body is not None
    assert body["error"]["code"] == "rate_limit_exceeded"
