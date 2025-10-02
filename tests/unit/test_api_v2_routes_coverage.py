import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from api.v1.models import ModelError
from api.v1.validation import ValidationError
from api.v2 import routes as v2_routes
from relay import app as relay_app


@pytest.fixture
def client():
    relay_app.config["TESTING"] = True
    with relay_app.test_client() as test_client:
        yield test_client


def test_routes_configures_null_logger_in_prod(monkeypatch):
    """Reload the module with ENVIRONMENT=prod to cover the production branch."""

    module_path = Path(v2_routes.__file__)
    monkeypatch.setenv("ENVIRONMENT", "prod")

    spec = importlib.util.spec_from_file_location("api.v2.routes_prod", module_path)
    temp_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = temp_module
    try:
        spec.loader.exec_module(temp_module)  # type: ignore[union-attr]
        assert temp_module.ENVIRONMENT == "prod"
    finally:
        sys.modules.pop(spec.name, None)
        monkeypatch.delenv("ENVIRONMENT", raising=False)


def test_log_error_invokes_logger(monkeypatch):
    """Ensure log_error emits through the logger when not in production."""

    calls = []

    class DummyLogger:
        def error(self, message, exc_info=False):
            calls.append((message, exc_info))

    monkeypatch.setattr(v2_routes, "ENVIRONMENT", "dev")
    monkeypatch.setattr(v2_routes, "logger", DummyLogger())

    v2_routes.log_error("boom", exc_info=True)

    assert calls == [("boom", True)]


def test_format_error_response_includes_optional_fields():
    with relay_app.app_context():
        response = v2_routes.format_error_response(
            "failed",
            error_type="test_error",
            param="messages",
            code="invalid",
            status_code=418,
        )

        assert response.status_code == 418
        payload = response.get_json()
        assert payload == {
            "error": {
                "message": "failed",
                "type": "test_error",
                "param": "messages",
                "code": "invalid",
            }
        }


def test_list_models_success_path(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])

    response = client.get("/api/v2/models")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["object"] == "list"
    assert payload["data"][0]["id"] == "alpha"


def test_get_model_success(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])

    response = client.get("/api/v2/models/alpha")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["id"] == "alpha"


def test_get_model_not_found(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [])

    response = client.get("/api/v2/models/beta")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"]["code"] == "model_not_found"


def test_list_community_providers_success(client, monkeypatch):
    directory = {
        "providers": [{"id": "relay-1", "name": "Relay"}],
        "updated": "2024-01-01",
    }
    monkeypatch.setattr(v2_routes, "get_community_provider_directory", lambda: directory)

    response = client.get("/api/v2/community/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"] == directory["providers"]
    assert payload["updated"] == "2024-01-01"


def test_list_community_providers_error(client, monkeypatch):
    def _raise():
        raise v2_routes.CommunityDirectoryError("boom")

    monkeypatch.setattr(v2_routes, "get_community_provider_directory", _raise)

    response = client.get("/api/v2/community/providers")

    assert response.status_code == 500
    assert response.get_json()["error"]["message"] == "Community directory temporarily unavailable"


def test_list_server_providers_success(client, monkeypatch):
    directory = {
        "providers": [{"id": "server-1", "name": "Server"}],
        "metadata": {"region": "global"},
    }
    monkeypatch.setattr(v2_routes, "get_provider_directory", lambda: directory)

    response = client.get("/api/v2/server-providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["metadata"] == {"region": "global"}


def test_list_server_providers_error(client, monkeypatch):
    def _raise():
        raise v2_routes.ProviderRegistryError("offline")

    monkeypatch.setattr(v2_routes, "get_provider_directory", _raise)

    response = client.get("/api/v2/server-providers")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["code"] == "provider_registry_unavailable"


def test_chat_completion_missing_body(client):
    response = client.post(
        "/api/v2/chat/completions",
        data="null",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["message"].startswith("Invalid request body")


def _base_encrypted_payload():
    return {
        "model": "alpha",
        "encrypted": True,
        "stream": False,
        "client_public_key": "client",
        "messages": {
            "ciphertext": "YQ==",
            "cipherkey": "Yg==",
            "iv": "Yw==",
        },
    }


def _setup_model_stubs(monkeypatch):
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])
    monkeypatch.setattr(v2_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v2_routes, "validate_chat_messages", lambda messages: None)


def _allow_policy(monkeypatch):
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )


