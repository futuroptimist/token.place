import base64
from unittest.mock import MagicMock, patch

import pytest

from utils.crypto_helpers import CryptoClient


def _prep_client():
    client = CryptoClient('https://example.com')
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    return client


def test_encrypt_message_requires_key():
    client = CryptoClient('https://example.com')
    client.server_public_key = None
    with pytest.raises(ValueError):
        client.encrypt_message({'msg': 'hi'})


def test_send_encrypted_message_http_error(monkeypatch):
    client = _prep_client()
    resp = MagicMock(status_code=500, text='fail')
    monkeypatch.setattr('utils.crypto_helpers.requests.post', lambda *a, **kw: resp)
    result = client.send_encrypted_message('/bad', {})
    assert result is None


def test_retrieve_chat_response_retry(monkeypatch):
    client = _prep_client()
    enc = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    seq = [
        {'error': 'No response available'},
        {**enc, 'chat_history': 'c'}
    ]
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(side_effect=seq))
    monkeypatch.setattr(client, 'decrypt_message', MagicMock(return_value=[{'role': 'assistant', 'content': 'ok'}]))
    result = client.retrieve_chat_response(max_retries=2, retry_delay=0)
    assert result[0]['content'] == 'ok'


def test_fetch_server_public_key_non_200(monkeypatch):
    client = CryptoClient('https://example.com')
    resp = MagicMock(status_code=500, json=lambda: {})
    monkeypatch.setattr('utils.crypto_helpers.requests.get', lambda *a, **kw: resp)
    assert not client.fetch_server_public_key()


def test_fetch_server_public_key_error_field(monkeypatch):
    client = CryptoClient('https://example.com')
    resp = MagicMock(status_code=200, json=lambda: {'error': {'message': 'no'}})
    monkeypatch.setattr('utils.crypto_helpers.requests.get', lambda *a, **kw: resp)
    assert not client.fetch_server_public_key()


def test_decrypt_message_requires_private_key(monkeypatch):
    client = _prep_client()
    client.client_private_key = None
    with pytest.raises(ValueError):
        client.decrypt_message({'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'})


def test_send_encrypted_message_exception(monkeypatch):
    client = _prep_client()
    def boom(*a, **k):
        raise RuntimeError('fail')
    monkeypatch.setattr('utils.crypto_helpers.requests.post', boom)
    assert client.send_encrypted_message('/x', {}) is None


def test_send_chat_message_unexpected_faucet(monkeypatch):
    client = _prep_client()
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value={'success': False}))
    assert client.send_chat_message('hi') is None
