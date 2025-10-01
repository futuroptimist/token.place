import pytest

from api.v1 import routes
from relay import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_chat_completion_blocked_by_content_policy(client, monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "block")
    monkeypatch.setenv("CONTENT_MODERATION_BLOCKLIST", "forbidden")

    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "test-model"}])
    monkeypatch.setattr(routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(
        routes,
        "generate_response",
        lambda model_id, messages: messages + [{"role": "assistant", "content": "ack"}],
    )

    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "This text is clearly forbidden."}],
    }

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["type"] == "content_policy_violation"
    assert body["error"]["code"] == "content_blocked"
    assert "forbidden" in body["error"]["message"].lower()


def test_text_completion_blocked_by_content_policy(client, monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "block")
    monkeypatch.setenv("CONTENT_MODERATION_BLOCKLIST", "forbidden")

    monkeypatch.setattr(routes, "get_model_instance", lambda model_id: object())

    def _generate(model_id, messages):
        return messages + [{"role": "assistant", "content": "ack"}]

    monkeypatch.setattr(routes, "generate_response", _generate)

    payload = {
        "model": "test-model",
        "prompt": "Make something forbidden",
    }

    response = client.post("/api/v1/completions", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["type"] == "content_policy_violation"
    assert body["error"]["code"] == "content_blocked"
    assert "forbidden" in body["error"]["message"].lower()
