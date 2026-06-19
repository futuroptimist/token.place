"""Focused CORS contract tests for public API v1 browser clients."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask

from api import init_app

UNRELATED_ORIGIN = "https://cors-smoke.invalid"
SECOND_ORIGIN = "https://second-cors-smoke.invalid"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_RATE_LIMIT", "2/hour")
    monkeypatch.setenv("API_DAILY_QUOTA", "100/day")
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_app(app)
    with app.test_client() as test_client:
        yield test_client


def _preflight(client, path: str, origin: str = UNRELATED_ORIGIN):
    return client.open(
        path,
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, Accept",
        },
    )


def _assert_no_credentials(response):
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_api_v1_chat_preflight_returns_literal_wildcard(client):
    response = _preflight(client, "/api/v1/chat/completions")

    assert response.status_code == 204
    assert response.data == b""
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    assert "content-type" in response.headers["Access-Control-Allow-Headers"].lower()
    assert response.headers["Access-Control-Max-Age"] == "600"
    _assert_no_credentials(response)


def test_api_v1_preflight_does_not_echo_or_allowlist_origin(client):
    first = _preflight(client, "/api/v1/chat/completions", UNRELATED_ORIGIN)
    second = _preflight(client, "/api/v1/chat/completions", SECOND_ORIGIN)

    assert first.headers["Access-Control-Allow-Origin"] == "*"
    assert second.headers["Access-Control-Allow-Origin"] == "*"
    assert (
        first.headers["Access-Control-Allow-Origin"]
        == second.headers["Access-Control-Allow-Origin"]
    )
    _assert_no_credentials(first)
    _assert_no_credentials(second)


def test_api_v1_validation_error_includes_wildcard_cors(client):
    response = client.post(
        "/api/v1/chat/completions",
        json={},
        headers={"Origin": UNRELATED_ORIGIN},
    )

    assert response.status_code == 400
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Expose-Headers"] == "Retry-After"
    _assert_no_credentials(response)


def test_api_v1_get_response_includes_wildcard_cors(client):
    response = client.get("/api/v1/models", headers={"Origin": UNRELATED_ORIGIN})

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    _assert_no_credentials(response)


def test_openai_v1_alias_has_same_cors_behavior(client):
    preflight = _preflight(client, "/v1/chat/completions", SECOND_ORIGIN)
    post = client.post(
        "/v1/chat/completions",
        json={},
        headers={"Origin": SECOND_ORIGIN},
    )

    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "*"
    assert post.status_code == 400
    assert post.headers["Access-Control-Allow-Origin"] == "*"
    _assert_no_credentials(preflight)
    _assert_no_credentials(post)


def test_repeated_options_requests_do_not_consume_public_quota(client):
    for _ in range(8):
        response = _preflight(client, "/api/v1/chat/completions")
        assert response.status_code == 204
        assert response.headers["Access-Control-Allow-Origin"] == "*"

    first_post = client.post(
        "/api/v1/chat/completions",
        json={},
        headers={"Origin": UNRELATED_ORIGIN},
    )
    second_post = client.post(
        "/api/v1/chat/completions",
        json={},
        headers={"Origin": UNRELATED_ORIGIN},
    )
    limited_post = client.post(
        "/api/v1/chat/completions",
        json={},
        headers={"Origin": UNRELATED_ORIGIN},
    )

    assert first_post.status_code == 400
    assert second_post.status_code == 400
    assert limited_post.status_code == 429
    assert limited_post.headers["Access-Control-Allow-Origin"] == "*"
    assert limited_post.headers["Access-Control-Expose-Headers"] == "Retry-After"
    _assert_no_credentials(limited_post)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/chat/completions",
        "/v2/chat/completions",
        "/relay/api/v1/chat/completions",
        "/",
    ],
)
def test_non_public_api_v1_paths_do_not_gain_wildcard_policy(client, path):
    with patch("api.v2.routes.get_model_instance", return_value=object()), patch(
        "api.v2.routes.generate_response", return_value="hello"
    ):
        response = client.open(
            path, method="OPTIONS", headers={"Origin": UNRELATED_ORIGIN}
        )

    assert "Access-Control-Allow-Origin" not in response.headers
    assert "Access-Control-Allow-Credentials" not in response.headers
