import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

import relay
from relay import app
from api.v1 import compute_provider, routes
from api.v1.models import ModelError
from api.v1.validation import ValidationError


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_get_public_key_exception(client, monkeypatch):
    monkeypatch.setattr(routes, 'encryption_manager', MagicMock(public_key_b64=property(lambda self: (_ for _ in ()).throw(RuntimeError('fail')))))
    resp = client.get('/api/v1/public-key')
    assert resp.status_code == 400
    assert 'Failed to retrieve public key' in resp.get_json()['error']['message']


def test_chat_completion_encrypted_validation_error(client, monkeypatch):
    monkeypatch.setattr(routes, 'validate_encrypted_request', MagicMock(side_effect=ValidationError('bad', 'field', 'code')))
    payload = {'model': 'llama-3-8b-instruct', 'encrypted': True, 'client_public_key': 'x', 'messages': {}}
    resp = client.post('/api/v1/chat/completions', json=payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['error']['message'] == 'bad'
    assert data['error']['code'] == 'code'


def test_create_completion_missing_body(client):
    resp = client.post('/api/v1/completions', data='', content_type='application/json')
    assert resp.status_code == 400


def test_create_completion_missing_model(client):
    resp = client.post('/api/v1/completions', json={})
    assert resp.status_code == 400
    assert 'Invalid request body' in resp.get_json()['error']['message']


def test_create_completion_model_error(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_model_instance', MagicMock(side_effect=ModelError('oops', status_code=400)))
    payload = {'model': 'bad', 'prompt': 'hi'}
    resp = client.post('/api/v1/completions', json=payload)
    assert resp.status_code == 400


def test_health_check_exception(client, monkeypatch):
    orig_jsonify = routes.jsonify
    def fake_jsonify(*args, **kwargs):
        if not hasattr(fake_jsonify, 'called'):
            fake_jsonify.called = True
            raise RuntimeError('boom')
        return orig_jsonify(*args, **kwargs)
    monkeypatch.setattr(routes, 'jsonify', fake_jsonify)
    resp = client.get('/api/v1/health')
    assert resp.status_code == 400
    assert 'Health check failed' in resp.get_json()['error']['message']


def test_openai_alias_get_model(client):
    models = client.get('/api/v1/models').get_json()['data']
    model_id = models[0]['id']
    api_resp = client.get(f'/api/v1/models/{model_id}').get_json()
    alias_resp = client.get(f'/v1/models/{model_id}').get_json()
    assert api_resp == alias_resp


def test_relay_unregister_requires_operator_access(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "ensure_operator_access",
        lambda *_args, **_kwargs: routes.format_error_response(
            "forbidden",
            error_type="invalid_request_error",
            status_code=403,
        ),
    )

    response = client.post("/api/v1/relay/unregister", json={"server_public_key": "abc"})

    assert response.status_code == 403
    assert response.get_json()["error"]["message"] == "forbidden"


def test_relay_unregister_removes_node_from_diagnostics(client, monkeypatch):
    monkeypatch.setattr(routes, "ensure_operator_access", lambda *_args, **_kwargs: None)
    relay.known_servers.clear()
    relay.known_servers["abc"] = {"public_key": "abc", "last_ping": routes.time.time(), "last_ping_duration": 10}

    before = client.get("/relay/diagnostics")
    assert before.get_json()["registered_compute_nodes"][0]["server_public_key"] == "abc"

    response = client.post("/api/v1/relay/unregister", json={"server_public_key": "abc"})

    assert response.status_code == 200
    assert response.get_json() == {"message": "Server unregistered", "removed": True}
    after = client.get("/relay/diagnostics")
    assert after.get_json()["registered_compute_nodes"] == []
    relay.known_servers.clear()


def test_relay_unregister_openai_alias_delegates(client, monkeypatch):
    monkeypatch.setattr(routes, "relay_unregister", lambda: routes.format_error_response("ok", status_code=200))

    response = client.post("/v1/relay/unregister")

    assert response.status_code == 200
    assert response.get_json()["error"]["message"] == "ok"


def test_chat_completion_alias_reroutes_to_canonical_model(client, monkeypatch):
    canonical_id = 'llama-3-8b-instruct'
    payload = {
        'model': 'gpt-5-chat-latest',
        'messages': [
            {'role': 'user', 'content': 'Hello'}
        ],
    }

    monkeypatch.setattr(routes, 'get_models_info', lambda: [{'id': canonical_id}])
    captured = {}

    def fake_generate_response(model_id, messages):
        captured['model_id'] = model_id
        return messages + [{'role': 'assistant', 'content': 'Mock reply'}]

    monkeypatch.setattr(compute_provider, 'generate_response', fake_generate_response)

    alias = MagicMock(return_value=canonical_id)
    monkeypatch.setattr(routes, 'resolve_model_alias', alias)

    mock_log_info = MagicMock()
    monkeypatch.setattr(routes, 'log_info', mock_log_info)

    response = client.post('/api/v1/chat/completions', json=payload)
    assert response.status_code == 200

    data = response.get_json()
    assert data['model'] == 'gpt-5-chat-latest'
    assert captured['model_id'] == canonical_id
    alias.assert_called_once_with('gpt-5-chat-latest')
    assert any(
        call.args
        and "Routing alias model 'gpt-5-chat-latest'" in str(call.args[0])
        for call in mock_log_info.call_args_list
    )


def test_chat_completion_echoes_request_metadata(client, monkeypatch):
    payload = {
        'model': 'llama-3-8b-instruct',
        'messages': [{'role': 'user', 'content': 'Hello'}],
        'metadata': {'client': 'dspace', 'conversation_id': 'conv-99'},
    }

    monkeypatch.setattr(routes, 'get_models_info', lambda: [{'id': 'llama-3-8b-instruct'}])
    monkeypatch.setattr(routes, 'validate_model_name', lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, 'get_model_instance', lambda model_id: object())
    monkeypatch.setattr(routes, 'resolve_model_alias', lambda model_id: None)
    monkeypatch.setattr(
        routes,
        'evaluate_messages_for_policy',
        lambda messages: SimpleNamespace(allowed=True),
    )

    def _generate(model_id, messages):
        assert model_id == 'llama-3-8b-instruct'
        return messages + [{'role': 'assistant', 'content': 'Mock reply'}]

    monkeypatch.setattr(routes, 'generate_response', _generate)

    response = client.post('/api/v1/chat/completions', json=payload)
    assert response.status_code == 200

    body = response.get_json()
    assert body['metadata'] == payload['metadata']


def test_chat_completion_sets_provider_path_and_stream_mode_headers(client, monkeypatch):
    class _DistributedProvider:
        def complete_chat(self, model_id, messages, options):
            assert model_id == "llama-3-8b-instruct"
            assert isinstance(options, dict)
            return {"role": "assistant", "content": "Paris"}

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "Capital of France?"}],
    }

    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(routes, "validate_model_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "evaluate_messages_for_policy", lambda _messages: SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_api_v1_compute_provider", lambda: _DistributedProvider())
    monkeypatch.setattr(routes, "get_api_v1_resolved_provider_path", lambda _provider: "distributed")

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["X-Tokenplace-API-V1-Provider"] == "_DistributedProvider"
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert (
        response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"]
        == "unknown"
    )
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"


