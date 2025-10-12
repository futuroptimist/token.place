"""Tests for production relay upstream configuration overrides."""

import json
import sys
from pathlib import Path
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


def test_json_encoded_pool_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON encoded upstream lists are parsed and normalised."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv(
        "TOKEN_PLACE_RELAY_UPSTREAMS",
        '["https://node-a.example.com", "https://node-b.example.net/", "https://node-a.example.com/"]',
    )

    from config import Config

    config = Config()

    assert config.get("relay.server_pool") == [
        "https://node-a.example.com",
        "https://node-b.example.net",
    ]


def test_newline_delimited_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newline-delimited upstream definitions are supported."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv(
        "TOKEN_PLACE_RELAY_UPSTREAMS",
        "https://alpha.example.com:8000\n https://beta.example.net:9443/",
    )

    from config import Config

    config = Config()

    assert config.get("relay.server_url") == "https://alpha.example.com:8000"
    assert config.get("relay.server_pool_secondary") == [
        "https://beta.example.net:9443",
    ]


def test_unsupported_json_type_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Unsupported JSON types for upstreams log a warning and are ignored."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_UPSTREAMS", "{\"url\": \"https://ignored\"}")

    from config import Config

    with caplog.at_level("WARNING"):
        config = Config()

    assert "Unsupported TOKEN_PLACE_RELAY_UPSTREAMS format" in caplog.text
    assert config.get("relay.server_pool") == [config.get("relay.server_url")]


def test_server_pool_defaults_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no overrides exist, defaults are normalised consistently."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")

    from config import Config

    config = Config()

    assert config.get("relay.server_pool") == [
        "http://localhost:5000",
    ]
    assert config.get("relay.server_pool_secondary") == []


def test_normalise_promotes_pool_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first pool entry is promoted when server_url is missing."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")

    from config import Config

    config = Config()

    relay = config.config["relay"]
    relay["server_pool"] = [
        "https://primary.example.com", "https://secondary.example.net",
    ]
    relay["server_url"] = ""

    config._normalise_relay_server_pool()

    assert config.get("relay.server_url") == "https://primary.example.com"
    assert config.get("relay.server_pool") == [
        "https://primary.example.com",
        "https://secondary.example.net",
    ]


def test_normalise_reorders_pool_to_match_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """server_url is inserted at the front of the pool when mismatched."""
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")

    from config import Config

    config = Config()

    relay = config.config["relay"]
    relay["server_url"] = "https://primary.example.com/"
    relay["server_pool"] = [
        "https://secondary.example.net",
        "https://primary.example.com/",
    ]

    config._normalise_relay_server_pool()

    assert config.get("relay.server_pool") == [
        "https://primary.example.com",
        "https://secondary.example.net",
    ]
    assert config.get("relay.server_pool_secondary") == [
        "https://secondary.example.net",
    ]


def test_cluster_only_env_true_and_registration_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boolean cluster-only overrides and registration tokens are applied."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_CLUSTER_ONLY", "TrUE")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", "  secret-token  ")

    from config import Config

    config = Config()

    assert config.get("relay.cluster_only") is True
    assert config.get("relay.server_registration_token") == "secret-token"


def test_cluster_only_env_invalid_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid cluster-only overrides are ignored with a warning."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_CLUSTER_ONLY", "sometimes")

    from config import Config

    with caplog.at_level("WARNING"):
        config = Config()

    assert "Invalid TOKEN_PLACE_RELAY_CLUSTER_ONLY value" in caplog.text
    assert config.get("relay.cluster_only") is False


def test_cloudflare_fallbacks_merge_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cloudflare fallback URLs combine config and environment overrides."""

    config_path = tmp_path / "user_config.json"
    config_path.write_text(
        json.dumps(
            {
                "relay": {
                    "cloudflare_fallback_urls": [
                        " https://relay.cloudflare.workers.dev/api/v1/ ",
                    ]
                }
            }
        )
    )

    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKEN_PLACE_CONFIG", str(config_path))
    monkeypatch.setenv(
        "TOKEN_PLACE_RELAY_CLOUDFLARE_URLS",
        '["https://relay.cloudflare.workers.dev/api/v1", "https://beta.cloudflare.workers.dev/api/v1/"]',
    )
    monkeypatch.setenv(
        "TOKEN_PLACE_RELAY_CLOUDFLARE_URL",
        "https://single.cloudflare.workers.dev/api/v1/",
    )

    from config import Config

    config = Config()

    assert config.get("relay.cloudflare_fallback_urls") == [
        "https://relay.cloudflare.workers.dev/api/v1",
        "https://beta.cloudflare.workers.dev/api/v1",
        "https://single.cloudflare.workers.dev/api/v1",
    ]


def test_cloudflare_fallbacks_ignore_blank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank Cloudflare overrides are ignored, leaving the list empty."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_CLOUDFLARE_URLS", "   ")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_CLOUDFLARE_URL", " \t ")

    from config import Config

    config = Config()

    assert config.get("relay.cloudflare_fallback_urls") == []
