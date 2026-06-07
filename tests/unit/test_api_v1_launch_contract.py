"""Regression guardrails for the frozen API v1 launch route contract."""

from __future__ import annotations

import re
from pathlib import Path

from relay import app


PUBLIC_CLIENT_API_V1_ROUTES = {
    ("GET", "/api/v1/models"),
    ("GET", "/api/v1/models/{model_id}"),
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

OPENAI_ALIAS_API_V1_ROUTES = {
    ("GET", "/v1/models"),
    ("GET", "/v1/models/{model_id}"),
    ("GET", "/v1/public-key"),
    ("POST", "/v1/public-key/rotate"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("POST", "/v1/images/generations"),
    ("GET", "/v1/health"),
    ("POST", "/v1/relay/unregister"),
}

INTERNAL_COMPUTE_NODE_API_V1_ROUTES = {
    ("POST", "/api/v1/relay/servers/register"),
    ("POST", "/api/v1/relay/servers/poll"),
    ("POST", "/api/v1/relay/servers/unregister"),
    ("POST", "/api/v1/relay/responses"),
    ("POST", "/api/v1/relay/requests/cancel"),
}

INTERNAL_RELAY_BRIDGE_API_V1_ROUTES = {
    ("POST", "/relay/api/v1/chat/completions"),
    ("POST", "/relay/api/v1/source"),
}

STATIC_INDEX = Path(__file__).resolve().parents[2] / "static" / "index.html"


def _normalise_rule(rule: str) -> str:
    return rule.replace("<model_id>", "{model_id}")


def _registered_routes_with_prefixes(*prefixes: str) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        path = _normalise_rule(rule.rule)
        if not path.startswith(prefixes):
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            routes.add((method, path))
    return routes


def _route_label(route: tuple[str, str]) -> str:
    method, path = route
    return f"{method} {path}"


def _index_html() -> str:
    return STATIC_INDEX.read_text(encoding="utf-8")


def _visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _contains_route_label(html: str, route: tuple[str, str]) -> bool:
    text = _visible_text(html)
    return re.search(rf"(?<!\S){re.escape(_route_label(route))}(?=$|\s|—)", text) is not None


def test_api_v1_launch_route_inventory_is_registered():
    """Every API v1 launch route must remain registered in Flask."""

    expected_api_v1_routes = (
        PUBLIC_CLIENT_API_V1_ROUTES | INTERNAL_COMPUTE_NODE_API_V1_ROUTES
    )
    registered_api_v1_routes = _registered_routes_with_prefixes("/api/v1")

    assert expected_api_v1_routes == registered_api_v1_routes
    assert OPENAI_ALIAS_API_V1_ROUTES == _registered_routes_with_prefixes("/v1")
    assert INTERNAL_RELAY_BRIDGE_API_V1_ROUTES == _registered_routes_with_prefixes(
        "/relay/api/v1"
    )


def test_landing_page_documents_public_client_routes_and_aliases():
    """The landing page must document every public/client API v1 launch route."""

    html = _index_html()

    for method, path in sorted(PUBLIC_CLIENT_API_V1_ROUTES):
        assert path in html, f"missing landing-page docs for {_route_label((method, path))}"

    for _method, path in sorted(OPENAI_ALIAS_API_V1_ROUTES):
        assert path in html, f"missing landing-page alias mention for {path}"

    assert "API v1 launch contract" in html
    assert "Operator-gated" in html
    assert _contains_route_label(html, ("POST", "/api/v1/public-key/rotate"))
    assert _contains_route_label(html, ("POST", "/api/v1/relay/unregister"))


def test_internal_compute_node_routes_are_accounted_for_separately():
    """Compute-node and fail-closed bridge paths are listed as internal, not public."""

    html = _index_html()
    public_section = html.split("Internal relay compute-node control plane", 1)[0]
    internal_section = html.split("Internal relay compute-node control plane", 1)[1]

    for _method, path in sorted(INTERNAL_COMPUTE_NODE_API_V1_ROUTES):
        route = (_method, path)
        assert _contains_route_label(internal_section, route), f"missing internal route accounting for {path}"
        assert not _contains_route_label(public_section, route), f"{path} is presented before the internal section"

    for _method, path in sorted(INTERNAL_RELAY_BRIDGE_API_V1_ROUTES):
        route = (_method, path)
        assert _contains_route_label(internal_section, route), f"missing internal bridge route accounting for {path}"
        assert not _contains_route_label(public_section, route), f"{path} is presented before the internal section"

    assert "not general user-facing API calls" in internal_section


def test_landing_api_v1_docs_do_not_include_api_v2_launch_surface():
    """API v2 is out of scope for the API v1 launch contract docs."""

    html = _index_html().lower()

    assert "/api/v2" not in html
    assert "/v2" not in html
    assert "api v2" not in html


def test_api_v1_chat_completions_rejects_stream_true():
    """API v1 remains non-streaming for the 0.1.x launch contract."""

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    payload = response.get_json()
    assert response.status_code == 400
    assert payload["error"]["param"] == "stream"
    assert "Streaming is not supported for API v1 chat completions" in payload["error"]["message"]


def test_api_v1_model_listing_stays_openai_compatible_not_v2_catalog_dump():
    """The v1 model list should not grow v2-only catalog fields by accident."""

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/api/v1/models")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["object"] == "list"
    assert payload["data"]

    v2_only_fields = {"adapters", "capabilities", "modalities", "input_modalities"}
    for model in payload["data"]:
        assert model["object"] == "model"
        assert set(model) == {
            "id",
            "object",
            "created",
            "owned_by",
            "permission",
            "root",
            "parent",
        }
        assert v2_only_fields.isdisjoint(model)
