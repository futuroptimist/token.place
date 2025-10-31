"""Regression tests for the /api/v1/health endpoint promises."""

from __future__ import annotations

import importlib

from flask import Flask

import api.v1.routes as v1_routes_module


def test_health_defaults_service_name_when_env_blank(monkeypatch):
    """Blank SERVICE_NAME overrides should fall back to the token.place default."""

    monkeypatch.setenv("SERVICE_NAME", "   ")

    try:
        reloaded = importlib.reload(v1_routes_module)

        app = Flask(__name__)
        app.register_blueprint(reloaded.v1_bp)

        response = app.test_client().get("/api/v1/health")
        payload = response.get_json()

        assert payload["service"] == "token.place"
        assert payload["status"] == "ok"
    finally:
        monkeypatch.delenv("SERVICE_NAME", raising=False)
        importlib.reload(v1_routes_module)
