"""Tests for the environment file loader utilities."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from utils import env_loader
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


def test_explicit_env_file_missing_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing explicit env file emits a warning and is skipped."""

    explicit = tmp_path / "missing.env"

    with caplog.at_level("WARNING"):
        result = load_project_env(root=tmp_path, explicit=explicit)

    assert explicit not in result.loaded_files
    assert result.explicit_file == explicit
    assert "does not exist" in caplog.text


def test_normalise_explicit_path_resolution_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Resolution failures while normalising explicit paths are handled gracefully."""

    class FailingPath:
        def __init__(self, value: object) -> None:
            self.value = value

        def expanduser(self) -> "FailingPath":
            return self

        def resolve(self) -> Path:
            raise OSError("boom")

    monkeypatch.setattr(env_loader, "Path", FailingPath)

    with caplog.at_level("WARNING"):
        resolved = env_loader._normalise_explicit_path("~/bad.env")

    assert resolved is None
    assert "Unable to resolve explicit env file path" in caplog.text


def test_merge_skips_when_reading_env_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Failed env file reads log a warning and do not apply values."""

    (tmp_path / ".env").write_text("FOO=base\n")

    def failing_dotenv(_: Path) -> dict[str, str]:
        raise OSError("cannot read")

    monkeypatch.setattr(env_loader, "dotenv_values", failing_dotenv)

    with caplog.at_level("WARNING"):
        result = load_project_env(root=tmp_path)

    assert "Failed to read env file" in caplog.text
    assert "FOO" not in result.applied_values
    assert result.loaded_files == ()


def test_merge_skips_empty_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty env files are ignored without affecting the result."""

    (tmp_path / ".env").write_text("FOO=base\n")

    monkeypatch.setattr(env_loader, "dotenv_values", lambda _: {})

    result = load_project_env(root=tmp_path)

    assert "FOO" not in result.applied_values
    assert result.loaded_files == ()
