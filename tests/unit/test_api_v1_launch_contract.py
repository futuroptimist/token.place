"""Regression tests for the frozen API v1 launch contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PUBLIC_CLIENT_ROUTES = {
    ("GET", "/api/v1/models"),
    ("GET", "/api/v1/models/<model_id>"),
    ("GET", "/api/v1/public-key"),
    ("POST", "/api/v1/public-key/rotate"),
    ("POST", "/api/v1/chat/completions"),
    ("POST", "/api/v1/completions"),
    ("POST", "/api/v1/images/generations"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/community/providers"),
    ("GET", "/api/v1/community/leaderboard"),
    ("POST", "/api/v1/community/contributions"),
    ("GET", "/api/v1/community/contributions/summary"),
    ("GET", "/api/v1/server-providers"),
    ("GET", "/api/v1/relay/server-nodes"),
    ("POST", "/api/v1/relay/unregister"),
    ("GET", "/api/v1/relay/servers/next"),
    ("POST", "/api/v1/relay/requests"),
    ("POST", "/api/v1/relay/responses/retrieve"),
}

OPENAI_V1_ALIAS_ROUTES = {
    ("GET", "/v1/models"),
    ("GET", "/v1/models/<model_id>"),
    ("GET", "/v1/public-key"),
    ("POST", "/v1/public-key/rotate"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("POST", "/v1/images/generations"),
    ("GET", "/v1/health"),
    ("POST", "/v1/relay/unregister"),
}

COMPUTE_CONTROL_PLANE_ROUTES = {
    ("POST", "/api/v1/relay/servers/register"),
    ("POST", "/api/v1/relay/servers/unregister"),
    ("POST", "/api/v1/relay/servers/poll"),
    ("POST", "/api/v1/relay/requests/cancel"),
    ("POST", "/api/v1/relay/responses"),
}

# API v1 model-listing docs/tests must not drift into API v2's broader catalogue.
API_V2_ONLY_MODEL_IDS = {
    "gpt-oss-20b",
    "mistral-7b-instruct",
    "mixtral-8x7b-instruct",
    "phi-3-mini-4k-instruct",
    "mistral-nemo-instruct",
    "qwen2.5-7b-instruct",
    "qwen2.5-coder-7b-instruct",
    "gemma-2-9b-it",
    "codegemma-7b",
    "smollm2-1.7b-instruct",
}


@pytest.fixture(scope="module")
def client():
    from relay import app

    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(scope="module")
def route_inventory():
    from relay import app

    inventory: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            inventory.add((method, rule.rule))
    return inventory


@pytest.fixture(scope="module")
def landing_html() -> str:
    return Path("static/index.html").read_text(encoding="utf-8")


def _html_path(path: str) -> str:
    return path.replace("<model_id>", "{model_id}")


def _public_route_blocks(html: str) -> list[str]:
    pattern = re.compile(
        r'<div class="api-endpoint" data-api-surface="[^"]*public-client[^"]*">(.*?)</div>',
        re.DOTALL,
    )
    return pattern.findall(html)


def test_api_v1_launch_routes_are_registered(route_inventory):
    """The launch docs are backed by actual Flask routes, not memory."""

    expected = PUBLIC_CLIENT_ROUTES | OPENAI_V1_ALIAS_ROUTES | COMPUTE_CONTROL_PLANE_ROUTES
    assert expected <= route_inventory


def test_no_unaccounted_api_v1_launch_routes(route_inventory):
    """All production API v1 relay/client routes are categorized by this contract."""

    ignored = {
        ("GET", "/api/v1/metrics"),
        ("GET", "/api/v1/docs"),
    }
    api_v1_routes = {
        item
        for item in route_inventory
        if item[1].startswith("/api/v1/") or item[1] == "/api/v1"
    }
    accounted = PUBLIC_CLIENT_ROUTES | COMPUTE_CONTROL_PLANE_ROUTES | ignored
    assert api_v1_routes <= accounted


def test_openai_v1_aliases_are_limited_to_intentional_routes(route_inventory):
    aliases = {item for item in route_inventory if item[1].startswith("/v1/")}
    assert aliases == OPENAI_V1_ALIAS_ROUTES


def test_landing_page_documents_public_client_routes(landing_html):
    for _method, path in PUBLIC_CLIENT_ROUTES:
        assert _html_path(path) in landing_html


def test_landing_page_keeps_compute_routes_internal(landing_html):
    public_blocks = "\n".join(_public_route_blocks(landing_html))
    for _method, path in COMPUTE_CONTROL_PLANE_ROUTES:
        html_path = _html_path(path)
        path_markup = f'<span class="api-path">{html_path}</span>'
        assert path_markup in landing_html
        assert path_markup not in public_blocks

    assert "Internal compute-node relay control-plane routes" in landing_html


def test_landing_page_excludes_api_v2_from_launch_contract(landing_html):
    assert "/api/v2" not in landing_html
    assert "/v2" not in landing_html
    assert "API v2 is intentionally not part of this launch contract" in landing_html


def test_api_v1_chat_completions_reject_stream_true(client):
    response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["param"] == "stream"
    assert "streaming" in payload["error"]["message"].lower()


def test_api_v1_model_listing_stays_launch_catalog_not_v2_dump(client):
    response = client.get("/api/v1/models")
    assert response.status_code == 200

    payload = response.get_json()
    model_ids = {item["id"] for item in payload["data"]}

    assert model_ids
    assert model_ids.isdisjoint(API_V2_ONLY_MODEL_IDS)
    assert all("metadata" not in item for item in payload["data"])
