from types import SimpleNamespace

import pytest
import requests

from api.v1 import routes
from relay import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def _patch_route_dependencies(monkeypatch):
    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(routes, "validate_model_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(routes, "resolve_model_alias", lambda model_id: None)
    monkeypatch.setattr(
        routes,
        "evaluate_messages_for_policy",
        lambda messages: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(routes, "_configured_relay_servers", lambda: ["http://compute-1"])


def test_chat_completion_uses_distributed_compute_provider(client, monkeypatch):
    _patch_route_dependencies(monkeypatch)
    monkeypatch.setenv(routes.COMPUTE_PROVIDER_ENV, routes.COMPUTE_PROVIDER_DISTRIBUTED)

    def _unexpected_local(*args, **kwargs):
        raise AssertionError("local generate_response should not be used in distributed happy path")

    monkeypatch.setattr(routes, "generate_response", _unexpected_local)

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Distributed provider response",
                        }
                    }
                ]
            }

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: _FakeResponse())

    response = client.post(
        "/api/v1/chat/completions",
        json={"model": "llama-3-8b-instruct", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["choices"][0]["message"]["content"] == "Distributed provider response"


def test_chat_completion_distributed_fallbacks_to_local(client, monkeypatch):
    _patch_route_dependencies(monkeypatch)
    monkeypatch.setenv(routes.COMPUTE_PROVIDER_ENV, routes.COMPUTE_PROVIDER_DISTRIBUTED)
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.RequestException("boom")),
    )
    monkeypatch.setattr(
        routes,
        "generate_response",
        lambda model_id, messages, **options: messages
        + [{"role": "assistant", "content": "Local fallback response"}],
    )

    response = client.post(
        "/api/v1/chat/completions",
        json={"model": "llama-3-8b-instruct", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["choices"][0]["message"]["content"] == "Local fallback response"
