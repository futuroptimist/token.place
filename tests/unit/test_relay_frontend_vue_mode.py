from __future__ import annotations

from pathlib import Path

import relay


INDEX_HTML_PATH = Path(relay.INDEX_HTML_PATH)


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


def test_index_route_supports_conditional_get(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_FRONTEND_MODE", raising=False)

    with relay.app.test_client() as client:
        first_response = client.get("/")
        etag = first_response.headers.get("ETag")
        assert etag

        second_response = client.get("/", headers={"If-None-Match": etag})

    assert second_response.status_code == 304


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
