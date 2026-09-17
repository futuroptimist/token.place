"""Readiness behavior for the relay deployment."""

from __future__ import annotations

import pytest
from unittest.mock import Mock

import relay
from relay import DRAINING, app
from valkey_relay_state import ValkeyRegistrationStore, ValkeySchemaIncompatibleError


@pytest.fixture()
def relay_client():
    """Provide a Flask test client with a resolvable GPU host."""

    DRAINING.clear()
    had_gpu_host = "gpu_host" in app.config
    previous_gpu_host = app.config.get("gpu_host")
    app.config["gpu_host"] = None

    with app.test_client() as client:
        yield client

    if had_gpu_host:
        app.config["gpu_host"] = previous_gpu_host
    else:
        app.config.pop("gpu_host", None)
    DRAINING.clear()


def test_healthz_reports_draining_state(relay_client):
    """/healthz should return 503 and Retry-After when the relay is draining."""

    response = relay_client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"

    try:
        DRAINING.set()
        draining_response = relay_client.get("/healthz")
    finally:
        DRAINING.clear()

    assert draining_response.status_code == 503
    assert draining_response.headers.get("Retry-After") == "0"

    assert draining_response.headers.get("Cache-Control") == "no-store"
    assert draining_response.get_json() == {
        "error": {"message": "Relay is draining", "code": "state_draining"}
    }


def test_healthz_does_not_mutate_state(relay_client, monkeypatch):
    monkeypatch.setattr(relay, "_evict_stale_servers", Mock())
    assert relay_client.get("/healthz").status_code == 200
    relay._evict_stale_servers.assert_not_called()


def test_valkey_healthz_checks_readiness_before_diagnostics(relay_client, monkeypatch):
    store = ValkeyRegistrationStore.__new__(ValkeyRegistrationStore)
    store.readiness = Mock(side_effect=ValkeySchemaIncompatibleError("secret endpoint"))
    diagnostics = Mock()
    monkeypatch.setattr(relay, "api_v1_relay_state_store", store)
    monkeypatch.setattr(relay, "_live_server_diagnostics", diagnostics)

    response = relay_client.get("/healthz")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "message": "Relay state schema is incompatible",
            "code": "state_schema_incompatible",
        }
    }
    assert "secret endpoint" not in response.get_data(as_text=True)
    diagnostics.assert_not_called()