def test_chat_completion_rejects_streaming_for_api_v1(client, monkeypatch):
    provider_called = {"value": False}

    class _GuardrailProvider:
        def complete_chat(self, model_id, messages, options):
            provider_called["value"] = True
            return {"role": "assistant", "content": "Paris"}

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "Capital of France?"}],
        "stream": True,
    }

    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(routes, "validate_model_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "evaluate_messages_for_policy", lambda _messages: SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_api_v1_compute_provider", lambda: _GuardrailProvider())

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["param"] == "stream"
    assert "Streaming is not supported for API v1 chat completions" in body["error"]["message"]
    assert provider_called["value"] is False


def test_chat_completion_encrypted_response_sets_provider_headers(client, monkeypatch):
    class _DistributedProvider:
        def complete_chat(self, model_id, messages, options):
            assert model_id == "llama-3-8b-instruct"
            assert isinstance(options, dict)
            return {"role": "assistant", "content": "Paris"}

    encrypted_payload = {
        "ciphertext": base64.b64encode(
            json.dumps([{"role": "user", "content": "Capital of France?"}]).encode("utf-8")
        ).decode("utf-8"),
        "iv": base64.b64encode(b"fake-iv").decode("utf-8"),
        "cipherkey": base64.b64encode(b"fake-key").decode("utf-8"),
    }

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": encrypted_payload,
        "encrypted": True,
        "client_public_key": "client-key",
    }

    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(routes, "validate_model_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "evaluate_messages_for_policy", lambda _messages: SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_api_v1_compute_provider", lambda: _DistributedProvider())
    monkeypatch.setattr(routes, "get_api_v1_resolved_provider_path", lambda _provider: "distributed")
    monkeypatch.setattr(routes, "validate_encrypted_request", lambda _data: None)
    monkeypatch.setattr(
        routes.encryption_manager,
        "decrypt_message",
        lambda encrypted_messages, encrypted_key: base64.b64decode(encrypted_payload["ciphertext"]),
    )
    monkeypatch.setattr(
        routes.encryption_manager,
        "encrypt_message",
        lambda data, public_key: {"ciphertext": "abc123", "pub": public_key},
    )

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 200
    body = response.get_json()
    assert body["encrypted"] is True
    assert response.headers["X-Tokenplace-API-V1-Provider"] == "_DistributedProvider"
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"


