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


def test_chat_completion_streaming_not_supported(client):
    payload = {
        'model': 'llama-3-8b-instruct',
        'messages': [
            {'role': 'user', 'content': 'hi there'},
        ],
        'stream': True,
    }

    resp = client.post('/api/v1/chat/completions', json=payload)

    assert resp.status_code == 400
    body = resp.get_json()
    assert body['error']['code'] == 'stream_not_supported'
    assert 'Streaming' in body['error']['message']


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
