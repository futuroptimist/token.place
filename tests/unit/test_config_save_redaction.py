"""Tests for redacting sensitive config values when saving user config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Config, SensitiveKey


@pytest.fixture(name="config_tmp_file")
def fixture_config_tmp_file(tmp_path: Path) -> Path:
    """Return a path for writing config files inside a temporary directory."""

    file_path = tmp_path / "user_config.json"
    return file_path


def test_save_user_config_redacts_registration_token(config_tmp_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving config should not persist sensitive relay tokens to disk."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    config = Config(env="testing")
    config.set("relay.server_registration_token", "  super-secret-token  ")

    config.save_user_config(str(config_tmp_file))

    assert config_tmp_file.exists()
    saved = json.loads(config_tmp_file.read_text())
    assert saved["relay"].get("server_registration_token") is None


def test_redacted_config_copy_handles_non_dict_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redaction gracefully skips sensitive keys when structure changes."""

    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    config = Config(env="testing")
    # Force an unexpected structure so traversal encounters non-dict entries.
    config.set("relay", ["unexpected-structure"])
    monkeypatch.setattr(
        "config.SENSITIVE_CONFIG_KEYS",
        [SensitiveKey("relay.updated.server_registration_token")],
        raising=False,
    )

    redacted = config._redacted_config_copy()

    # The method should still return a deepcopy without mutating the list value.
    assert redacted["relay"] == ["unexpected-structure"]
    assert redacted is not config.config
