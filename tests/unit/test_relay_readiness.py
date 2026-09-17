"""Readiness behavior for the relay deployment."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import relay
from relay import DRAINING, app
from relay_state_store import RelayStateStoreError
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

    draining_payload = draining_response.get_json()
    assert draining_payload == {
        "error": {"message": "Relay is draining", "code": "relay_draining"}
    }


def test_livez_is_process_only_during_drain(relay_client, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("livez accessed the state store"))
    monkeypatch.setattr(relay, "_api_v1_store", forbidden)
    DRAINING.set()

    response = relay_client.get("/livez")

    assert response.status_code == 200
    assert response.get_json() == {"status": "alive"}
    forbidden.assert_not_called()


def test_draining_health_is_bounded_and_performs_no_store_access(
    relay_client, monkeypatch
):
    forbidden = Mock(side_effect=AssertionError("draining health accessed state"))
    monkeypatch.setattr(relay, "_api_v1_store", forbidden)
    monkeypatch.setattr(relay, "_live_server_diagnostics", forbidden)
    monkeypatch.setattr(relay, "_evict_stale_servers", forbidden)
    DRAINING.set()

    response = relay_client.get("/healthz")

    assert response.status_code == 503
    assert len(response.data) < 256
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (
            RelayStateStoreError("redis://secret@backend/key"),
            "state_backend_unavailable",
        ),
        (
            ValkeySchemaIncompatibleError("secret schema/key"),
            "state_schema_incompatible",
        ),
    ],
)
def test_valkey_health_fails_closed_before_diagnostics(
    relay_client, monkeypatch, failure, code
):
    store = Mock(spec=ValkeyRegistrationStore)
    store.readiness.side_effect = failure
    monkeypatch.setattr(relay, "_api_v1_store", Mock(return_value=store))
    diagnostics = Mock(side_effect=AssertionError("readiness failure read protocol state"))
    monkeypatch.setattr(relay, "_live_server_diagnostics", diagnostics)

    response = relay_client.get("/healthz")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == code
    assert b"secret" not in response.data
    assert len(response.data) < 256
    store.readiness.assert_called_once_with()
    diagnostics.assert_not_called()


def test_memory_health_preserves_empty_capacity_readiness(relay_client, monkeypatch):
    store = Mock()
    monkeypatch.setattr(relay, "_api_v1_store", Mock(return_value=store))
    monkeypatch.setattr(relay, "_live_server_diagnostics", Mock(return_value=[]))
    evict = Mock()
    monkeypatch.setattr(relay, "_evict_stale_servers", evict)

    response = relay_client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json()["registeredServers"] == []
    evict.assert_not_called()


@pytest.mark.parametrize(
    "path", ["/api/v1/relay/servers/next", "/api/v1/relay/servers/poll"]
)
def test_draining_suppresses_reservations_and_claims(
    relay_client, monkeypatch, path
):
    store = Mock()
    monkeypatch.setattr(relay, "_api_v1_store", Mock(return_value=store))
    DRAINING.set()
    if path.endswith("poll"):
        response = relay_client.post(path, json={
            "server_public_key": "node", "control_credential": "credential"
        })
    else:
        response = relay_client.get(path)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "relay_draining"
    store.select_and_reserve.assert_not_called()
    store.claim_queued_request.assert_not_called()
