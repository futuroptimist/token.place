from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from relay import app
from api.v1 import routes
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


def test_chat_completion_alias_reroutes_to_canonical_model(client, monkeypatch):
    canonical_id = 'llama-3-8b-instruct'
    payload = {
        'model': 'gpt-5-chat-latest',
        'messages': [
            {'role': 'user', 'content': 'Hello'}
        ],
    }

    monkeypatch.setattr(routes, 'get_models_info', lambda: [{'id': canonical_id}])
    validate_model_name = MagicMock()
    monkeypatch.setattr(routes, 'validate_model_name', validate_model_name)
    monkeypatch.setattr(routes, 'get_model_instance', MagicMock(return_value='MOCK'))

    captured = {}

    def fake_generate_response(model_id, messages):
        captured['model_id'] = model_id
        return messages + [{'role': 'assistant', 'content': 'Mock reply'}]

    monkeypatch.setattr(routes, 'generate_response', fake_generate_response)

    alias = MagicMock(return_value=canonical_id)
    monkeypatch.setattr(routes, 'resolve_model_alias', alias)

    mock_log_info = MagicMock()
    monkeypatch.setattr(routes, 'log_info', mock_log_info)

    response = client.post('/api/v1/chat/completions', json=payload)
    assert response.status_code == 200

    data = response.get_json()
    assert data['model'] == 'gpt-5-chat-latest'
    assert captured['model_id'] == canonical_id
    validate_model_name.assert_called_once_with(canonical_id, [canonical_id])
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
