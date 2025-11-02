"""Unit tests for community provider directory handling."""

import json

import pytest

from api.v1 import community


@pytest.fixture(autouse=True)
def clear_provider_directory_cache():
    """Ensure cached directory state does not leak between tests."""

    community.invalidate_provider_directory_cache()
    yield
    community.invalidate_provider_directory_cache()


def _write_directory_payload(tmp_path, payload):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_get_provider_directory_skips_blank_identifier(monkeypatch, tmp_path):
    """Whitespace-only provider identifiers should be ignored by the loader."""

    payload = {
        "providers": [
            {"id": "   ", "name": "Blank", "region": "nowhere"},
            {"id": "valid", "name": "Valid", "region": "somewhere"},
        ],
        "updated": "2025-01-01T00:00:00Z",
    }

    path = _write_directory_payload(tmp_path, payload)
    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", path)

    directory = community.get_provider_directory()

    assert [provider["id"] for provider in directory["providers"]] == ["valid"]
    assert directory["updated"] == "2025-01-01T00:00:00Z"


def test_get_provider_directory_reraises_for_invalid_non_blank(monkeypatch, tmp_path):
    """Invalid providers with real identifiers should surface directory errors."""

    payload = {"providers": [{"id": "present-but-invalid"}]}

    path = _write_directory_payload(tmp_path, payload)
    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", path)

    with pytest.raises(community.CommunityDirectoryError):
        community.get_provider_directory()


@pytest.mark.parametrize(
    "entry, expected",
    [
        (["not", "a", "dict"], False),
        ({"id": None}, False),
        ({"id": "valid"}, False),
        ({"id": "   "}, True),
    ],
)
def test_has_blank_identifier_classifies_entries(entry, expected):
    """_has_blank_identifier should only flag whitespace-only identifiers."""

    assert community._has_blank_identifier(entry) is expected
