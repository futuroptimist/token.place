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
    monkeypatch.setattr('utils.crypto_helpers.requests.post', lambda *a, **_kw: resp)
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
    monkeypatch.setattr('utils.crypto_helpers.requests.get', lambda *a, **_kw: resp)
    assert not client.fetch_server_public_key()


def test_fetch_server_public_key_error_field(monkeypatch):
    client = CryptoClient('https://example.com')
    resp = MagicMock(status_code=200, json=lambda: {'error': {'message': 'no'}})
    monkeypatch.setattr('utils.crypto_helpers.requests.get', lambda *a, **_kw: resp)
    assert not client.fetch_server_public_key()


def test_fetch_server_public_key_exception(monkeypatch):
    """Handle exception when fetching the server key"""
    client = CryptoClient('https://example.com')
    def boom(*a, **k):
        raise RuntimeError('fail')
    monkeypatch.setattr('utils.crypto_helpers.requests.get', boom)
    assert not client.fetch_server_public_key()


def test_decrypt_message_requires_private_key(monkeypatch):
    client = _prep_client()
    client.client_private_key = None
    with pytest.raises(ValueError):
        client.decrypt_message({'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'})


def test_decrypt_message_failure(monkeypatch):
    """Decrypt returns None when underlying decrypt fails"""
    client = _prep_client()
    enc = client.encrypt_message({'msg': 'hi'})
    monkeypatch.setattr('utils.crypto_helpers.decrypt', lambda *a, **k: None)
    res = client.decrypt_message(enc)
    assert res is None


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

def test_send_chat_message_fetch_key_fail(monkeypatch):
    client = _prep_client()
    monkeypatch.setattr(client, 'fetch_server_public_key', lambda: False)
    assert client.send_chat_message('hi') is None


def test_retrieve_chat_response_server_error(monkeypatch):
    client = _prep_client()
    # First call to send_encrypted_message returns {'error': 'fail'} so it exits
    monkeypatch.setattr(client, 'send_encrypted_message', lambda *a, **k: {'error': 'fail'})
    result = client.retrieve_chat_response(max_retries=1, retry_delay=0)
    assert result is None


def test_send_api_request_invalid_format(monkeypatch):
    client = _prep_client()
    monkeypatch.setattr(client, 'send_encrypted_message', lambda *a, **k: {'unexpected': True})
    res = client.send_api_request([{'role': 'user', 'content': 'hi'}])
    assert res is None

def test_fetch_server_public_key_missing_key(monkeypatch):
    client = CryptoClient('https://example.com')
    resp = MagicMock(status_code=200, json=lambda: {'unexpected': 'x'})
    monkeypatch.setattr('utils.crypto_helpers.requests.get', lambda *a, **k: resp)
    assert not client.fetch_server_public_key()


def test_send_chat_message_encrypt_exception(monkeypatch):
    client = _prep_client()
    monkeypatch.setattr(client, 'encrypt_message', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    assert client.send_chat_message('hi') is None


def test_send_api_request_decrypt_failure(monkeypatch):
    client = _prep_client()
    enc_payload = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    monkeypatch.setattr(client, 'encrypt_message', lambda msgs: enc_payload)
    monkeypatch.setattr(client, 'send_encrypted_message', lambda *a, **k: {'data': {'encrypted': True, **enc_payload}})
    monkeypatch.setattr(client, 'decrypt_message', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fail')))
    res = client.send_api_request([{'role': 'user', 'content': 'hi'}])
    assert res is None
