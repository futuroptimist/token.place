"""Focused tests for the public API v1 browser CORS contract."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from flask import Flask, jsonify

from api import init_app


@pytest.fixture
def app() -> Iterator[Flask]:
    with patch.dict(
        os.environ,
        {"API_RATE_LIMIT": "1/minute", "API_DAILY_QUOTA": "1000/day"},
        clear=True,
    ):
        flask_app = Flask(__name__)
        init_app(flask_app)

        @flask_app.get("/")
        def index():
            return "ok"

        @flask_app.post("/relay/api/v1/chat/completions")
        def internal_relay_chat():
            return jsonify({"ok": True})

        @flask_app.get("/api/v1/meta")
        def api_v1_meta():
            return jsonify({"version": "test"})

        yield flask_app


@pytest.fixture
def client(app: Flask):
    with app.test_client() as test_client:
        yield test_client


def _preflight(client, path: str, origin: str = "https://cors-smoke.invalid"):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, accept",
        },
    )


def _header_values(response, name: str) -> set[str]:
    return {
        value.strip().lower()
        for header_value in response.headers.get_all(name)
        for value in header_value.split(",")
    }


def test_api_v1_chat_preflight_uses_literal_wildcard_without_credentials(client):
    response = _preflight(client, "/api/v1/chat/completions")

    assert response.status_code in {200, 204}
    assert response.get_data() == b""
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "post" in _header_values(response, "Access-Control-Allow-Methods")
    allowed_headers = _header_values(response, "Access-Control-Allow-Headers")
    assert "content-type" in allowed_headers
    assert "accept" in allowed_headers
    assert "authorization" not in allowed_headers
    assert "x-relay-server-token" not in allowed_headers
    assert response.headers["Access-Control-Max-Age"] == "600"
    assert "Access-Control-Allow-Credentials" not in response.headers


def test_unrelated_origins_receive_same_literal_wildcard_not_origin_echo(client):
    first = _preflight(client, "/api/v1/chat/completions", "https://cors-smoke.invalid")
    second = _preflight(client, "/api/v1/chat/completions", "https://totally-unrelated.example")

    assert first.headers["Access-Control-Allow-Origin"] == "*"
    assert second.headers["Access-Control-Allow-Origin"] == "*"
    assert second.headers["Access-Control-Allow-Origin"] != "https://totally-unrelated.example"
    assert "Access-Control-Allow-Credentials" not in second.headers


def test_api_v1_validation_error_response_includes_wildcard_cors(client):
    response = client.post(
        "/api/v1/chat/completions",
        json={},
        headers={"Origin": "https://cors-smoke.invalid"},
    )

    assert response.status_code == 400
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Expose-Headers"] == "Retry-After"
    assert "Access-Control-Allow-Credentials" not in response.headers


def test_api_v1_rate_limit_error_response_includes_wildcard_cors(client):
    assert client.get("/api/v1/models").status_code == 200
    response = client.get(
        "/api/v1/models",
        headers={"Origin": "https://cors-smoke.invalid"},
    )

    assert response.status_code == 429
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Expose-Headers"] == "Retry-After"
    assert response.headers.get("Retry-After") is not None
    assert "Access-Control-Allow-Credentials" not in response.headers


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/models"),
        ("get", "/api/v1/health"),
        ("get", "/v1/health"),
        ("get", "/api/v1/relay/servers/next"),
        ("post", "/api/v1/relay/requests"),
        ("post", "/api/v1/relay/requests/cancel"),
        ("post", "/api/v1/relay/responses/retrieve"),
    ],
)
def test_api_v1_public_response_includes_wildcard_cors(client, method, path):
    response = getattr(client, method)(
        path,
        headers={"Origin": "https://cors-smoke.invalid"},
    )

    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in response.headers


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/chat/completions",
        "/v1/chat/completions",
        "/api/v1/health",
        "/v1/health",
        "/api/v1/relay/servers/next",
        "/api/v1/relay/requests",
        "/api/v1/relay/requests/cancel",
        "/api/v1/relay/responses/retrieve",
    ],
)
def test_api_v1_public_preflight_includes_wildcard_cors(client, path):
    response = _preflight(client, path)

    assert response.status_code in {200, 204}
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "content-type" in _header_values(response, "Access-Control-Allow-Headers")
    assert "Access-Control-Allow-Credentials" not in response.headers


def test_openai_v1_alias_has_same_cors_behavior(client):
    preflight = _preflight(client, "/v1/chat/completions")
    error_response = client.post(
        "/v1/chat/completions",
        json={},
        headers={"Origin": "https://cors-smoke.invalid"},
    )

    assert preflight.headers["Access-Control-Allow-Origin"] == "*"
    assert "post" in _header_values(preflight, "Access-Control-Allow-Methods")
    assert "Access-Control-Allow-Credentials" not in preflight.headers
    assert error_response.status_code == 400
    assert error_response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in error_response.headers


def test_repeated_preflights_do_not_consume_public_api_quota(client):
    for _ in range(5):
        response = _preflight(client, "/api/v1/chat/completions")
        assert response.status_code != 429

    assert client.get("/api/v1/models").status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("options", "/api/v2/chat/completions"),
        ("options", "/v2/chat/completions"),
        ("post", "/relay/api/v1/chat/completions"),
        ("options", "/relay/api/v12/chat/completions"),
        ("options", "/relay/api/v1something/chat/completions"),
        ("options", "/api/v1/relay/servers/register"),
        ("options", "/api/v1/relay/servers/poll"),
        ("options", "/api/v1/relay/servers/unregister"),
        ("options", "/api/v1/relay/responses"),
        ("options", "/api/v1/relay/unregister"),
        ("options", "/v1/relay/unregister"),
        ("options", "/api/v1/public-key/rotate"),
        ("options", "/v1/public-key/rotate"),
        ("post", "/api/v1/relay/servers/register"),
        ("post", "/api/v1/relay/servers/poll"),
        ("post", "/api/v1/relay/servers/unregister"),
        ("post", "/api/v1/relay/responses"),
        ("post", "/api/v1/relay/unregister"),
        ("post", "/v1/relay/unregister"),
        ("post", "/api/v1/public-key/rotate"),
        ("post", "/v1/public-key/rotate"),
        ("get", "/"),
    ],
)
def test_cors_policy_does_not_apply_outside_public_api_v1_prefixes(client, method, path):
    response = getattr(client, method)(path, headers={"Origin": "https://cors-smoke.invalid"})

    assert "Access-Control-Allow-Origin" not in response.headers
    assert "Access-Control-Allow-Credentials" not in response.headers
