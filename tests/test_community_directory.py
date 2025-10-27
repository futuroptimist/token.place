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
        "capabilities",
        "notes",
        "endpoints",
    }
    assert expected_keys.issubset(provider.keys())
    assert isinstance(provider["endpoints"], list)

    metadata = payload.get("metadata")
    assert metadata and metadata["updated_at"]


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
    assert provider["endpoints"] == []


def test_get_provider_directory_normalises_endpoint_payload(monkeypatch, tmp_path):
    """Provider endpoints should be filtered to valid type/url dictionaries."""

    payload_path = tmp_path / "providers.json"
    payload = {
        "providers": [
            {
                "id": "endpoints-test",
                "name": "Endpoints Test",
                "region": "test-region",
                "endpoints": [
                    {"type": "relay", "url": " http://relay.example.com "},
                    {"type": "", "url": "http://invalid.example.com"},
                    "not-a-dict",
                    {"type": "server"},
                ],
            }
        ],
        "updated": "2025-03-04T00:00:00Z",
    }
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", payload_path)
    _reset_directory_cache()

    directory = community.get_provider_directory()
    provider = directory["providers"][0]

    assert provider["endpoints"] == [
        {"type": "relay", "url": "http://relay.example.com"}
    ]


def test_list_community_providers_handles_directory_error(client, monkeypatch):
    """The HTTP endpoint should convert directory errors into API responses."""

    def _raise_error() -> Path:
        raise community.CommunityDirectoryError("boom")

    monkeypatch.setattr("api.v1.routes.get_community_provider_directory", _raise_error)

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

    monkeypatch.setattr("api.v1.routes.get_community_provider_directory", _return_directory)

    response = client.get("/api/v1/community/providers")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "object": "list",
        "data": _return_directory()["providers"],
    }
    assert "metadata" not in payload


def test_normalise_provider_missing_id():
    """Test that missing id field raises CommunityDirectoryError."""
    
    provider = {
        "name": "Test Provider",
        "region": "test-region"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_missing_name():
    """Test that missing name field raises CommunityDirectoryError."""
    
    provider = {
        "id": "test-id",
        "region": "test-region"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_missing_region():
    """Test that missing region field raises CommunityDirectoryError."""
    
    provider = {
        "id": "test-id",
        "name": "Test Provider"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_empty_id():
    """Test that empty id field raises CommunityDirectoryError."""
    
    provider = {
        "id": "",
        "name": "Test Provider",
        "region": "test-region"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_empty_name():
    """Test that empty name field raises CommunityDirectoryError."""
    
    provider = {
        "id": "test-id",
        "name": "",
        "region": "test-region"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_empty_region():
    """Test that empty region field raises CommunityDirectoryError."""
    
    provider = {
        "id": "test-id",
        "name": "Test Provider",
        "region": ""
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_none_id():
    """Test that None id field raises CommunityDirectoryError."""
    
    provider = {
        "id": None,
        "name": "Test Provider",
        "region": "test-region"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_none_name():
    """Test that None name field raises CommunityDirectoryError."""
    
    provider = {
        "id": "test-id",
        "name": None,
        "region": "test-region"
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_none_region():
    """Test that None region field raises CommunityDirectoryError."""
    
    provider = {
        "id": "test-id",
        "name": "Test Provider",
        "region": None
    }
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._normalise_provider(provider)
    
    assert "Provider entry missing required fields: ('id', 'name', 'region')" in str(exc_info.value)


def test_normalise_provider_with_all_optional_fields():
    """Test normalisation with all optional fields provided."""
    
    provider = {
        "id": "test-id",
        "name": "Test Provider",
        "region": "test-region",
        "latency_ms": 100,
        "status": "online",
        "contact": {"email": "test@example.com"},
        "capabilities": ["chat", "completion"],
        "notes": "Test notes"
    }
    
    result = community._normalise_provider(provider)
    
    expected = {
        "id": "test-id",
        "name": "Test Provider",
        "region": "test-region",
        "latency_ms": 100,
        "status": "online",
        "contact": {"email": "test@example.com"},
        "capabilities": ["chat", "completion"],
        "notes": "Test notes"
    }
    
    assert result == expected


def test_load_raw_directory_invalid_json(monkeypatch, tmp_path):
    """Test that invalid JSON raises CommunityDirectoryError."""
    
    payload_path = tmp_path / "providers.json"
    payload_path.write_text("invalid json content", encoding="utf-8")
    
    monkeypatch.setattr(community, "COMMUNITY_DIRECTORY_PATH", payload_path)
    _reset_directory_cache()
    
    with pytest.raises(community.CommunityDirectoryError) as exc_info:
        community._load_raw_directory()
    
    assert "Invalid community provider directory JSON" in str(exc_info.value)


def test_normalise_provider_successful_path():
    """Test the successful path through _normalise_provider function."""
    
    provider = {
        "id": "test-id",
        "name": "Test Provider",
        "region": "test-region"
    }
    
    result = community._normalise_provider(provider)
    
    expected = {
        "id": "test-id",
        "name": "Test Provider",
        "region": "test-region",
        "latency_ms": None,
        "status": "unknown",
        "contact": {},
        "capabilities": [],
        "notes": None
    }
    
    assert result == expected


def test_list_community_providers_includes_updated_when_present(client, monkeypatch):
    """The HTTP response should include updated timestamp when provided."""

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
            "updated": "2025-03-04T00:00:00Z",
        }

    monkeypatch.setattr("api.v1.routes.get_community_provider_directory", _return_directory)

    response = client.get("/api/v1/community/providers")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload == {
        "object": "list",
        "data": _return_directory()["providers"],
        "updated": "2025-03-04T00:00:00Z",
    }
