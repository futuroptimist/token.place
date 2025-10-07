"""Tests for relay-configured compute node metadata exposure."""

import importlib
import sys
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def reset_modules() -> Iterator[None]:
    """Ensure relay imports see fresh configuration."""
    module_names = ("relay", "config", "api", "api.v1", "api.v1.routes")
    for module in module_names:
        sys.modules.pop(module, None)
    yield
    for module in module_names:
        sys.modules.pop(module, None)


def test_configured_nodes_endpoint_reflects_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relay should surface configured upstream servers via diagnostics endpoint."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv(
        "TOKEN_PLACE_RELAY_UPSTREAMS",
        "https://node-one.example.com:8000,https://node-two.example.com:8000/",
    )

    relay = importlib.import_module("relay")

    client = relay.app.test_client()
    response = client.get("/api/v1/relay/server-nodes")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["configured_servers"] == [
        "https://node-one.example.com:8000",
        "https://node-two.example.com:8000",
    ]
    assert payload["primary"] == "https://node-one.example.com:8000"
    assert payload["secondary"] == ["https://node-two.example.com:8000"]
    assert payload["total"] == 2
