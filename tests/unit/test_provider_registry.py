"""Unit tests for the provider registry loader utilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from utils.providers import ProviderRegistryError
from utils.providers import registry


@pytest.fixture(autouse=True)
def clear_registry_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with a clean cache and no env overrides."""

    registry._reset_provider_directory_cache()
    monkeypatch.delenv("TOKEN_PLACE_PROVIDER_REGISTRY", raising=False)
    yield
    registry._reset_provider_directory_cache()
    monkeypatch.delenv("TOKEN_PLACE_PROVIDER_REGISTRY", raising=False)


def _write_registry(path: Path, payload: Dict[str, Any] | Any) -> None:
    """Helper to serialise payloads to YAML for the registry loader."""

    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _valid_provider() -> Dict[str, Any]:
    return {
        "id": "test", "name": "Test", "region": "us-test", "status": "active",
        "endpoints": [{"type": "relay", "url": "http://example"}]
    }


def test_get_provider_directory_uses_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry loader should honour TOKEN_PLACE_PROVIDER_REGISTRY overrides."""

    registry_file = tmp_path / "registry.yaml"
    _write_registry(
        registry_file,
        {
            "metadata": {"version": 1},
            "providers": [
                {
                    "id": "abc",
                    "name": "ABC",  # minimal valid provider entry
                    "region": "local",
                    "status": "active",
                    "description": "Custom registry entry",
                    "endpoints": [{"type": "relay", "url": "http://localhost"}],
                }
            ],
        },
    )

    monkeypatch.setenv("TOKEN_PLACE_PROVIDER_REGISTRY", str(registry_file))

    result = registry.get_provider_directory()

    assert result["metadata"] == {"version": 1}
    assert result["providers"][0]["description"] == "Custom registry entry"


def test_get_provider_directory_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing registry files should raise a ProviderRegistryError."""

    missing_path = tmp_path / "missing.yaml"
    monkeypatch.setenv("TOKEN_PLACE_PROVIDER_REGISTRY", str(missing_path))

    with pytest.raises(ProviderRegistryError) as exc:
        registry.get_provider_directory()

    assert "not found" in str(exc.value)


@pytest.mark.parametrize(
    "payload, expected_message",
    [
        ([{"foo": "bar"}], "Provider registry root must be a mapping"),
        ({"metadata": [1], "providers": []}, "Provider registry metadata must be a mapping"),
        ({"metadata": {}, "providers": "nope"}, "Provider registry providers must be a list"),
        ({"metadata": {}, "providers": ["bad"]}, "Each provider entry must be a mapping"),
        ({"metadata": {}, "providers": [{"id": "x", "name": "X", "region": "R"}]}, "missing required fields"),
        ({"metadata": {}, "providers": [{**_valid_provider(), "endpoints": "bad"}]}, "endpoints must be a list"),
        ({"metadata": {}, "providers": [{**_valid_provider(), "endpoints": ["bad"]}]}, "invalid endpoint definition"),
        ({"metadata": {}, "providers": [{**_valid_provider(), "endpoints": [{"type": "relay"}]}]}, "missing fields"),
    ],
)
def test_get_provider_directory_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
    expected_message: str,
) -> None:
    """Structured validation errors should surface as ProviderRegistryError."""

    registry_file = tmp_path / "registry.yaml"
    _write_registry(registry_file, payload)
    monkeypatch.setenv("TOKEN_PLACE_PROVIDER_REGISTRY", str(registry_file))

    with pytest.raises(ProviderRegistryError) as exc:
        registry.get_provider_directory()

    assert expected_message in str(exc.value)
