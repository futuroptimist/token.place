"""Unit tests for relay compute-node unregister flow."""

import importlib
import sys

import pytest

MODULES_TO_CLEAR = ("relay", "config", "api", "api.v1", "api.v1.routes")


@pytest.fixture()
def relay_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKENS", raising=False)

    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)

    relay = importlib.import_module("relay")
    relay.app.config["TESTING"] = True

    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()

    yield relay

    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)


def test_unregister_removes_server_and_related_state(relay_module) -> None:
    client = relay_module.app.test_client()

    relay_module.known_servers["server-key"] = {
        "public_key": "server-key",
        "last_ping": relay_module.datetime.now(),
        "last_ping_duration": 10,
    }
    relay_module.client_inference_requests["server-key"] = [{"chat_history": "cipher"}]
    relay_module.streaming_sessions["session-1"] = {
        "session_id": "session-1",
        "server_public_key": "server-key",
        "client_public_key": "client-key",
        "chunks": [],
        "status": "open",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    relay_module.streaming_sessions_by_client["client-key"] = "session-1"

    diagnostics_before = client.get("/relay/diagnostics")
    assert diagnostics_before.status_code == 200
    assert diagnostics_before.get_json()["total_registered_compute_nodes"] == 1

    response = client.post("/unregister", json={"server_public_key": "server-key"})
    assert response.status_code == 200
    assert response.get_json() == {"message": "Server unregistered"}

    assert "server-key" not in relay_module.known_servers
    assert "server-key" not in relay_module.client_inference_requests
    assert "session-1" not in relay_module.streaming_sessions
    assert "client-key" not in relay_module.streaming_sessions_by_client

    diagnostics_after = client.get("/relay/diagnostics")
    assert diagnostics_after.status_code == 200
    payload = diagnostics_after.get_json()
    assert payload["registered_compute_nodes"] == []
    assert payload["total_registered_compute_nodes"] == 0


def test_unregister_requires_server_public_key(relay_module) -> None:
    client = relay_module.app.test_client()

    missing_key = client.post("/unregister", json={})
    assert missing_key.status_code == 400
    assert missing_key.get_json() == {"error": "Invalid public key"}

    invalid_data = client.post("/unregister", data="[]", content_type="application/json")
    assert invalid_data.status_code == 400
    assert invalid_data.get_json() == {"error": "Invalid request data"}
