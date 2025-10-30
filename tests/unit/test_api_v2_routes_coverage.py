import importlib.util
import json
import sys
import base64
import json
import types
from pathlib import Path

import pytest

import encrypt
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
    assert payload["metadata"] == {"updated_at": "2024-01-01"}


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


def test_get_service_name_defaults_to_module_constant(monkeypatch):
    """When no override is configured the module constant should be returned."""

    monkeypatch.delenv("SERVICE_NAME", raising=False)
    monkeypatch.setattr(v2_routes, "SERVICE_NAME", "token.place")

    assert v2_routes._get_service_name() == "token.place"


def test_get_service_name_strips_override(monkeypatch):
    """Whitespace around SERVICE_NAME overrides should be ignored."""

    monkeypatch.setattr(v2_routes, "SERVICE_NAME", "token.place")
    monkeypatch.setenv("SERVICE_NAME", "  relay.alpha  ")

    assert v2_routes._get_service_name() == "relay.alpha"


def test_get_service_name_falls_back_for_blank_override(monkeypatch):
    """Blank overrides should fall back to the module constant."""

    monkeypatch.setattr(v2_routes, "SERVICE_NAME", "token.place")
    monkeypatch.setenv("SERVICE_NAME", "   ")

    assert v2_routes._get_service_name() == "token.place"


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


def _streaming_encrypted_payload():
    return {
        "model": "alpha",
        "encrypted": True,
        "stream": True,
        "client_public_key": base64.b64encode(b"client-key").decode("ascii"),
        "messages": {
            "ciphertext": base64.b64encode(b"ciphertext").decode("ascii"),
            "cipherkey": base64.b64encode(b"cipherkey").decode("ascii"),
            "iv": base64.b64encode(b"iv").decode("ascii"),
        },
    }


def _configure_encrypted_streaming(monkeypatch, *, assistant_content="Hello!"):
    _setup_model_stubs(monkeypatch)
    _allow_policy(monkeypatch)
    monkeypatch.setattr(v2_routes, "validate_encrypted_request", lambda data: None)

    class DummyEncryption:
        public_key_b64 = base64.b64encode(b"server-key").decode("ascii")

        def decrypt_message(self, payload, cipherkey):
            return json.dumps(
                [
                    {"role": "system", "content": "You are a unit test."},
                    {"role": "user", "content": "Say hello."},
                ]
            ).encode("utf-8")

        def encrypt_message(self, response, client_public_key):
            return {"ciphertext": "unused"}

    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryption())
    monkeypatch.setattr(
        v2_routes,
        "generate_response",
        lambda model_id, messages, **options: messages
        + [{"role": "assistant", "content": assistant_content}],
    )


def test_chat_completion_encrypted_streaming_requires_client_key_unit(client, monkeypatch):
    _configure_encrypted_streaming(monkeypatch)
    payload = _streaming_encrypted_payload()
    payload["client_public_key"] = ""

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["type"] == "encryption_error"
    assert body["error"]["message"] == "Client public key required for encrypted streaming"


def test_chat_completion_encrypted_streaming_rejects_invalid_client_key_base64(client, monkeypatch):
    _configure_encrypted_streaming(monkeypatch)
    payload = _streaming_encrypted_payload()
    payload["client_public_key"] = "!!!not-base64!!!"

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["type"] == "encryption_error"
    assert body["error"]["message"] == "Client public key is not valid base64"


