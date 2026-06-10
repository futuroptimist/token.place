"""Regression guardrails for the frozen API v1 launch contract."""

from __future__ import annotations

from pathlib import Path

import pytest

import relay
from api.v1 import compute_provider, routes

INDEX_HTML = Path(relay.INDEX_HTML_PATH)

PUBLIC_CLIENT_ROUTES = {
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

OPENAI_V1_ALIASES = {
    ("GET", "/v1/models"),
    ("GET", "/v1/models/{model_id}"),
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

INTERNAL_RELAY_LIFECYCLE_ROUTES = {
    ("POST", "/api/v1/relay/requests/cancel"),
    ("POST", "/relay/api/v1/chat/completions"),
    ("POST", "/relay/api/v1/source"),
}

DOCUMENTED_INTERNAL_ROUTES = COMPUTE_NODE_CONTROL_PLANE_ROUTES | INTERNAL_RELAY_LIFECYCLE_ROUTES


def _normalise_rule(rule: str) -> str:
    return rule.replace("<model_id>", "{model_id}")


def _registered_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for rule in relay.app.url_map.iter_rules():
        path = _normalise_rule(rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            routes.add((method, path))
    return routes


@pytest.fixture
def client():
    relay.app.config["TESTING"] = True
    with relay.app.test_client() as test_client:
        yield test_client


def test_frozen_api_v1_launch_routes_are_registered_from_flask_url_map():
    registered = _registered_routes()

    assert PUBLIC_CLIENT_ROUTES <= registered
    assert OPENAI_V1_ALIASES <= registered
    assert DOCUMENTED_INTERNAL_ROUTES <= registered


def test_no_unclassified_api_v1_routes_leak_into_launch_contract():
    registered_api_v1 = {
        (method, path)
        for method, path in _registered_routes()
        if path.startswith("/api/v1/") or path.startswith("/v1/") or path.startswith("/relay/api/v1/")
    }
    expected = PUBLIC_CLIENT_ROUTES | OPENAI_V1_ALIASES | DOCUMENTED_INTERNAL_ROUTES

    assert registered_api_v1 == expected


def test_landing_page_documents_every_public_client_api_v1_route():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "Public/client API v1 launch contract" in html
    assert "Internal relay control-plane routes" in html
    public_section = html.split("Internal relay control-plane routes", 1)[0]

    for method, path in sorted(PUBLIC_CLIENT_ROUTES):
        assert f"{method}</span> <span class=\"api-path\">{path}</span>" in public_section


def test_landing_page_documents_openai_aliases_and_excludes_api_v2_launch_docs():
    html = INDEX_HTML.read_text(encoding="utf-8")

    for _method, path in sorted(OPENAI_V1_ALIASES):
        assert path in html
    assert "/api/v2" not in html
    assert "API v2 is intentionally outside this launch contract" in html


def test_landing_page_freezes_single_meta_llama_model_without_owner_display():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "llama-3.1-8b-instruct" in html
    assert "llama-3-8b-instruct:alignment" not in html
    assert "llama-3.1-8b-instruct:alignment" not in html
    assert "owned by token.place" not in html
    assert "selectedModelSummary" not in html


def test_compute_node_control_plane_routes_are_documented_separately():
    html = INDEX_HTML.read_text(encoding="utf-8")
    public_section, internal_section = html.split("Internal relay control-plane routes", 1)

    for method, path in sorted(COMPUTE_NODE_CONTROL_PLANE_ROUTES):
        assert f"{method} {path}" not in public_section
        assert f"{method} {path}" in internal_section

    for method, path in sorted(INTERNAL_RELAY_LIFECYCLE_ROUTES):
        assert f"{method} {path}" not in public_section
        assert f"{method} {path}" in internal_section

    assert "not general user-facing API endpoints" in internal_section


def test_api_v1_chat_completions_rejects_stream_true(client):
    response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["param"] == "stream"
    assert "Streaming is not supported" in payload["error"]["message"]


def test_api_v1_model_listing_is_not_api_v2_catalog_dump(client):
    response = client.get("/api/v1/models")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["object"] == "list"
    assert len(payload["data"]) == 1
    model = payload["data"][0]
    assert model["id"] == "llama-3.1-8b-instruct"
    assert model["object"] == "model"
    assert model["owned_by"] == "Meta"
    assert "token.place" not in model["owned_by"]
    assert "permission" in model
    assert isinstance(model["permission"], list)
    assert model["permission"]
    assert model["permission"][0]["object"] == "model_permission"
    assert "metadata" not in model
    assert "adapter" not in model


def test_api_v1_model_aliases_are_invisible_and_alignment_is_rejected(client):
    listing = client.get("/api/v1/models").get_json()["data"]
    listed_ids = [model["id"] for model in listing]

    assert listed_ids == ["llama-3.1-8b-instruct"]
    assert "llama-3-8b-instruct" not in listed_ids
    assert "llama-3-8b-instruct:alignment" not in listed_ids

    alias_response = client.get("/api/v1/models/llama-3-8b-instruct")
    assert alias_response.status_code == 200
    assert alias_response.get_json()["id"] == "llama-3.1-8b-instruct"

    alignment_response = client.get("/api/v1/models/llama-3-8b-instruct:alignment")
    assert alignment_response.status_code == 404
    assert alignment_response.get_json()["error"]["code"] == "model_not_found"


def test_landing_page_model_example_documents_openai_permission_objects():
    html = INDEX_HTML.read_text(encoding="utf-8")
    models_section = html.split("/api/v1/models/{model_id}", 1)[0]

    assert '"permission": [' in models_section
    assert '"permission": ["..."]' not in models_section
    assert '"object": "model_permission"' in models_section
    assert '"allow_sampling": true' in models_section
    assert '"created": CREATED_UNIX_SECONDS' in models_section
    assert '"created": 1700000000' not in models_section


def test_route_level_generate_response_bridge_is_request_local(monkeypatch):
    original_compute_generator = compute_provider.generate_response

    def fake_generate_response(model_id, messages, **options):
        assert model_id == "llama-3.1-8b-instruct"
        assert options == {"temperature": 0.2}
        return messages + [{"role": "assistant", "content": "request-local bridge"}]

    monkeypatch.setattr(routes, "generate_response", fake_generate_response)

    result = routes._call_provider_complete_chat(
        compute_provider.LocalApiV1ComputeProvider(),
        model_id="llama-3.1-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"temperature": 0.2},
    )

    assert result == {"role": "assistant", "content": "request-local bridge"}
    assert compute_provider.generate_response is original_compute_generator
    assert compute_provider._active_generate_response() is original_compute_generator
