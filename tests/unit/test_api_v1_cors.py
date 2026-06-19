"""CORS contract tests for public browser-callable API v1 routes."""

from __future__ import annotations

import os
from unittest.mock import patch

from flask import Flask

from api import init_app


def _fresh_client(*, rate_limit: str = "1000/hour"):
    env = {
        "API_RATE_LIMIT": rate_limit,
        "API_DAILY_QUOTA": "10000/day",
        "TOKENPLACE_RATE_LIMIT_STORAGE_URI": "memory://",
    }
    patcher = patch.dict(os.environ, env, clear=True)
    patcher.start()
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.route("/")
    def index():
        return "landing"

    init_app(app)
    client = app.test_client()
    return client, patcher


def _preflight(client, path: str, origin: str = "https://cors-smoke.invalid"):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, accept",
        },
    )


def _lower_header_tokens(value: str) -> set[str]:
    return {token.strip().lower() for token in value.split(",") if token.strip()}


def test_api_v1_preflight_returns_literal_wildcard_without_credentials():
    client, patcher = _fresh_client()
    try:
        response = _preflight(client, "/api/v1/chat/completions")
    finally:
        patcher.stop()

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    allowed_headers = _lower_header_tokens(
        response.headers["Access-Control-Allow-Headers"]
    )
    assert "content-type" in allowed_headers
    assert "accept" in allowed_headers
    assert "authorization" not in allowed_headers
    assert "x-relay-server-token" not in allowed_headers
    assert response.headers.get("Access-Control-Allow-Credentials") is None
    assert response.headers["Access-Control-Max-Age"] == "600"


def test_api_v1_preflight_does_not_echo_unrelated_origins():
    client, patcher = _fresh_client()
    try:
        first = _preflight(
            client, "/api/v1/chat/completions", "https://first.invalid"
        )
        second = _preflight(
            client, "/api/v1/chat/completions", "https://second.invalid"
        )
    finally:
        patcher.stop()

    assert first.headers["Access-Control-Allow-Origin"] == "*"
    assert second.headers["Access-Control-Allow-Origin"] == "*"


def test_invalid_json_api_v1_post_keeps_wildcard_cors_on_api_owned_400():
    client, patcher = _fresh_client()
    try:
        response = client.post(
            "/api/v1/chat/completions",
            data="{",
            content_type="application/json",
            headers={"Origin": "https://cors-smoke.invalid"},
        )
    finally:
        patcher.stop()

    assert response.status_code == 400
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_get_api_v1_meta_includes_wildcard_cors():
    import relay

    relay.app.config["TESTING"] = True
    with relay.app.test_client() as client:
        response = client.get(
            "/api/v1/meta", headers={"Origin": "https://cors-smoke.invalid"}
        )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_openai_v1_alias_has_same_cors_behavior():
    client, patcher = _fresh_client()
    try:
        preflight = _preflight(client, "/v1/chat/completions")
        response = client.post(
            "/v1/chat/completions",
            data="{",
            content_type="application/json",
            headers={"Origin": "https://cors-smoke.invalid"},
        )
    finally:
        patcher.stop()

    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "*"
    assert response.status_code == 400
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_api_v1_rate_limit_429_keeps_wildcard_cors_and_exposes_retry_after():
    client, patcher = _fresh_client(rate_limit="1/minute")
    try:
        assert client.get(
            "/api/v1/models", headers={"Origin": "https://cors-smoke.invalid"}
        ).status_code == 200
        response = client.get(
            "/api/v1/models", headers={"Origin": "https://cors-smoke.invalid"}
        )
    finally:
        patcher.stop()

    assert response.status_code == 429
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Expose-Headers"] == "Retry-After"
    assert response.headers.get("Access-Control-Allow-Credentials") is None
    assert response.headers.get("Retry-After") is not None


def test_repeated_api_v1_options_do_not_consume_public_quota():
    client, patcher = _fresh_client(rate_limit="1/minute")
    try:
        for _ in range(5):
            assert _preflight(client, "/api/v1/chat/completions").status_code == 204
        response = client.get("/api/v1/models")
    finally:
        patcher.stop()

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_non_api_v1_routes_do_not_gain_public_api_v1_cors_policy():
    client, patcher = _fresh_client()
    try:
        responses = [
            client.options(
                "/api/v2/chat/completions",
                headers={"Origin": "https://cors-smoke.invalid"},
            ),
            client.options(
                "/v2/chat/completions",
                headers={"Origin": "https://cors-smoke.invalid"},
            ),
            client.options(
                "/relay/api/v1/chat/completions",
                headers={"Origin": "https://cors-smoke.invalid"},
            ),
            client.get("/", headers={"Origin": "https://cors-smoke.invalid"}),
        ]
    finally:
        patcher.stop()

    for response in responses:
        assert response.headers.get("Access-Control-Allow-Origin") is None
        assert response.headers.get("Access-Control-Allow-Credentials") is None
