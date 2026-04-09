from types import SimpleNamespace

import pytest

from api.v1 import routes
from relay import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _allow_policy(_messages):
    return SimpleNamespace(allowed=True, matched_term=None, reason=None)


def test_chat_completions_supports_distributed_provider_happy_path(client, monkeypatch):
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_URL", "https://distributed.example")
    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(routes, "validate_model_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "resolve_model_alias", lambda model_id: None)
    monkeypatch.setattr(routes, "evaluate_messages_for_policy", _allow_policy)
    monkeypatch.setattr(
        routes,
        "get_model_instance",
        lambda _model_id: (_ for _ in ()).throw(AssertionError("should not require local model")),
    )

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Distributed hello",
                        }
                    }
                ]
            }

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("utils.compute_provider.requests.post", fake_post)

    response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["choices"][0]["message"]["content"] == "Distributed hello"
    assert captured["url"] == "https://distributed.example/api/v1/chat/completions"


def test_chat_completions_distributed_provider_falls_back_to_local_generation(client, monkeypatch):
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_URL", "https://distributed.example")
    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(routes, "validate_model_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "resolve_model_alias", lambda model_id: None)
    monkeypatch.setattr(routes, "evaluate_messages_for_policy", _allow_policy)

    monkeypatch.setattr(
        "utils.compute_provider.requests.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    def fake_local_generate(_model_id, messages, **_options):
        return list(messages) + [{"role": "assistant", "content": "Local fallback"}]

    monkeypatch.setattr(routes, "generate_response", fake_local_generate)

    response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["choices"][0]["message"]["content"] == "Local fallback"
