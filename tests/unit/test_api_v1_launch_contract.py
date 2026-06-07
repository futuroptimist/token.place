"""Regression guardrails for the token.place API v1 launch contract."""

from __future__ import annotations

import re
from pathlib import Path

from relay import app


REPO_ROOT = Path(__file__).resolve().parents[2]
LANDING_PAGE = REPO_ROOT / "static" / "index.html"

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
    ("POST", "/api/v1/relay/requests/cancel"),
    ("POST", "/api/v1/relay/responses/retrieve"),
}

OPENAI_V1_ALIASES = {
    ("GET", "/v1/models"),
    ("GET", "/v1/models/<model_id>"),
    ("GET", "/v1/public-key"),
    ("POST", "/v1/public-key/rotate"),
    ("POST", "/v1/relay/unregister"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("POST", "/v1/images/generations"),
    ("GET", "/v1/health"),
}

COMPUTE_NODE_CONTROL_PLANE_ROUTES = {
    ("POST", "/api/v1/relay/servers/register"),
    ("POST", "/api/v1/relay/servers/unregister"),
    ("POST", "/api/v1/relay/servers/poll"),
    ("POST", "/api/v1/relay/responses"),
}


def _registered_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            routes.add((method, rule.rule))
    return routes


def _landing_html() -> str:
    return LANDING_PAGE.read_text(encoding="utf-8")


def _html_path(route: str) -> str:
    return route.replace("<model_id>", "{model_id}")


def _endpoint_block(html: str, route: str) -> str:
    path = f'<span class="api-path">{_html_path(route)}</span>'
    blocks = re.findall(r'<div class="api-endpoint"[^>]*>.*?</div>', html, re.DOTALL)
    for block in blocks:
        if path in block:
            return block
    assert False, f"missing API documentation block for {route}"


def test_api_v1_launch_routes_are_registered() -> None:
    registered = _registered_routes()

    expected = PUBLIC_CLIENT_ROUTES | OPENAI_V1_ALIASES | COMPUTE_NODE_CONTROL_PLANE_ROUTES
    assert expected <= registered


def test_public_client_routes_are_documented_on_landing_page() -> None:
    html = _landing_html()

    for _method, route in PUBLIC_CLIENT_ROUTES:
        block = _endpoint_block(html, route)
        assert 'data-api-surface="public-client' in block
        assert "internal-compute-node-control-plane" not in block


def test_openai_aliases_are_documented_as_aliases_only() -> None:
    html = _landing_html()

    assert "OpenAI-compatible aliases" in html
    for _method, route in OPENAI_V1_ALIASES:
        assert _html_path(route) in html


def test_compute_node_control_plane_routes_are_internal_only() -> None:
    html = _landing_html()

    assert "Internal compute-node control-plane routes" in html
    for _method, route in COMPUTE_NODE_CONTROL_PLANE_ROUTES:
        block = _endpoint_block(html, route)
        assert 'data-api-surface="internal-compute-node-control-plane"' in block
        assert 'data-api-surface="public-client' not in block


def test_landing_api_v1_launch_docs_do_not_publish_api_v2_or_legacy_relay_routes() -> None:
    html = _landing_html()

    assert "/api/v2" not in html
    assert "/v2/" not in html

    forbidden_legacy_routes = {
        "/sink",
        "/faucet",
        "/source",
        "/retrieve",
        "/next_server",
    }
    for route_text in forbidden_legacy_routes:
        assert f'<span class="api-path">{route_text}</span>' not in html
        assert f'<code>{route_text}</code>' not in html


def test_api_v1_chat_completions_rejects_stream_true() -> None:
    with app.test_client() as client:
        response = client.post(
            "/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["param"] == "stream"
    assert "Streaming is not supported for API v1" in payload["error"]["message"]


def test_api_v1_model_listing_stays_v1_openai_shape_not_v2_catalog_dump() -> None:
    with app.test_client() as client:
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"object", "data"}
    assert payload["object"] == "list"
    assert payload["data"], "expected at least one API v1 launch model"

    for model in payload["data"]:
        assert set(model) == {
            "id",
            "object",
            "created",
            "owned_by",
            "permission",
            "root",
            "parent",
        }
        assert model["object"] == "model"
        assert model["owned_by"] == "token.place"
        assert isinstance(model["created"], int)
        assert "capabilities" not in model
        assert "providers" not in model
        assert "modalities" not in model
