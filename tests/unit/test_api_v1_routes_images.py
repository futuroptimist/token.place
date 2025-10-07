import base64
import pytest

from relay import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_image_generation_returns_png_payload(client):
    payload = {
        "prompt": "vibrant sunrise over the ocean",
        "size": "64x64",
        "seed": 42,
    }
    response = client.post("/api/v1/images/generations", json=payload)
    assert response.status_code == 200

    data = response.get_json()
    assert "data" in data and isinstance(data["data"], list)
    assert data["data"], "expected at least one image entry"

    image_entry = data["data"][0]
    assert image_entry["revised_prompt"] == payload["prompt"]

    binary = base64.b64decode(image_entry["b64_json"], validate=True)
    assert binary.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(binary) > 100


def test_image_generation_requires_prompt(client):
    response = client.post("/api/v1/images/generations", json={})
    assert response.status_code == 400

    error = response.get_json()["error"]
    assert "prompt" in error["message"].lower()
    assert error["type"] == "invalid_request_error"
