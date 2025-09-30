import json
from pathlib import Path

import pytest

from api.v1 import community
from relay import app

app.config['TESTING'] = True


@pytest.fixture
def client():
    with app.test_client() as client:
        yield client


def _reset_directory_cache():
    """Clear the cached provider directory across tests."""

    community.invalidate_provider_directory_cache()


def test_community_provider_directory_endpoint(client):
    _reset_directory_cache()

    response = client.get("/api/v1/community/providers")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["object"] == "list"
    assert isinstance(payload["data"], list)
    assert payload["data"], "Expected at least one community provider"

    provider = payload["data"][0]
    expected_keys = {
        "id",
        "name",
        "region",
        "latency_ms",
        "status",
        "contact",
    }
    assert expected_keys.issubset(provider.keys())


def test_get_provider_directory_missing_file(monkeypatch, tmp_path):
    """The loader should gracefully handle a missing JSON file."""

    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", tmp_path / "providers.json")
    _reset_directory_cache()

    directory = community.get_provider_directory()
    assert directory == {"providers": [], "updated": None}


def test_get_provider_directory_invalid_provider_entry(monkeypatch, tmp_path):
    """Invalid provider entries should raise a directory error."""

    payload_path = tmp_path / "providers.json"
    payload = {"providers": [{"id": "missing-fields"}]}
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", payload_path)
    _reset_directory_cache()

    with pytest.raises(community.CommunityDirectoryError):
        community.get_provider_directory()


def test_get_provider_directory_applies_optional_defaults(monkeypatch, tmp_path):
    """Optional provider fields should default to safe values."""

    payload_path = tmp_path / "providers.json"
    payload = {
        "providers": [
            {
                "id": "defaults-test",
                "name": "Defaults Test",
                "region": "test-region",
            }
        ],
        "updated": "2025-03-04T00:00:00Z",
    }
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", payload_path)
    _reset_directory_cache()

    directory = community.get_provider_directory()
    provider = directory["providers"][0]

    assert provider["latency_ms"] is None
    assert provider["status"] == "unknown"
    assert provider["contact"] == {}
    assert provider["capabilities"] == []
    assert provider["notes"] is None


def test_list_community_providers_handles_directory_error(client, monkeypatch):
    """The HTTP endpoint should convert directory errors into API responses."""

    def _raise_error() -> Path:
        raise community.CommunityDirectoryError("boom")

    monkeypatch.setattr("api.v1.routes.get_provider_directory", _raise_error)

    response = client.get("/api/v1/community/providers")
    assert response.status_code == 500

    payload = response.get_json()
    assert payload["error"]["type"] == "internal_server_error"
    assert payload["error"]["message"] == "Community directory temporarily unavailable"


def test_list_community_providers_omits_updated_when_missing(client, monkeypatch):
    """The HTTP response should only include an updated timestamp when provided."""

    def _return_directory():
        return {
            "providers": [
                {
                    "id": "relay-one",
                    "name": "Relay One",
                    "region": "moon-base",
                    "latency_ms": 12,
                    "status": "online",
                    "contact": {},
                    "capabilities": [],
                    "notes": None,
                }
            ],
            "updated": None,
        }

    monkeypatch.setattr("api.v1.routes.get_provider_directory", _return_directory)

    response = client.get("/api/v1/community/providers")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "object": "list",
        "data": _return_directory()["providers"],
    }
