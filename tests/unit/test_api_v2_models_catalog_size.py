"""Regression tests guarding the API v2 model catalogue size."""

import pytest


@pytest.fixture
def client():
    from relay import app

    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


EXPECTED_MODEL_IDS = {
    "llama-3-8b-instruct",
    "llama-3-8b-instruct:alignment",
    "gpt-oss-20b",
    "mistral-7b-instruct",
    "mixtral-8x7b-instruct",
    "phi-3-mini-4k-instruct",
    "mistral-nemo-instruct",
    "qwen2.5-7b-instruct",
    "qwen2.5-coder-7b-instruct",
    "gemma-2-9b-it",
    "codegemma-7b",
    "smollm2-1.7b-instruct",
}


def test_api_v2_models_catalog_matches_curated_set(client):
    response = client.get("/api/v2/models")
    assert response.status_code == 200

    payload = response.get_json()
    entries = payload["data"]

    model_ids = [item["id"] for item in entries]
    assert len(model_ids) == len(set(model_ids)), "Model identifiers must be unique"

    assert set(model_ids) == EXPECTED_MODEL_IDS
