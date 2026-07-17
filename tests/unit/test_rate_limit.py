"""Unit tests for API rate limiting."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, request as flask_request

from api import (
    _check_control_plane_limits,
    _control_plane_identity_for_request,
    _control_server_owner_identity,
    _load_relay_server_registration_tokens,
    init_app,
)


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


@patch.dict(
    os.environ,
    {"API_RATE_LIMIT": "100/minute", "API_STREAM_RATE_LIMIT": "1/minute"},
    clear=True,
)
def test_non_json_chat_completion_posts_do_not_consume_stream_limit():
    """The streaming-only limiter should ignore non-JSON chat requests."""

    app = Flask(__name__)
    init_app(app)

    with app.test_client() as client:
        responses = [
            client.post("/api/v2/chat/completions", data="not-json") for _ in range(2)
        ]

    assert 429 not in {response.status_code for response in responses}


@patch.dict(os.environ, {}, clear=True)
def test_relay_token_loader_handles_missing_config(monkeypatch):
    """Missing config/env tokens should leave relay mutations quota-protected."""

    monkeypatch.delitem(sys.modules, "relay", raising=False)
    monkeypatch.delattr(
        sys.modules["__main__"], "SERVER_REGISTRATION_TOKENS", raising=False
    )

    with patch("api.get_config", side_effect=AttributeError):
        assert _load_relay_server_registration_tokens() == []


@patch.dict(
    os.environ,
    {
        "TOKEN_PLACE_RELAY_SERVER_TOKENS": "plural-one\nplural-two, ",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "single-token",
    },
    clear=True,
)
def test_relay_token_loader_combines_config_and_env_tokens(monkeypatch):
    """Fallback token loading should normalize configured and env tokens."""

    monkeypatch.delitem(sys.modules, "relay", raising=False)
    monkeypatch.delattr(
        sys.modules["__main__"], "SERVER_REGISTRATION_TOKENS", raising=False
    )

    with patch(
        "api.get_config",
        return_value={"relay.server_registration_token": " config-one,config-two "},
    ):
        assert _load_relay_server_registration_tokens() == [
            "config-one",
            "config-two",
            "plural-one",
            "plural-two",
            "single-token",
        ]


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

    control_plane_storage = MagicMock()
    control_plane_limiter = MagicMock()
    with (
        patch("api.Limiter", return_value=limiter_instance) as limiter_cls,
        patch(
            "api.storage_from_string", return_value=control_plane_storage
        ) as storage_cls,
        patch(
            "api.FixedWindowRateLimiter", return_value=control_plane_limiter
        ) as control_limiter_cls,
    ):
        limiter = init_app(app)

    assert limiter is limiter_instance
    assert limiter_cls.call_args.kwargs["storage_uri"] == "memcached://127.0.0.1:11211"
    storage_cls.assert_called_once_with("memcached://127.0.0.1:11211")
    control_limiter_cls.assert_called_once_with(control_plane_storage)
    assert app.config["relay_control_plane_rate_limiter"] is control_plane_limiter


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
def test_authenticated_api_v1_relay_heartbeat_routes_do_not_inherit_public_quota(
    monkeypatch,
):
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

    @app.post("/api/v1/relay/servers/unregister")
    def relay_servers_unregister():
        return {"status": "unregistered"}

    with app.test_client() as client:
        for path in (
            "/api/v1/relay/servers/register",
            "/api/v1/relay/servers/poll",
            "/api/v1/relay/servers/unregister",
        ):
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
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "2/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "100/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_control_plane_routes_use_server_key_bucket_not_public_quota(monkeypatch):
    """Compute nodes behind one IP should not collide in the user API bucket."""
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

    with app.test_client() as client:
        server_a = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-a"},
                headers={"X-Relay-Server-Token": "relay-token"},
            )
            for _ in range(2)
        ]
        server_b = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-b"},
                headers={"X-Relay-Server-Token": "relay-token"},
            )
            for _ in range(2)
        ]
        limited = client.post(
            "/api/v1/relay/servers/register",
            json={"server_public_key": "server-a"},
            headers={"X-Relay-Server-Token": "relay-token"},
        )

    assert [response.status_code for response in server_a] == [200, 200]
    assert [response.status_code for response in server_b] == [200, 200]
    assert limited.status_code == 429
    assert limited.get_json()["error"]["code"] == "rate_limit_exceeded"


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "2/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "100/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "2/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_control_plane_routes_keep_aggregate_ip_abuse_budget():
    """Unique server keys cannot bypass aggregate route protection."""
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    with app.test_client() as client:
        responses = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": f"server-{index}"},
                headers={"X-Relay-Server-Token": "relay-token"},
            )
            for index in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 200, 429]


def test_control_server_owner_identity_requires_matching_bound_credential(monkeypatch):
    """Control buckets use the exact owner only after proof verification."""

    def digest(value: str) -> str:
        return f"digest:{value}"

    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(
            known_servers={
                "server-a": {"api_v1_control_credential_digest": digest("secret-a")},
            },
            _api_v1_control_credential_digest=digest,
        ),
    )

    assert _control_server_owner_identity(None) is None
    assert _control_server_owner_identity({"server_public_key": "server-a"}) is None
    assert (
        _control_server_owner_identity(
            {"server_public_key": "server-a", "control_credential": "wrong"}
        )
        is None
    )
    valid_control_payload = {
        "server_public_key": " server-a ",
        "control_credential": "secret-a",
    }
    assert _control_server_owner_identity(valid_control_payload) == (
        "server_public_key",
        "server-a",
    )

    app = Flask(__name__)
    with app.test_request_context("/api/v1/relay/servers/control", method="POST"):
        assert _control_plane_identity_for_request(
            "/api/v1/relay/servers/control", valid_control_payload
        ) == ("server_public_key", "server-a")


def test_control_route_rate_limit_identity_falls_back_to_ip_without_owner_proof(
    monkeypatch,
):
    """Invalid control credentials cannot burn another server's identity bucket."""

    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(
            known_servers={}, _api_v1_control_credential_digest=lambda value: value
        ),
    )
    app = Flask(__name__)

    with app.test_request_context(
        "/api/v1/relay/servers/control",
        method="POST",
        environ_base={"REMOTE_ADDR": "203.0.113.9"},
    ):
        assert _control_plane_identity_for_request(
            "/api/v1/relay/servers/control",
            {"server_public_key": "victim", "control_credential": "wrong"},
        ) == ("client_ip", "203.0.113.9")


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "2/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_POLL_RATE_LIMIT": "65/hour",
        "API_RELAY_CONTROL_PLANE_CONTROL_RATE_LIMIT": "65/hour",
        "API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT": "65/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "1000/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_poll_control_and_response_control_plane_routes_do_not_use_public_quota():
    """Compute control-plane routes allow healthy cadence above user quota."""
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/poll")
    def relay_servers_poll():
        return {"status": "polling"}

    @app.post("/api/v1/relay/servers/control")
    def relay_servers_control():
        return {"status": "active"}

    @app.post("/api/v1/relay/responses")
    def relay_responses():
        return {"status": "queued"}

    digest = lambda value: f"digest:{value}"
    relay_module = SimpleNamespace(
        SERVER_REGISTRATION_TOKENS=["relay-token"],
        known_servers={
            "server-a": {"api_v1_control_credential_digest": digest("control-secret-a")},
        },
        _api_v1_control_credential_digest=digest,
    )

    with patch.dict(sys.modules, {"relay": relay_module, "__main__": relay_module}):
        with app.test_client() as client:
            poll_responses = [
                client.post(
                    "/api/v1/relay/servers/poll",
                    json={"server_public_key": "server-a"},
                    headers={"X-Relay-Server-Token": "relay-token"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.10"},
                )
                for _ in range(65)
            ]
            control_responses = [
                client.post(
                    "/api/v1/relay/servers/control",
                    json={
                        "server_public_key": "server-a",
                        "request_id": f"req-{index}",
                        "control_credential": "control-secret-a",
                    },
                    headers={"X-Relay-Server-Token": "relay-token"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.11"},
                )
                for index in range(66)
            ]
            response_submissions = [
                client.post(
                    "/api/v1/relay/responses",
                    json={"client_public_key": "client-a", "request_id": f"req-{index}"},
                    headers={"X-Relay-Server-Token": "relay-token"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.12"},
                )
                for index in range(65)
            ]

    assert {response.status_code for response in poll_responses} == {200}
    assert [response.status_code for response in control_responses[:65]] == [200] * 65
    assert control_responses[65].status_code == 429
    assert {response.status_code for response in response_submissions} == {200}


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "2/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_CONTROL_RATE_LIMIT": "65/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "200/hour",
    },
    clear=True,
)
def test_tokenless_control_route_uses_verified_owner_identity_bucket():
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/control")
    def relay_servers_control():
        return {"status": "active"}

    digest = lambda value: f"digest:{value}"
    relay_module = SimpleNamespace(
        SERVER_REGISTRATION_TOKENS=[],
        known_servers={
            "server-a": {"api_v1_control_credential_digest": digest("control-secret-a")},
            "server-b": {"api_v1_control_credential_digest": digest("control-secret-b")},
        },
        _api_v1_control_credential_digest=digest,
    )
    with patch.dict(sys.modules, {"relay": relay_module, "__main__": relay_module}):
        with app.test_client() as client:
            owner_a = [
                client.post(
                    "/api/v1/relay/servers/control",
                    json={
                        "server_public_key": "server-a",
                        "request_id": f"a-{index}",
                        "control_credential": "control-secret-a",
                    },
                    environ_overrides={"REMOTE_ADDR": "198.51.100.44"},
                )
                for index in range(66)
            ]
            owner_b = [
                client.post(
                    "/api/v1/relay/servers/control",
                    json={
                        "server_public_key": "server-b",
                        "request_id": f"b-{index}",
                        "control_credential": "control-secret-b",
                    },
                    environ_overrides={"REMOTE_ADDR": "198.51.100.44"},
                )
                for index in range(65)
            ]

    assert [response.status_code for response in owner_a[:65]] == [200] * 65
    assert owner_a[65].status_code == 429
    assert [response.status_code for response in owner_b] == [200] * 65


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "2/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "100/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_invalid_relay_tokens_do_not_burn_server_identity_quota(monkeypatch):
    """Unauthenticated callers cannot spoof-limit a victim server key."""
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

    with app.test_client() as client:
        invalid_attempts = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-a"},
                headers={"X-Relay-Server-Token": "wrong-token"},
            )
            for _ in range(2)
        ]
        valid_attempts = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-a"},
                headers={"X-Relay-Server-Token": "relay-token"},
            )
            for _ in range(3)
        ]

    assert [response.status_code for response in invalid_attempts] == [200, 200]
    assert [response.status_code for response in valid_attempts] == [200, 200, 429]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT": "2/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "2/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_invalid_relay_response_tokens_do_not_burn_client_identity_quota(monkeypatch):
    """Unauthenticated response submissions cannot spoof-limit a client bucket."""
    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(SERVER_REGISTRATION_TOKENS=["relay-token"]),
    )
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/responses")
    def relay_responses():
        return {"status": "queued"}

    payload = {
        "client_public_key": "client-a",
        "request_id": "request-a",
        "ciphertext": "sealed-response",
        "cipherkey": "sealed-key",
        "iv": "sealed-iv",
    }
    with app.test_client() as client:
        invalid_attempts = [
            client.post(
                "/api/v1/relay/responses",
                json=payload,
                headers={"X-Relay-Server-Token": "wrong-token"},
                environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
            )
            for _ in range(2)
        ]
        valid_attempts = [
            client.post(
                "/api/v1/relay/responses",
                json=payload,
                headers={"X-Relay-Server-Token": "relay-token"},
                environ_overrides={"REMOTE_ADDR": "192.0.2.11"},
            )
            for _ in range(3)
        ]

    assert [response.status_code for response in invalid_attempts] == [200, 200]
    assert [response.status_code for response in valid_attempts] == [200, 200, 429]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT": "2/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "2/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_malformed_relay_responses_do_not_burn_victim_response_bucket(monkeypatch):
    """Malformed response bodies cannot spoof-limit a victim client/request id."""
    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(SERVER_REGISTRATION_TOKENS=["relay-token"]),
    )
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/responses")
    def relay_responses():
        data = flask_request.get_json(silent=True) or {}
        if "ciphertext" not in data:
            return {"error": "malformed encrypted response envelope"}, 400
        return {"status": "queued"}

    victim_fields = {"client_public_key": "client-a", "request_id": "request-a"}
    with app.test_client() as client:
        malformed_attempts = [
            client.post(
                "/api/v1/relay/responses",
                json=victim_fields,
                headers={"X-Relay-Server-Token": "relay-token"},
                environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
            )
            for _ in range(2)
        ]
        valid_attempts = [
            client.post(
                "/api/v1/relay/responses",
                json={
                    **victim_fields,
                    "ciphertext": f"sealed-{index}",
                    "cipherkey": f"sealed-key-{index}",
                    "iv": f"sealed-iv-{index}",
                },
                headers={"X-Relay-Server-Token": "relay-token"},
                environ_overrides={"REMOTE_ADDR": "192.0.2.11"},
            )
            for index in range(3)
        ]

    assert [response.status_code for response in malformed_attempts] == [400, 400]
    assert [response.status_code for response in valid_attempts] == [200, 200, 429]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT": "2/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "100/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_authenticated_relay_responses_use_response_identity_budget(monkeypatch):
    """Valid response envelopes consume the response-specific client budget."""
    monkeypatch.setitem(
        sys.modules,
        "relay",
        SimpleNamespace(SERVER_REGISTRATION_TOKENS=["relay-token"]),
    )
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/responses")
    def relay_responses():
        return {"status": "queued"}

    with app.test_client() as client:
        responses = [
            client.post(
                "/api/v1/relay/responses",
                json={
                    "client_public_key": "client-a",
                    "request_id": f"request-{index}",
                    "ciphertext": f"sealed-{index}",
                    "cipherkey": f"sealed-key-{index}",
                    "iv": f"sealed-iv-{index}",
                },
                headers={"X-Relay-Server-Token": "relay-token"},
                environ_overrides={"REMOTE_ADDR": f"192.0.2.{10 + index}"},
            )
            for index in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 200, 429]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "2/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "1/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_ip_limited_rejections_do_not_burn_identity_quota():
    """Aggregate-IP 429s should not increment the server identity bucket."""
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    headers = {"X-Relay-Server-Token": "relay-token"}
    payload = {"server_public_key": "server-a"}
    with app.test_client() as client:
        first_ip = [
            client.post(
                "/api/v1/relay/servers/register",
                json=payload,
                headers=headers,
                environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
            )
            for _ in range(2)
        ]
        second_ip = [
            client.post(
                "/api/v1/relay/servers/register",
                json=payload,
                headers=headers,
                environ_overrides={"REMOTE_ADDR": "192.0.2.11"},
            )
            for _ in range(2)
        ]

    assert [response.status_code for response in first_ip] == [200, 429]
    assert [response.status_code for response in second_ip] == [200, 429]


