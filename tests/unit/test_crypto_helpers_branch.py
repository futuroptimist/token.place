import base64
from unittest.mock import MagicMock
from utils.crypto_helpers import CryptoClient


def _fresh_client():
    return CryptoClient('https://example.com')


def test_send_chat_message_no_public_key(monkeypatch):
    client = _fresh_client()
    monkeypatch.setattr(client, 'fetch_server_public_key', lambda: False)
    assert client.send_chat_message('hi') is None


def test_retrieve_chat_error_object(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    seq = [
        {'error': {'message': 'No response available'}},
        {
            'chat_history': base64.b64encode(b'[{}]').decode(),
            'cipherkey': base64.b64encode(b'k').decode(),
            'iv': base64.b64encode(b'i').decode()
        }
    ]
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(side_effect=seq))
    monkeypatch.setattr(client, 'decrypt_message', lambda data: [{}])
    monkeypatch.setattr('utils.crypto_helpers.time.sleep', lambda x: None)
    assert client.retrieve_chat_response(max_retries=2, retry_delay=0) is None


def test_retrieve_chat_invalid_entry(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    enc = {
        'chat_history': base64.b64encode(b'[{"role":"assistant"}]').decode(),
        'cipherkey': base64.b64encode(b'k').decode(),
        'iv': base64.b64encode(b'i').decode()
    }
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value=enc))
    monkeypatch.setattr(client, 'decrypt_message', lambda data: [{"role": "assistant"}])
    assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_retrieve_chat_decrypt_exception(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    enc = {
        'chat_history': base64.b64encode(b'd').decode(),
        'cipherkey': base64.b64encode(b'k').decode(),
        'iv': base64.b64encode(b'i').decode()
    }
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value=enc))
    def boom(*a, **k):
        raise RuntimeError('bad decrypt')
    monkeypatch.setattr(client, 'decrypt_message', boom)
    assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_retrieve_chat_unexpected_fields(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value={'foo': 'bar'}))
    monkeypatch.setattr('utils.crypto_helpers.time.sleep', lambda x: None)
    assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_send_api_request_no_server_key(monkeypatch):
    client = _fresh_client()
    monkeypatch.setattr(client, 'fetch_server_public_key', lambda endpoint='/api/v1/public-key': False)
    assert client.send_api_request([{'role': 'user', 'content': 'hi'}]) is None


def test_send_api_request_encrypt_error(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    monkeypatch.setattr(client, 'encrypt_message', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fail')))
    assert client.send_api_request([{'role': 'user', 'content': 'hi'}]) is None


def test_send_api_request_no_response(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    monkeypatch.setattr(client, 'encrypt_message', lambda msgs: {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'})
    monkeypatch.setattr(client, 'send_encrypted_message', lambda *a, **k: None)
    assert client.send_api_request([{'role': 'user', 'content': 'hi'}]) is None


def test_send_api_request_decrypt_error(monkeypatch):
    client = _fresh_client()
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    monkeypatch.setattr(client, 'encrypt_message', lambda msgs: {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'})
    monkeypatch.setattr(client, 'send_encrypted_message', lambda *a, **k: {'data': {'encrypted': True, 'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}})
    monkeypatch.setattr(client, 'decrypt_message', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('bad')))
    assert client.send_api_request([{'role': 'user', 'content': 'hi'}]) is None
