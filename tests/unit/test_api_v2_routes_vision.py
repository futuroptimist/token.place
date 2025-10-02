"""Vision support tests for API v2 chat completions."""

import json
import types

import pytest

from api.v2 import routes as v2_routes
from relay import app as relay_app


@pytest.fixture
def client():
    relay_app.config["TESTING"] = True
    with relay_app.test_client() as test_client:
        yield test_client


def _allow_policy(monkeypatch):
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )


def test_chat_completion_with_base64_image_returns_analysis(client, monkeypatch):
    """Requests containing base64-encoded images should receive a vision analysis."""

    base64_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y7ZlJ4AAAAASUVORK5CYII="
    )

    _allow_policy(monkeypatch)
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])
    monkeypatch.setattr(v2_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    payload = {
        "model": "alpha",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe the attachment."},
                    {"type": "input_image", "image": {"b64_json": base64_png}},
                ],
            }
        ],
    }

    response = client.post(
        "/api/v2/chat/completions",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["object"] == "chat.completion"
    message = data["choices"][0]["message"]["content"]
    assert "png" in message.lower()
    assert "1x1" in message
    assert "bytes" in message
