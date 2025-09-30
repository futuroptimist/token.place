import pytest

from relay import app

app.config['TESTING'] = True


@pytest.fixture
def client():
    with app.test_client() as client:
        yield client


def test_community_provider_directory_endpoint(client):
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