def test_chat_completion_decrypt_failure(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)
    monkeypatch.setattr(v2_routes, "validate_encrypted_request", lambda data: None)

    class DummyEncryption:
        public_key_b64 = "server"

        def decrypt_message(self, payload, cipherkey):
            return None

    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryption())

    response = client.post("/api/v2/chat/completions", json=_base_encrypted_payload())

    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == "Failed to decrypt messages"


def test_chat_completion_bad_json_after_decrypt(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)
    monkeypatch.setattr(v2_routes, "validate_encrypted_request", lambda data: None)

    class DummyEncryption:
        public_key_b64 = "server"

        def decrypt_message(self, payload, cipherkey):
            return b"not-json"

    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryption())

    response = client.post("/api/v2/chat/completions", json=_base_encrypted_payload())

    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == "Failed to parse JSON from decrypted messages"


def test_chat_completion_encrypted_validation_error(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)

    def _raise_validation_error(data):
        raise ValidationError("bad", field="messages", code="invalid")

    monkeypatch.setattr(v2_routes, "validate_encrypted_request", _raise_validation_error)

    response = client.post("/api/v2/chat/completions", json=_base_encrypted_payload())

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["code"] == "invalid"


def test_chat_completion_standard_validation_error(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)

    def _validate_required_fields(data, fields):
        if fields == ["model"]:
            return None
        raise ValidationError("missing", field="messages", code="missing_field")

    monkeypatch.setattr(v2_routes, "validate_required_fields", _validate_required_fields)
    monkeypatch.setattr(v2_routes, "validate_field_type", lambda *a, **k: None)

    payload = {
        "model": "alpha",
        "messages": None,
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["code"] == "missing_field"


def test_chat_completion_message_validation_failure(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    def _raise_validation(messages):
        raise ValidationError("bad", field="messages", code="invalid")

    monkeypatch.setattr(v2_routes, "validate_chat_messages", _raise_validation)

    request_payload = {
        "model": "alpha",
        "messages": [{"role": "user", "content": "hi"}],
    }

    response = client.post("/api/v2/chat/completions", json=request_payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid"


def test_chat_completion_blocked_by_policy(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=False, matched_term="term", reason="nope"),
    )

    payload = {
        "model": "alpha",
        "messages": [{"role": "user", "content": "hi"}],
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "content_blocked"


def test_chat_completion_standard_response(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)
    monkeypatch.setattr(
        v2_routes,
        "generate_response",
        lambda model_id, messages, **opts: messages + [{"role": "assistant", "content": "ok"}],
    )

    payload = {
        "model": "alpha",
        "messages": [{"role": "user", "content": "hi"}],
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json()["choices"][0]["message"]["content"] == "ok"


def test_chat_completion_tool_serialization_handles_non_dict_function(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)

    def fake_generate_response(model_id, messages, **options):
        return messages + [
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {"id": "call", "type": "function", "function": "not-a-dict"}
                ],
            }
        ]

    monkeypatch.setattr(v2_routes, "generate_response", fake_generate_response)

    payload = {
        "model": "alpha",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    chunks = [chunk.decode("utf-8") for chunk in response.iter_encoded() if chunk.strip()]
    assert any("tool_calls" in chunk for chunk in chunks)


def test_chat_completion_encryption_failure_after_generation(client, monkeypatch):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)

    class DummyEncryption:
        public_key_b64 = "server"

        def decrypt_message(self, payload, cipherkey):
            messages = [{"role": "user", "content": "hi"}]
            return json.dumps(messages).encode("utf-8")

        def encrypt_message(self, response, client_public_key):
            return None

    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryption())
    monkeypatch.setattr(v2_routes, "validate_encrypted_request", lambda data: None)
    monkeypatch.setattr(
        v2_routes,
        "generate_response",
        lambda model_id, messages, **opts: messages + [{"role": "assistant", "content": "ok"}],
    )

    payload = _base_encrypted_payload()

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 500
    assert response.get_json()["error"]["message"] == "Failed to encrypt response"


def test_chat_completion_model_error(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])

    def _raise_model_error(model_id):
        raise ModelError("no model", status_code=400, error_type="model_not_found")

    monkeypatch.setattr(v2_routes, "get_model_instance", _raise_model_error)

    payload = {
        "model": "alpha",
        "messages": [{"role": "user", "content": "hi"}],
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "model_error"


def test_chat_completion_top_level_validation_error(client, monkeypatch):
    def _raise_validation_error(data, required):
        raise ValidationError("missing", field="model", code="missing_model")

    monkeypatch.setattr(v2_routes, "validate_required_fields", _raise_validation_error)

    payload = {
        "messages": [{"role": "user", "content": "hi"}],
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "missing_model"


def test_completion_missing_body(client):
    response = client.post(
        "/api/v2/completions",
        data="null",
        content_type="application/json",
    )
    assert response.status_code == 400


def test_completion_missing_model_id(client):
    payload = {"prompt": "hi"}
    response = client.post("/api/v2/completions", json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"]["param"] == "model"


def test_completion_model_lookup_failure(client, monkeypatch):
    def _raise_model_error(model_id):
        raise ModelError("missing", status_code=404, error_type="model_not_found")

    monkeypatch.setattr(v2_routes, "get_model_instance", _raise_model_error)

    payload = {"model": "alpha", "prompt": "hi"}

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "model_not_found"


def test_completion_policy_blocked(client, monkeypatch):
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=False, matched_term="term", reason="blocked"),
    )
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    payload = {"model": "alpha", "prompt": "hi"}

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "content_blocked"


def test_completion_standard_response(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )
    monkeypatch.setattr(
        v2_routes,
        "generate_response",
        lambda model_id, messages: messages + [{"role": "assistant", "content": "ok"}],
    )

    payload = {"model": "alpha", "prompt": "hi"}

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json()["choices"][0]["text"] == "ok"


def test_completion_success_and_encryption_failure(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )

    class DummyEncryption:
        public_key_b64 = "server"

        def encrypt_message(self, response, client_public_key):
            return None

    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryption())
    monkeypatch.setattr(
        v2_routes,
        "generate_response",
        lambda model_id, messages: messages + [{"role": "assistant", "content": "ok"}],
    )

    payload = {
        "model": "alpha",
        "prompt": "hi",
        "encrypted": True,
        "client_public_key": "client",
    }

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 500
    assert response.get_json()["error"]["message"] == "Failed to encrypt response"


