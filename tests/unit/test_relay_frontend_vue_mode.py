from __future__ import annotations

import importlib


def _load_relay_module(monkeypatch, mode: str | None):
    if mode is None:
        monkeypatch.delenv("TOKENPLACE_FRONTEND_MODE", raising=False)
    else:
        monkeypatch.setenv("TOKENPLACE_FRONTEND_MODE", mode)

    import relay

    return importlib.reload(relay)


def test_relay_uses_production_vue_by_default(monkeypatch):
    relay = _load_relay_module(monkeypatch, None)

    html = relay._render_index_html()

    assert relay.VUE_PROD_SCRIPT_SRC in html
    assert relay.VUE_DEV_SCRIPT_SRC not in html
    assert relay.VUE_SCRIPT_PLACEHOLDER not in html


def test_relay_uses_development_vue_when_explicitly_requested(monkeypatch):
    relay = _load_relay_module(monkeypatch, "development")

    html = relay._render_index_html()

    assert relay.VUE_DEV_SCRIPT_SRC in html
    assert relay.VUE_PROD_SCRIPT_SRC not in html


def test_static_index_uses_runtime_vue_placeholder() -> None:
    with open("static/index.html", encoding="utf-8") as index_file:
        html = index_file.read()

    assert "__TOKENPLACE_VUE_SCRIPT_SRC__" in html
    assert "dist/vue.js" not in html
