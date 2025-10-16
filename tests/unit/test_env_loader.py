"""Tests for the environment file loader utilities."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from utils.env_loader import EXPLICIT_ENV_FILE_VAR, load_project_env


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure test-specific variables are cleared after each case."""

    tracked = {
        "FOO",
        "BAR",
        "TOKEN_PLACE_ENV",
        EXPLICIT_ENV_FILE_VAR,
    }

    for key in tracked:
        monkeypatch.delenv(key, raising=False)

    yield

    for key in tracked:
        monkeypatch.delenv(key, raising=False)


def test_loads_base_env_file(tmp_path: Path) -> None:
    """Values from `.env` are applied when no OS overrides exist."""

    env_file = tmp_path / ".env"
    env_file.write_text("FOO=base\nTOKEN_PLACE_ENV=testing\n")

    result = load_project_env(root=tmp_path)

    assert result.loaded_files == (env_file,)
    assert os.environ["FOO"] == "base"
    assert result.resolved_env == "testing"


def test_local_file_overrides_base(tmp_path: Path) -> None:
    """`.env.local` takes precedence over values from `.env`."""

    (tmp_path / ".env").write_text("FOO=base\n")
    local_file = tmp_path / ".env.local"
    local_file.write_text("FOO=local\n")

    result = load_project_env(root=tmp_path)

    assert result.loaded_files[-1] == local_file
    assert os.environ["FOO"] == "local"


def test_environment_specific_file_loaded(tmp_path: Path) -> None:
    """Environment specific files are loaded when an env is provided."""

    env_specific = tmp_path / ".env.production"
    env_specific.write_text("BAR=from-production\n")

    result = load_project_env(env="production", root=tmp_path)

    assert env_specific in result.loaded_files
    assert os.environ["BAR"] == "from-production"


def test_existing_environment_values_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing process environment values win over file contents."""

    (tmp_path / ".env").write_text("FOO=base\n")
    monkeypatch.setenv("FOO", "already-set")

    load_project_env(root=tmp_path)

    assert os.environ["FOO"] == "already-set"


def test_explicit_env_file_loaded_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit env file path is honoured with highest precedence."""

    (tmp_path / ".env").write_text("FOO=base\n")
    explicit = tmp_path / "custom.env"
    explicit.write_text("FOO=custom\n")

    monkeypatch.setenv(EXPLICIT_ENV_FILE_VAR, str(explicit))

    result = load_project_env(root=tmp_path)

    assert result.loaded_files[-1] == explicit
    assert os.environ["FOO"] == "custom"
