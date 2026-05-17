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


def test_validate_chat_messages_rejects_empty_image_block():
    """v2 image blocks must include an analyzable payload before runtime use."""

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this."},
                {"type": "image"},
            ],
        }
    ]

    with pytest.raises(v2_routes.ValidationError) as exc:
        v2_routes.validate_chat_messages(messages)

    assert exc.value.field == "messages"
    assert "image must be an object" in exc.value.message


def test_chat_completion_rejects_empty_image_block_before_runtime(client, monkeypatch):
    """Malformed v2 images should fail validation instead of reaching API v1 runtime."""

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])
    monkeypatch.setattr(v2_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    def _unexpected_generate(*_args, **_kwargs):
        raise AssertionError("generate_response should not be called")

    monkeypatch.setattr(v2_routes, "generate_response", _unexpected_generate)

    payload = {
        "model": "alpha",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this."},
                    {"type": "image"},
                ],
            }
        ],
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    data = response.get_json()
    assert data["error"]["param"] == "messages"
    assert "image must be an object" in data["error"]["message"]
