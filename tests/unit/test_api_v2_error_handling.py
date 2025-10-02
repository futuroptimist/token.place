import types

import pytest

from api.v2 import routes as v2_routes
from relay import app as relay_app


@pytest.fixture
def client():
    relay_app.config["TESTING"] = True
    with relay_app.test_client() as test_client:
        yield test_client


def _recording_logger():
    calls = []

    def _log(message, exc_info=False):
        calls.append((message, exc_info))

    return calls, _log


def _boom():
    raise RuntimeError("boom")


def test_list_models_hides_internal_error_details(client, monkeypatch):
    calls, fake_log_error = _recording_logger()
    monkeypatch.setattr(v2_routes, "log_error", fake_log_error)
    monkeypatch.setattr(v2_routes, "get_models_info", _boom)

    response = client.get("/api/v2/models")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "Internal server error"
    assert calls == [("Error in list_models endpoint", True)]


def test_get_model_hides_internal_error_details(client, monkeypatch):
    calls, fake_log_error = _recording_logger()
    monkeypatch.setattr(v2_routes, "log_error", fake_log_error)
    monkeypatch.setattr(v2_routes, "get_models_info", _boom)

    response = client.get("/api/v2/models/test-model")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "Internal server error"
    assert calls == [("Error in get_model endpoint for model test-model", True)]


def test_get_public_key_hides_exception_message(client, monkeypatch):
    calls, fake_log_error = _recording_logger()

    class BrokenEncryptionManager:
        @property
        def public_key_b64(self):
            raise ValueError("sensitive secret")

    monkeypatch.setattr(v2_routes, "log_error", fake_log_error)
    monkeypatch.setattr(v2_routes, "encryption_manager", BrokenEncryptionManager())

    response = client.get("/api/v2/public-key")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "Failed to retrieve public key"
    assert calls == [("Error in get_public_key endpoint", True)]


def test_chat_completion_unexpected_error_is_sanitized(client, monkeypatch):
    calls, fake_log_error = _recording_logger()

    monkeypatch.setattr(v2_routes, "log_error", fake_log_error)
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v2_routes, "validate_chat_messages", lambda messages: None)
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )

    def _generate(model_id, messages, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(v2_routes, "generate_response", _generate)

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.4,
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "Internal server error"
    assert calls == [("Unexpected error in create_chat_completion endpoint", True)]


def test_completion_unexpected_error_is_sanitized(client, monkeypatch):
    calls, fake_log_error = _recording_logger()

    monkeypatch.setattr(v2_routes, "log_error", fake_log_error)
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v2_routes, "validate_chat_messages", lambda messages: None)
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )

    def _generate(model_id, messages, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(v2_routes, "generate_response", _generate)

    payload = {
        "model": "llama-3-8b-instruct",
        "prompt": "Say hello",
    }

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "Internal server error"
    assert calls == [("Unexpected error in create_completion endpoint", True)]


def test_health_check_hides_internal_failure(client, monkeypatch):
    calls, fake_log_error = _recording_logger()
    monkeypatch.setattr(v2_routes, "log_error", fake_log_error)

    class BrokenTime:
        @staticmethod
        def time():
            raise RuntimeError("broken clock")

    monkeypatch.setattr(v2_routes, "time", BrokenTime())

    response = client.get("/api/v2/health")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "Health check failed"
    assert calls == [("Error in health_check endpoint", True)]
