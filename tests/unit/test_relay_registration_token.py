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


def test_unregister_requires_registration_token(relay_module) -> None:
    """/unregister should enforce token auth parity with /sink and /source."""

    relay_module.known_servers.clear()
    client = relay_module.app.test_client()

    client.post(
        "/sink",
        json={"server_public_key": "abc"},
        headers={"X-Relay-Server-Token": "unit-token"},
    )
    assert "abc" in relay_module.known_servers

    unauthorised = client.post("/unregister", json={"server_public_key": "abc"})
    assert unauthorised.status_code == 401
    assert "abc" in relay_module.known_servers

    authorised = client.post(
        "/unregister",
        json={"server_public_key": "abc"},
        headers={"X-Relay-Server-Token": "unit-token"},
    )
    assert authorised.status_code == 200
    assert authorised.get_json()["removed"] is True
    assert "abc" not in relay_module.known_servers


def test_unregister_removes_server_and_diagnostics_immediately(relay_module) -> None:
    """Relay diagnostics should no longer list a server right after /unregister."""

    relay_module.known_servers.clear()
    relay_module.client_inference_requests.clear()
    client = relay_module.app.test_client()
    headers = {"X-Relay-Server-Token": "unit-token"}

    server_key = "server-a"
    client.post("/sink", json={"server_public_key": server_key}, headers=headers)
    relay_module.client_inference_requests[server_key] = [{"chat_history": "queued"}]

    before = client.get("/relay/diagnostics")
    before_payload = before.get_json()
    assert before_payload["total_registered_compute_nodes"] == 1

    response = client.post(
        "/unregister",
        json={"server_public_key": server_key},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["removed"] is True
    assert relay_module.client_inference_requests.get(server_key) is None

    after = client.get("/relay/diagnostics")
    after_payload = after.get_json()
    assert after_payload["registered_compute_nodes"] == []
    assert after_payload["total_registered_compute_nodes"] == 0