def test_control_plane_identity_race_does_not_hit_aggregate_ip_bucket():
    """A concurrent identity rejection should fail before charging client IP."""

    class FakeLimit:
        def __init__(self, name: str):
            self.name = name

        def key_for(self, *identifiers):
            return f"{self.name}:{':'.join(identifiers)}"

    class FakeRateLimiter:
        def __init__(self):
            self.hit_calls = []
            self.storage = MagicMock()

        def test(self, limit_item, *identifiers):
            return True

        def hit(self, limit_item, *identifiers):
            self.hit_calls.append((limit_item.name, identifiers))
            return limit_item.name != "identity"

        def get_window_stats(self, limit_item, *identifiers):
            return SimpleNamespace(reset_time=9999999999)

    fake_limiter = FakeRateLimiter()
    allowed, _, bucket_kind, _, _ = _check_control_plane_limits(
        fake_limiter,
        [
            ("client_ip", "192.0.2.10", FakeLimit("ip")),
            ("server_public_key", "server-a", FakeLimit("identity")),
        ],
        route="/api/v1/relay/servers/register",
    )

    assert allowed is False
    assert bucket_kind == "server_public_key"
    assert [name for name, _ in fake_limiter.hit_calls] == ["identity"]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "1/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "2/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_identity_limited_rejections_do_not_burn_ip_quota(monkeypatch):
    """Identity-bucket 429s should roll back any aggregate-IP hit."""
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

    headers = {"X-Relay-Server-Token": "relay-token"}
    with app.test_client() as client:
        responses = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-a"},
                headers=headers,
            ),
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-a"},
                headers=headers,
            ),
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": "server-b"},
                headers=headers,
            ),
        ]

    assert [response.status_code for response in responses] == [200, 429, 200]


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "100/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "1/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "1/hour",
        "TOKEN_PLACE_RELAY_SERVER_TOKEN": "relay-token",
    },
    clear=True,
)
def test_control_plane_limiter_only_applies_to_post_methods():
    """GET/OPTIONS on POST-only control routes should not consume control budgets."""
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    with app.test_client() as client:
        get_responses = [client.get("/api/v1/relay/servers/register") for _ in range(3)]
        post_response = client.post(
            "/api/v1/relay/servers/register",
            json={"server_public_key": "server-a"},
            headers={"X-Relay-Server-Token": "relay-token"},
        )

    assert [response.status_code for response in get_responses] == [405, 405, 405]
    assert post_response.status_code == 200


@patch.dict(
    os.environ,
    {
        "API_RATE_LIMIT": "2/hour",
        "API_DAILY_QUOTA": "1000/day",
        "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT": "100/hour",
        "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT": "2/hour",
    },
    clear=True,
)
def test_tokenless_control_plane_mutations_use_ip_only_control_budget():
    """Tokenless relay deployments stay off public quota without spoofable keys."""
    app = Flask(__name__)
    init_app(app)

    @app.post("/api/v1/relay/servers/register")
    def relay_servers_register():
        return {"status": "registered"}

    with app.test_client() as client:
        responses = [
            client.post(
                "/api/v1/relay/servers/register",
                json={"server_public_key": f"spoofable-server-{index}"},
            )
            for index in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 200, 429]


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
