"""Tests for explicit compute-node unregister relay behaviour."""

import importlib
import sys
import time
from typing import Iterator

import pytest

MODULES_TO_CLEAR = ("relay", "config", "api", "api.v1", "api.v1.routes")


@pytest.fixture(autouse=True)
def reset_modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)
    yield
    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)


def test_unregister_removes_server_and_related_queue_state() -> None:
    relay = importlib.import_module("relay")
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()

    server_public_key = "server-key"
    client_public_key = "client-key"
    relay.known_servers[server_public_key] = {
        "public_key": server_public_key,
        "last_ping": relay.datetime.now(),
        "last_ping_duration": 10,
    }
    relay.client_inference_requests[server_public_key] = [{"chat_history": "cipher"}]
    relay.streaming_sessions["session-1"] = {
        "session_id": "session-1",
        "server_public_key": server_public_key,
        "client_public_key": client_public_key,
        "chunks": [],
        "status": "open",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    relay.streaming_sessions_by_client[client_public_key] = "session-1"

    client = relay.app.test_client()
    response = client.post("/unregister", json={"server_public_key": server_public_key})

    assert response.status_code == 200
    assert response.get_json() == {"message": "Server unregistered"}
    assert server_public_key not in relay.known_servers
    assert server_public_key not in relay.client_inference_requests
    assert "session-1" not in relay.streaming_sessions
    assert client_public_key not in relay.streaming_sessions_by_client


def test_unregister_removes_node_from_relay_diagnostics_immediately() -> None:
    relay = importlib.import_module("relay")
    relay.known_servers.clear()

    server_public_key = "server-key"
    relay.known_servers[server_public_key] = {
        "public_key": server_public_key,
        "last_ping": relay.datetime.now(),
        "last_ping_duration": 10,
    }

    client = relay.app.test_client()
    before = client.get("/relay/diagnostics")
    assert before.status_code == 200
    listed_before = before.get_json()["registered_compute_nodes"]
    assert any(node["server_public_key"] == server_public_key for node in listed_before)

    unregister = client.post("/unregister", json={"server_public_key": server_public_key})
    assert unregister.status_code == 200

    after = client.get("/relay/diagnostics")
    assert after.status_code == 200
    listed_after = after.get_json()["registered_compute_nodes"]
    assert all(node["server_public_key"] != server_public_key for node in listed_after)
