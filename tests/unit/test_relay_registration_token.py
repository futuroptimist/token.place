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
