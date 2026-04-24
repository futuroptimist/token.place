"""Unit tests for relay server registration token enforcement."""

import importlib
import sys
from typing import Iterator

import pytest

MODULES_TO_CLEAR = ("relay", "config", "api", "api.v1", "api.v1.routes")


@pytest.fixture()
def relay_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Load the relay module with a configured server registration token."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", "unit-token")

    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)

    relay = importlib.import_module("relay")
    relay.app.config["TESTING"] = True

    yield relay

    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)

    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKENS", raising=False)


def test_sink_rejects_missing_registration_token(relay_module) -> None:
    """/sink should deny server registration when the auth header is absent."""

    client = relay_module.app.test_client()

    response = client.post("/sink", json={"server_public_key": "abc"})
    assert response.status_code == 401

    payload = response.get_json()
    assert payload == {
        "error": {
            "code": 401,
            "message": "Missing or invalid relay server token",
        }
    }

    authorised = client.post(
        "/sink",
        json={"server_public_key": "abc"},
        headers={"X-Relay-Server-Token": "unit-token"},
    )
    assert authorised.status_code == 200


def test_source_requires_registration_token(relay_module) -> None:
    """/source should require the server token for posting encrypted replies."""

    relay_module.client_responses.clear()
    client = relay_module.app.test_client()

    message = {
        "client_public_key": "client",
        "chat_history": "cipher",
        "cipherkey": "key",
        "iv": "iv",
    }

    unauthorised = client.post("/source", json=message)
    assert unauthorised.status_code == 401

    payload = unauthorised.get_json()
    assert payload == {
        "error": {
            "code": 401,
            "message": "Missing or invalid relay server token",
        }
    }

    authorised = client.post(
        "/source",
        json=message,
        headers={"X-Relay-Server-Token": "unit-token"},
    )
    assert authorised.status_code == 200

    queued = authorised.get_json()
    assert queued == {"message": "Response received and queued for client"}


def test_unregister_requires_registration_token(relay_module) -> None:
    """/unregister should require auth parity with /sink and /source."""

    relay_module.known_servers.clear()
    relay_module.client_inference_requests.clear()
    relay_module.streaming_sessions.clear()
    relay_module.streaming_sessions_by_client.clear()

    relay_module.known_servers["abc"] = {
        "public_key": "abc",
        "last_ping": relay_module.datetime.now(),
        "last_ping_duration": 10,
    }
    relay_module.client_inference_requests["abc"] = [{"chat_history": "cipher"}]
    relay_module.streaming_sessions["session-1"] = {
        "session_id": "session-1",
        "server_public_key": "abc",
        "client_public_key": "client-1",
        "chunks": [],
        "status": "open",
        "created_at": 0,
        "updated_at": 0,
    }
    relay_module.streaming_sessions_by_client["client-1"] = "session-1"

    client = relay_module.app.test_client()
    payload = {"server_public_key": "abc"}

    unauthorised = client.post("/unregister", json=payload)
    assert unauthorised.status_code == 401

    authorised = client.post(
        "/unregister",
        json=payload,
        headers={"X-Relay-Server-Token": "unit-token"},
    )
    assert authorised.status_code == 200
    assert authorised.get_json() == {"message": "Server unregistered", "removed": True}
    assert "abc" not in relay_module.known_servers
    assert "abc" not in relay_module.client_inference_requests
    assert "session-1" not in relay_module.streaming_sessions
    assert "client-1" not in relay_module.streaming_sessions_by_client


def test_unregister_removes_node_from_relay_diagnostics_immediately(relay_module) -> None:
    """Relay diagnostics should stop listing nodes as soon as unregister succeeds."""

    relay_module.known_servers.clear()
    relay_module.known_servers["abc"] = {
        "public_key": "abc",
        "last_ping": relay_module.datetime.now(),
        "last_ping_duration": 10,
    }

    client = relay_module.app.test_client()

    before = client.get("/relay/diagnostics")
    assert before.status_code == 200
    before_payload = before.get_json()
    assert before_payload["total_registered_compute_nodes"] == 1
    assert before_payload["registered_compute_nodes"][0]["server_public_key"] == "abc"

    unregister_response = client.post(
        "/unregister",
        json={"server_public_key": "abc"},
        headers={"X-Relay-Server-Token": "unit-token"},
    )
    assert unregister_response.status_code == 200

    after = client.get("/relay/diagnostics")
    assert after.status_code == 200
    after_payload = after.get_json()
    assert after_payload["total_registered_compute_nodes"] == 0
    assert after_payload["registered_compute_nodes"] == []


def test_sink_accepts_plural_registration_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plural env var should allow any listed registration token."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKENS", "alpha-token, beta-token")

    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)

    try:
        relay = importlib.import_module("relay")
        relay.app.config["TESTING"] = True
        client = relay.app.test_client()

        accepted = client.post(
            "/sink",
            json={"server_public_key": "abc"},
            headers={"X-Relay-Server-Token": "beta-token"},
        )
        assert accepted.status_code == 200

        rejected = client.post(
            "/sink",
            json={"server_public_key": "abc"},
            headers={"X-Relay-Server-Token": "wrong-token"},
        )
        assert rejected.status_code == 401
    finally:
        for name in MODULES_TO_CLEAR:
            sys.modules.pop(name, None)


def test_evict_stale_servers_cleans_up_queue_and_stream_state(relay_module) -> None:
    """TTL eviction should use unregister cleanup so per-server state is removed."""

    relay_module.known_servers.clear()
    relay_module.client_inference_requests.clear()
    relay_module.streaming_sessions.clear()
    relay_module.streaming_sessions_by_client.clear()

    relay_module.known_servers["stale-server"] = {
        "public_key": "stale-server",
        "last_ping": 0.0,
        "last_ping_duration": 10,
    }
    relay_module.client_inference_requests["stale-server"] = [{"chat_history": "cipher"}]
    relay_module.streaming_sessions["session-stale"] = {
        "session_id": "session-stale",
        "server_public_key": "stale-server",
        "client_public_key": "client-stale",
        "chunks": [],
        "status": "open",
        "created_at": 0,
        "updated_at": 0,
    }
    relay_module.streaming_sessions_by_client["client-stale"] = "session-stale"

    evicted = relay_module._evict_stale_servers()

    assert evicted == ["stale-server"]
    assert "stale-server" not in relay_module.known_servers
    assert "stale-server" not in relay_module.client_inference_requests
    assert "session-stale" not in relay_module.streaming_sessions
    assert "client-stale" not in relay_module.streaming_sessions_by_client


def test_api_v1_relay_dispatch_requires_registration_tokens_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API v1 relay dispatch should fail closed when registration tokens are unset."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_SERVER_TOKENS", raising=False)

    for name in MODULES_TO_CLEAR:
        sys.modules.pop(name, None)

    try:
        relay = importlib.import_module("relay")
        relay.app.config["TESTING"] = True
        client = relay.app.test_client()

        response = client.post(
            "/relay/api/v1/chat/completions",
            json={"model": "llama", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503
        assert response.get_json() == {
            "error": {
                "type": "service_unavailable_error",
                "code": "relay_registration_tokens_required",
                "message": "API v1 relay dispatch requires relay server registration tokens",
            }
        }
    finally:
        for name in MODULES_TO_CLEAR:
            sys.modules.pop(name, None)
