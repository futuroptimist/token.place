import base64
import json
from unittest.mock import MagicMock

import pytest

from relay import app
from api.v1 import routes
from api.v1.models import ModelError
from api.v1.validation import ValidationError
from encrypt import encrypt as encrypt_payload, decrypt as decrypt_payload, generate_keys


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_get_public_key_exception(client, monkeypatch):
    class FailingEncryptionManager:
        @property
        def public_key_b64(self):
            raise RuntimeError('fail')

    monkeypatch.setattr(routes, 'encryption_manager', FailingEncryptionManager())
    resp = client.get('/api/v1/public-key')
    assert resp.status_code == 400
    assert 'Failed to retrieve public key' in resp.get_json()['error']['message']


def test_chat_completion_encrypted_validation_error(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        'validate_encrypted_request',
        MagicMock(side_effect=ValidationError('bad', 'field', 'code')),
    )
    payload = {
        'model': 'llama-3-8b-instruct',
        'encrypted': True,
        'client_public_key': 'x',
        'messages': {},
    }
    resp = client.post('/api/v1/chat/completions', json=payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['error']['message'] == 'bad'
    assert data['error']['code'] == 'code'


def _mock_stream_chunks():
    return iter([
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _extract_sse_payloads(response):
    raw = response.get_data(as_text=True)
    events = [segment for segment in raw.split("\n\n") if segment]
    payloads = []
    for event in events:
        for line in event.splitlines():
            if line.startswith('data: '):
                payloads.append(line[len('data: '):])
    return payloads


def test_chat_completion_stream_plaintext(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_model_instance', MagicMock(return_value='MOCK'))
    monkeypatch.setattr(
        routes,
        'stream_chat_completion',
        lambda *args, **kwargs: _mock_stream_chunks(),
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'messages': [
            {'role': 'user', 'content': 'hi there'},
        ],
        'stream': True,
    }

    resp = client.post('/api/v1/chat/completions', json=payload)

    assert resp.status_code == 200
    assert resp.headers['Content-Type'].startswith('text/event-stream')

    payloads = _extract_sse_payloads(resp)
    assert payloads[-1] == '[DONE]'

    accumulated = ''
    for chunk in payloads[:-1]:
        data = json.loads(chunk)
        delta = data['choices'][0]['delta']
        accumulated += delta.get('content', '')

    assert accumulated == 'Hello world'


def test_chat_completion_stream_encrypted(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_model_instance', MagicMock(return_value='MOCK'))
    monkeypatch.setattr(
        routes,
        'stream_chat_completion',
        lambda *args, **kwargs: _mock_stream_chunks(),
    )

    private_key, public_key = generate_keys()
    client_public_key_b64 = base64.b64encode(public_key).decode('utf-8')

    server_public_key = base64.b64decode(routes.encryption_manager.public_key_b64)
    messages = [{"role": "user", "content": "hi there"}]
    ciphertext_dict, cipherkey, iv = encrypt_payload(
        json.dumps(messages).encode('utf-8'),
        server_public_key,
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'encrypted': True,
        'client_public_key': client_public_key_b64,
        'messages': {
            'ciphertext': base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
            'cipherkey': base64.b64encode(cipherkey).decode('utf-8'),
            'iv': base64.b64encode(iv).decode('utf-8'),
        },
        'stream': True,
    }

    resp = client.post('/api/v1/chat/completions', json=payload)

    assert resp.status_code == 200
    assert resp.headers['Content-Type'].startswith('text/event-stream')

    payloads = _extract_sse_payloads(resp)
    assert payloads[-1] == '[DONE]'

    accumulated = ''
    for chunk in payloads[:-1]:
        data = json.loads(chunk)
        assert data['encrypted'] is True
        encrypted_chunk = data['chunk']
        decrypted = decrypt_payload(
            {
                'ciphertext': base64.b64decode(encrypted_chunk['ciphertext']),
                'iv': base64.b64decode(encrypted_chunk['iv']),
            },
            base64.b64decode(encrypted_chunk['cipherkey']),
            private_key,
        )
        chunk_payload = json.loads(decrypted.decode('utf-8'))
        accumulated += chunk_payload['choices'][0]['delta'].get('content', '')

    assert accumulated == 'Hello world'


def test_completion_stream_plaintext(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_model_instance', MagicMock(return_value='MOCK'))
    monkeypatch.setattr(
        routes,
        'stream_chat_completion',
        lambda *args, **kwargs: _mock_stream_chunks(),
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'prompt': 'hello',
    }

    resp = client.post('/api/v1/completions/stream', json=payload)

    assert resp.status_code == 200
    assert resp.headers['Content-Type'].startswith('text/event-stream')

    payloads = _extract_sse_payloads(resp)
    assert payloads[-1] == '[DONE]'

    accumulated = ''
    for chunk in payloads[:-1]:
        data = json.loads(chunk)
        accumulated += data['choices'][0].get('text', '')

    assert accumulated == 'Hello world'


def test_create_completion_missing_body(client):
    resp = client.post('/api/v1/completions', data='', content_type='application/json')
    assert resp.status_code == 400


def test_create_completion_missing_model(client):
    resp = client.post('/api/v1/completions', json={})
    assert resp.status_code == 400
    assert 'Invalid request body' in resp.get_json()['error']['message']


def test_list_models_internal_error(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_models_info', MagicMock(side_effect=RuntimeError('boom')))

    resp = client.get('/api/v1/models')

    assert resp.status_code == 400
    assert resp.get_json()['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE


def test_get_model_internal_error(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_models_info', MagicMock(side_effect=RuntimeError('boom')))

    resp = client.get('/api/v1/models/llama-3-8b-instruct')

    assert resp.status_code == 400
    assert resp.get_json()['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE


def test_chat_completion_internal_error(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        '_process_chat_completion_request',
        MagicMock(side_effect=RuntimeError('unexpected')),
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'messages': [{'role': 'user', 'content': 'hello'}],
    }

    resp = client.post('/api/v1/chat/completions', json=payload)

    assert resp.status_code == 500
    assert resp.get_json()['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE


def test_chat_completion_stream_internal_error(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        '_process_chat_completion_request',
        MagicMock(side_effect=RuntimeError('unexpected')),
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'messages': [{'role': 'user', 'content': 'hello'}],
    }

    resp = client.post('/api/v1/chat/completions/stream', json=payload)

    assert resp.status_code == 500
    assert resp.get_json()['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE


def test_completion_internal_error(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        '_process_completion_request',
        MagicMock(side_effect=RuntimeError('unexpected')),
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'prompt': 'Hello',
    }

    resp = client.post('/api/v1/completions', json=payload)

    assert resp.status_code == 400
    assert resp.get_json()['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE


def test_completion_stream_internal_error(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        '_process_completion_request',
        MagicMock(side_effect=RuntimeError('unexpected')),
    )

    payload = {
        'model': 'llama-3-8b-instruct',
        'prompt': 'Hello',
    }

    resp = client.post('/api/v1/completions/stream', json=payload)

    assert resp.status_code == 400
    assert resp.get_json()['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE


def test_create_completion_model_error(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        'get_model_instance',
        MagicMock(side_effect=ModelError('oops', status_code=400)),
    )
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
    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE
    assert payload['error']['type'] == 'server_error'


def test_get_public_key_exception(client, monkeypatch):
    class ExplosivePublicKey:
        @property
        def public_key_b64(self):
            raise RuntimeError('boom')

    monkeypatch.setattr(routes, 'encryption_manager', ExplosivePublicKey())

    resp = client.get('/api/v1/public-key')

    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload['error']['message'] == routes.INTERNAL_SERVER_ERROR_MESSAGE
    assert payload['error']['type'] == 'server_error'


def test_openai_alias_get_model(client):
    models = client.get('/api/v1/models').get_json()['data']
    model_id = models[0]['id']
    api_resp = client.get(f'/api/v1/models/{model_id}').get_json()
    alias_resp = client.get(f'/v1/models/{model_id}').get_json()
    assert api_resp == alias_resp