def test_chat_completion_encrypted_streaming_emits_encrypted_chunks_unit(client, monkeypatch):
    _configure_encrypted_streaming(monkeypatch)
    payload = _streaming_encrypted_payload()

    calls = []

    def stub_encrypt_stream_chunk(plaintext, client_key_bytes, *, session=None, **kwargs):
        assert isinstance(client_key_bytes, (bytes, bytearray))
        calls.append((plaintext, client_key_bytes))
        if session is None:
            session = types.SimpleNamespace(associated_data=b"meta-ad")
            return (
                {"ciphertext": b"chunk-1", "iv": b"iv-1", "tag": b"tag-1", "mode": "GCM"},
                b"cipherkey-1",
                session,
            )
        return (
            {"ciphertext": b"chunk-2", "iv": b"iv-2"},
            None,
            session,
        )

    monkeypatch.setattr(encrypt, "encrypt_stream_chunk", stub_encrypt_stream_chunk)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    events = [chunk.decode("utf-8").strip() for chunk in response.iter_encoded() if chunk.strip()]
    assert events[-1] == "data: [DONE]"

    first_event = events[0]
    assert first_event.startswith("data: ")
    envelope = json.loads(first_event[len("data: "):])

    assert envelope["event"] == "delta"
    assert envelope["encrypted"] is True
    payload_dict = envelope["data"]
    assert payload_dict["encrypted"] is True
    assert base64.b64decode(payload_dict["ciphertext"]) == b"chunk-1"
    assert base64.b64decode(payload_dict["iv"]) == b"iv-1"
    assert base64.b64decode(payload_dict["cipherkey"]) == b"cipherkey-1"
    assert payload_dict["mode"] == "GCM"
    assert base64.b64decode(payload_dict["tag"]) == b"tag-1"
    assert base64.b64decode(payload_dict["associated_data"]) == b"meta-ad"
    assert envelope["stream_session_id"] == payload_dict["stream_session_id"]
    assert calls, "Expected encrypt_stream_chunk to be invoked"


def test_chat_completion_encrypted_streaming_serialization_failure_unit(client, monkeypatch):
    _configure_encrypted_streaming(monkeypatch)
    payload = _streaming_encrypted_payload()

    standard_dumps = json.dumps
    triggered = {"value": False}

    def failing_dumps(obj, **kwargs):
        if kwargs:
            return standard_dumps(obj, **kwargs)
        if not triggered["value"]:
            triggered["value"] = True
            raise TypeError("boom")
        return standard_dumps(obj, **kwargs)

    monkeypatch.setattr(v2_routes.json, "dumps", failing_dumps)

    monkeypatch.setattr(
        v2_routes,
        "encryption_manager",
        types.SimpleNamespace(
            public_key_b64=base64.b64encode(b"server-key").decode("ascii"),
            decrypt_message=lambda payload, cipherkey: standard_dumps(
                [
                    {"role": "system", "content": "You are a unit test."},
                    {"role": "user", "content": "Say hello."},
                ]
            ).encode("utf-8"),
            encrypt_message=lambda response, client_public_key: {"ciphertext": "unused"},
        ),
    )

    def passthrough_encrypt_stream_chunk(*args, **kwargs):
        session = kwargs.get("session")
        if session is None:
            session = types.SimpleNamespace(associated_data=b"")
        return ({"ciphertext": b"chunk", "iv": b"iv"}, None, session)

    monkeypatch.setattr(encrypt, "encrypt_stream_chunk", passthrough_encrypt_stream_chunk)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    chunks = [chunk.decode("utf-8").strip() for chunk in response.iter_encoded() if chunk.strip()]
    assert chunks, "Expected at least one chunk"

    first_chunk = chunks[0]
    assert first_chunk.startswith("data: ")
    error_payload = json.loads(first_chunk[len("data: "):])
    assert error_payload == {"event": "error", "reason": "serialization_failed"}


def test_chat_completion_encrypted_streaming_encryption_failure_unit(client, monkeypatch):
    _configure_encrypted_streaming(monkeypatch)
    payload = _streaming_encrypted_payload()

    def failing_encrypt(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(encrypt, "encrypt_stream_chunk", failing_encrypt)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    chunks = [chunk.decode("utf-8").strip() for chunk in response.iter_encoded() if chunk.strip()]
    assert chunks, "Expected an error chunk"

    first_chunk = chunks[0]
    assert first_chunk.startswith("data: ")
    error_payload = json.loads(first_chunk[len("data: "):])
    assert error_payload == {"event": "error", "reason": "encryption_failed"}


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
        lambda model_id, messages, **_kwargs: messages
        + [{"role": "assistant", "content": "ok"}],
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
        lambda model_id, messages, **_kwargs: messages
        + [{"role": "assistant", "content": "ok"}],
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
    def _raise_validation_error(data, _required):
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