def test_legacy_completion_sets_provider_headers(client, monkeypatch):
    class _LocalProvider:
        def complete_chat(self, model_id, messages, options):
            assert model_id == "llama-3-8b-instruct"
            assert messages == [{"role": "user", "content": "hi"}]
            assert isinstance(options, dict)
            return {"role": "assistant", "content": "hello"}

    payload = {
        "model": "llama-3-8b-instruct",
        "prompt": "hi",
    }

    monkeypatch.setattr(routes, "evaluate_messages_for_policy", lambda _messages: SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_api_v1_compute_provider", lambda: _LocalProvider())
    monkeypatch.setattr(routes, "get_api_v1_resolved_provider_path", lambda _provider: "local")

    response = client.post("/api/v1/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["X-Tokenplace-API-V1-Provider"] == "_LocalProvider"
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "local"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"


def test_legacy_completion_encrypted_response_sets_provider_headers(client, monkeypatch):
    class _LocalProvider:
        def complete_chat(self, model_id, messages, options):
            assert model_id == "llama-3-8b-instruct"
            assert messages == [{"role": "user", "content": "hi"}]
            assert isinstance(options, dict)
            return {"role": "assistant", "content": "hello"}

    payload = {
        "model": "llama-3-8b-instruct",
        "prompt": "hi",
        "encrypted": True,
        "client_public_key": "client-key",
    }

    monkeypatch.setattr(routes, "evaluate_messages_for_policy", lambda _messages: SimpleNamespace(allowed=True))
    monkeypatch.setattr(routes, "get_api_v1_compute_provider", lambda: _LocalProvider())
    monkeypatch.setattr(routes, "get_api_v1_resolved_provider_path", lambda _provider: "local")
    monkeypatch.setattr(
        routes.encryption_manager,
        "encrypt_message",
        lambda data, public_key: {"ciphertext": "xyz789", "pub": public_key},
    )

    response = client.post("/api/v1/completions", json=payload)

    assert response.status_code == 200
    body = response.get_json()
    assert body["encrypted"] is True
    assert response.headers["X-Tokenplace-API-V1-Provider"] == "_LocalProvider"
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "local"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"
