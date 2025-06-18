import base64
import json
from unittest.mock import MagicMock, patch

from utils.crypto_helpers import CryptoClient


def _prep_client():
    client = CryptoClient('https://example.com')
    # assign server key manually to skip network call
    client.server_public_key = client.client_public_key
    client.server_public_key_b64 = client.client_public_key_b64
    return client


def test_send_api_request_new_format(monkeypatch):
    client = _prep_client()
    enc_payload = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    mock_response = {'data': {'encrypted': True, **enc_payload}}

    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value=mock_response))
    with patch.object(client, 'encrypt_message', return_value=enc_payload) as m_enc, \
         patch.object(client, 'decrypt_message', return_value={'choices': [{'message': {'content': 'ok'}}]}):
        result = client.send_api_request([{'role': 'user', 'content': 'hi'}])
        assert result['choices'][0]['message']['content'] == 'ok'
        assert client.send_encrypted_message.called
        m_enc.assert_called()


def test_send_api_request_old_format(monkeypatch):
    client = _prep_client()
    enc_payload = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    mock_response = {'encrypted': True, 'encrypted_content': enc_payload}

    monkeypatch.setattr(client, 'send_encrypted_message', MagicMock(return_value=mock_response))
    with patch.object(client, 'encrypt_message', return_value=enc_payload), \
         patch.object(client, 'decrypt_message', return_value={'id': '1'}):
        result = client.send_api_request([{'role': 'user', 'content': 'hi'}])
        assert result['id'] == '1'


def test_decrypt_message_non_json():
    client = _prep_client()
    data = client.encrypt_message('hello world')
    result = client.decrypt_message(data)
    assert isinstance(result, str)
