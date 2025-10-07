"""Tests for production relay upstream configuration overrides."""

import sys
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def reset_config_module() -> Iterator[None]:
    """Ensure config module is reloaded for each test case."""
    sys.modules.pop("config", None)
    yield
    sys.modules.pop("config", None)


def test_relay_upstreams_env_populates_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated upstream URLs populate the relay server pool."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv(
        "TOKEN_PLACE_RELAY_UPSTREAMS",
        "https://node-a.example.com:8000, https://node-b.example.net:9443/",
    )

    from config import Config

    config = Config()

    assert config.get("relay.server_url") == "https://node-a.example.com:8000"
    assert config.get("relay.server_pool") == [
        "https://node-a.example.com:8000",
        "https://node-b.example.net:9443",
    ]
    assert config.get("relay.server_pool_secondary") == [
        "https://node-b.example.net:9443",
    ]


def test_personal_pc_alias_included_in_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy PERSONAL_GAMING_PC_URL feeds into the upstream pool."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("PERSONAL_GAMING_PC_URL", "https://gaming-pc.local:8000/")

    from config import Config

    config = Config()

    assert config.get("relay.server_url") == "https://gaming-pc.local:8000"
    assert config.get("relay.server_pool") == ["https://gaming-pc.local:8000"]
    assert config.get("relay.server_pool_secondary") == []