def test_completion_success_encrypted_response(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )

    class DummyEncryption:
        public_key_b64 = "server"

        def encrypt_message(self, response, client_public_key):
            return {"ciphertext": "enc"}

    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryption())
    monkeypatch.setattr(
        v2_routes,
        "generate_response",
        lambda model_id, messages: messages + [{"role": "assistant", "content": "ok"}],
    )

    payload = {
        "model": "alpha",
        "prompt": "hi",
        "encrypted": True,
        "client_public_key": "client",
    }

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 200
    assert response.get_json()["encrypted"] is True


def test_completion_generation_model_error(client, monkeypatch):
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )

    def _raise_model_error(model_id, messages):
        raise ModelError("fail", status_code=503, error_type="model_unavailable")

    monkeypatch.setattr(v2_routes, "generate_response", _raise_model_error)

    payload = {"model": "alpha", "prompt": "hi"}

    response = client.post("/api/v2/completions", json=payload)

    assert response.status_code == 503
    assert response.get_json()["error"]["type"] == "model_unavailable"


def test_completion_top_level_model_validation_error(client, monkeypatch):
    response = client.post("/api/v2/completions", json={"prompt": "hi"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["message"] == "Missing required parameter: model"
    assert payload["error"]["param"] == "model"


def test_openai_alias_routes_delegate(client, monkeypatch):
    calls = []

    def record(name):
        def _inner(*args, **kwargs):
            calls.append(name)
            if name.startswith("create_"):
                return v2_routes.format_error_response("ok", status_code=200)
            return v2_routes.format_error_response("ok", status_code=200)

        return _inner

    monkeypatch.setattr(v2_routes, "list_models", record("list_models"))
    monkeypatch.setattr(v2_routes, "get_model", record("get_model"))
    monkeypatch.setattr(v2_routes, "get_public_key", record("get_public_key"))
    monkeypatch.setattr(v2_routes, "create_chat_completion", record("create_chat_completion"))
    monkeypatch.setattr(v2_routes, "create_completion", record("create_completion"))
    monkeypatch.setattr(v2_routes, "health_check", record("health_check"))

    client.get("/v2/models")
    client.get("/v2/models/alpha")
    client.get("/v2/public-key")
    client.post("/v2/chat/completions")
    client.post("/v2/completions")
    client.get("/v2/health")

    assert calls == [
        "list_models",
        "get_model",
        "get_public_key",
        "create_chat_completion",
        "create_completion",
        "health_check",
    ]
