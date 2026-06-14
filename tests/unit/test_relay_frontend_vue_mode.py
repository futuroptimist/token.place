from __future__ import annotations

import json
import re

from pathlib import Path

import relay


INDEX_HTML_PATH = Path(relay.INDEX_HTML_PATH)


def _embedded_release_metadata(html: str) -> dict[str, str]:
    match = re.search(
        r'<script id="tokenplace-release-metadata" type="application/json">([^<]+)</script>',
        html,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_flask_builtin_static_route_is_disabled():
    endpoints = {rule.endpoint for rule in relay.app.url_map.iter_rules()}

    assert "static" not in endpoints


def test_root_route_uses_production_vue_by_default(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_FRONTEND_MODE", raising=False)

    with relay.app.test_client() as client:
        response = client.get("/")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert relay.VUE_PROD_SCRIPT_SRC in body
    assert relay.VUE_DEV_SCRIPT_SRC not in body
    assert relay.VUE_SCRIPT_PLACEHOLDER not in body


def test_relay_uses_development_vue_when_explicitly_requested(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_FRONTEND_MODE", "development")

    html = relay._render_index_html()

    assert relay.VUE_DEV_SCRIPT_SRC in html
    assert relay.VUE_PROD_SCRIPT_SRC not in html


def test_static_index_uses_runtime_vue_placeholder() -> None:
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")

    assert relay.VUE_SCRIPT_PLACEHOLDER in html
    assert "dist/vue.js" not in html


def test_render_index_html_reads_from_module_anchored_path(monkeypatch):
    monkeypatch.chdir("/")

    html = relay._render_index_html()

    assert "<html" in html


def test_static_index_route_uses_runtime_rendering(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_FRONTEND_MODE", "development")

    with relay.app.test_request_context("/static/index.html"):
        response = relay.serve_static("index.html")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert relay.VUE_DEV_SCRIPT_SRC in body
    assert relay.VUE_SCRIPT_PLACEHOLDER not in body


def test_index_route_is_dynamic_no_store_without_conditional_cache(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_FRONTEND_MODE", raising=False)

    with relay.app.test_client() as client:
        first_response = client.get("/")
        second_response = client.get("/", headers={"If-None-Match": "anything"})
        static_index_response = client.get("/static/index.html")

    for response in (first_response, second_response, static_index_response):
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "ETag" not in response.headers
        assert "Last-Modified" not in response.headers


def test_root_route_uses_development_vue_when_requested(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_FRONTEND_MODE", "dev")

    with relay.app.test_client() as client:
        response = client.get("/")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert relay.VUE_DEV_SCRIPT_SRC in body
    assert relay.VUE_PROD_SCRIPT_SRC not in body


def test_production_served_html_excludes_dev_vue_paths(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_FRONTEND_MODE", raising=False)

    with relay.app.test_client() as client:
        root_response = client.get("/")
        static_response = client.get("/static/index.html")

    for response in (root_response, static_response):
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "dist/vue.js" not in body
        assert relay.VUE_SCRIPT_PLACEHOLDER not in body


def test_render_index_html_injects_release_badge_metadata(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "v0.1.1")
    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_ENV", raising=False)

    html = relay._render_index_html("token.place")

    assert 'data-testid="release-badge"' in html
    assert 'prod v0.1.1' in html
    assert relay.RELEASE_METADATA_PLACEHOLDER not in html
    assert relay.RELEASE_BADGE_TEXT_PLACEHOLDER not in html


def test_meta_endpoint_returns_public_safe_metadata(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "v0.1.1")
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-830d0a4")
    monkeypatch.setenv("TOKENPLACE_OPERATOR_TOKEN", "do-not-leak")

    with relay.app.test_client() as client:
        meta_response = client.get("/api/v1/meta", headers={"Host": "token.place"})
        version_response = client.get("/api/v1/version", headers={"Host": "token.place"})

    assert meta_response.status_code == 200
    assert version_response.status_code == 200
    assert version_response.get_json() == meta_response.get_json()
    body = meta_response.get_json()
    assert body["environment"] == "staging"
    assert body["version"] == "v0.1.1"
    assert body["label"] == "staging main-830d0a4"
    assert body["ref"] == "main-830d0a4"
    assert meta_response.headers["Cache-Control"] == "no-store"
    assert version_response.headers["Cache-Control"] == "no-store"
    assert "do-not-leak" not in meta_response.get_data(as_text=True)
    assert "do-not-leak" not in version_response.get_data(as_text=True)


def test_release_badge_and_embedded_metadata_agree_for_staging_git_ref(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-latest")
    monkeypatch.setenv("TOKENPLACE_GIT_SHA", "830d0a46beee297ac67de54f470c9939f9d514a1")

    with relay.app.test_client() as client:
        response = client.get("/", headers={"Host": "staging.token.place"})
        meta_response = client.get("/api/v1/meta", headers={"Host": "staging.token.place"})
        version_response = client.get("/api/v1/version", headers={"Host": "staging.token.place"})

    html = response.get_data(as_text=True)
    expected = {
        "environment": "staging",
        "version": "0.1.1",
        "label": "staging main-830d0a4",
        "ref": "main-830d0a4",
    }
    assert response.status_code == 200
    assert meta_response.get_json() == expected
    assert version_response.get_json() == expected
    assert _embedded_release_metadata(html) == expected
    assert '<span class="release-badge-label" data-testid="release-badge-label">staging main-830d0a4</span>' in html


def test_release_badge_and_embedded_metadata_agree_for_prod_release(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "prod")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-latest")
    monkeypatch.delenv("TOKENPLACE_GIT_SHA", raising=False)

    with relay.app.test_client() as client:
        response = client.get("/", headers={"Host": "token.place"})
        meta_response = client.get("/api/v1/meta", headers={"Host": "token.place"})
        version_response = client.get("/api/v1/version", headers={"Host": "token.place"})

    html = response.get_data(as_text=True)
    expected = {"environment": "prod", "version": "0.1.1", "label": "prod 0.1.1"}
    assert response.status_code == 200
    assert meta_response.get_json() == expected
    assert version_response.get_json() == expected
    assert _embedded_release_metadata(html) == expected
    assert '<span class="release-badge-label" data-testid="release-badge-label">prod 0.1.1</span>' in html


def test_rendered_index_versions_landing_javascript_assets(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-d35648d")
    monkeypatch.delenv("TOKENPLACE_GIT_SHA", raising=False)

    html = relay._render_index_html("staging.token.place")

    assert '/static/chat_typing.js?v=main-d35648d' in html
    assert '/static/chat.js?v=main-d35648d' in html
    assert 'src="/static/chat.js"' not in html
    assert 'src="/static/chat_typing.js"' not in html
    assert relay.ASSET_VERSION_PLACEHOLDER not in html


def test_landing_javascript_assets_are_no_cache():
    with relay.app.test_client() as client:
        chat_response = client.get("/static/chat.js")
        typing_response = client.get("/static/chat_typing.js")

    assert chat_response.status_code == 200
    assert typing_response.status_code == 200
    assert chat_response.headers["Cache-Control"] == "no-cache"
    assert typing_response.headers["Cache-Control"] == "no-cache"
