"""Tests for API v2 adapter-aware model metadata."""

import pytest


@pytest.fixture
def client():
    from relay import app

    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def _find_model(payload, model_id):
    return next(item for item in payload if item["id"] == model_id)


def test_list_models_includes_adapter_metadata(client):
    response = client.get("/api/v2/models")
    assert response.status_code == 200

    data = response.get_json()
    adapter_entry = _find_model(data["data"], "llama-3-8b-instruct:alignment")

    assert adapter_entry["parent"] == "llama-3-8b-instruct"
    assert adapter_entry["root"] == "llama-3-8b-instruct"
    assert adapter_entry["metadata"]["adapter"]["share_base"] is True


def test_get_model_returns_adapter_metadata(client):
    response = client.get("/api/v2/models/llama-3-8b-instruct:alignment")
    assert response.status_code == 200

    data = response.get_json()
    assert data["parent"] == "llama-3-8b-instruct"
    assert data["root"] == "llama-3-8b-instruct"
    assert data["metadata"]["adapter"]["id"] == "llama-3-8b-instruct:alignment"
