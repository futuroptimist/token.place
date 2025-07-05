import base64
from unittest.mock import MagicMock
import pytest
from utils.crypto_helpers import CryptoClient


def _prep_client():
    client = CryptoClient('https://example.com')
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    return client


def test_retrieve_chat_no_response(monkeypatch):
    client = _prep_client()
    monkeypatch.setattr(client, 'send_encrypted_message', lambda *a, **k: None)
    monkeypatch.setattr('utils.crypto_helpers.time.sleep', lambda x: None)
    result = client.retrieve_chat_response(max_retries=1, retry_delay=0)
    assert result is None


def test_retrieve_chat_invalid_decrypted(monkeypatch):
    client = _prep_client()
    enc = {
        'chat_history': base64.b64encode(b'd').decode(),
        'cipherkey': base64.b64encode(b'k').decode(),
        'iv': base64.b64encode(b'i').decode()
    }
    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value=enc))
    monkeypatch.setattr(client, 'decrypt_message', MagicMock(return_value='oops'))
    result = client.retrieve_chat_response(max_retries=1, retry_delay=0)
    assert result is None
