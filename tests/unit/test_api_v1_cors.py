"""Focused tests for public API v1 browser CORS behavior."""

from __future__ import annotations

import os
from unittest.mock import patch

from flask import Flask, jsonify

from api import init_app

ORIGIN_ONE = "https://cors-smoke.invalid"
ORIGIN_TWO = "https://another-browser.invalid"


def _build_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "ok"

    @app.route("/api/v1/meta")
    def api_v1_meta():
        return jsonify({"version": "test"})

    init_app(app)
    app.config["TESTING"] = True
    return app


def _preflight(client, path: str, origin: str = ORIGIN_ONE):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, Accept",
        },
    )


def _header_values(response, header_name: str) -> list[str]:
    return [value.lower().strip() for value in response.headers.get(header_name, "").split(",")]


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_api_v1_chat_preflight_returns_literal_wildcard_without_credentials():
    app = _build_app()

    with app.test_client() as client:
        response = _preflight(client, "/api/v1/chat/completions")

    assert response.status_code == 204
    assert response.data == b""
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "post" in _header_values(response, "Access-Control-Allow-Methods")
    assert "content-type" in _header_values(response, "Access-Control-Allow-Headers")
    assert response.headers["Access-Control-Max-Age"] == "600"
    assert "Access-Control-Allow-Credentials" not in response.headers


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_api_v1_cors_does_not_echo_or_allowlist_origins():
    app = _build_app()

    with app.test_client() as client:
        first = _preflight(client, "/api/v1/chat/completions", ORIGIN_ONE)
        second = _preflight(client, "/api/v1/chat/completions", ORIGIN_TWO)

    assert first.headers["Access-Control-Allow-Origin"] == "*"
    assert second.headers["Access-Control-Allow-Origin"] == "*"
    assert first.headers["Access-Control-Allow-Origin"] != ORIGIN_ONE
    assert second.headers["Access-Control-Allow-Origin"] != ORIGIN_TWO
    assert "Access-Control-Allow-Credentials" not in first.headers
    assert "Access-Control-Allow-Credentials" not in second.headers


@patch.dict(os.environ, {"API_RATE_LIMIT": "10/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_api_v1_invalid_json_400_keeps_cors_and_exposes_retry_after():
    app = _build_app()

    with app.test_client() as client:
        response = client.post(
            "/api/v1/chat/completions",
            data="{not-json",
            content_type="application/json",
            headers={"Origin": ORIGIN_ONE},
        )

    assert response.status_code == 400
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Expose-Headers"] == "Retry-After"
    assert "Access-Control-Allow-Credentials" not in response.headers


@patch.dict(os.environ, {"API_RATE_LIMIT": "10/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_api_v1_get_response_includes_cors():
    app = _build_app()

    with app.test_client() as client:
        response = client.get("/api/v1/meta", headers={"Origin": ORIGIN_ONE})

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in response.headers


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_openai_v1_alias_has_same_cors_preflight_behavior():
    app = _build_app()

    with app.test_client() as client:
        response = _preflight(client, "/v1/chat/completions")

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "post" in _header_values(response, "Access-Control-Allow-Methods")
    assert "content-type" in _header_values(response, "Access-Control-Allow-Headers")
    assert "Access-Control-Allow-Credentials" not in response.headers


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_repeated_options_requests_do_not_consume_public_api_quota():
    app = _build_app()

    with app.test_client() as client:
        preflights = [_preflight(client, "/api/v1/chat/completions") for _ in range(3)]
        post_response = client.post("/api/v1/chat/completions", json={})

    assert [response.status_code for response in preflights] == [204, 204, 204]
    assert post_response.status_code != 429
    assert post_response.headers["Access-Control-Allow-Origin"] == "*"


@patch.dict(os.environ, {"API_RATE_LIMIT": "10/hour", "API_DAILY_QUOTA": "1000/day"}, clear=True)
def test_non_api_v1_paths_do_not_receive_public_api_v1_cors_policy():
    app = _build_app()

    with app.test_client() as client:
        responses = [
            client.options("/api/v2/chat/completions", headers={"Origin": ORIGIN_ONE}),
            client.options("/v2/chat/completions", headers={"Origin": ORIGIN_ONE}),
            client.options("/relay/api/v1/chat/completions", headers={"Origin": ORIGIN_ONE}),
            client.get("/", headers={"Origin": ORIGIN_ONE}),
        ]

    for response in responses:
        assert "Access-Control-Allow-Origin" not in response.headers
        assert "Access-Control-Allow-Credentials" not in response.headers
