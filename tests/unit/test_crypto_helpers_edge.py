import base64
import logging
from unittest.mock import patch
import pytest

from encrypt import encrypt
from utils.crypto_helpers import CryptoClient, logger


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _prep_client():
    client = CryptoClient('https://example.com', debug=True)
    client.server_public_key = b'k'
    client.server_public_key_b64 = base64.b64encode(b'k').decode()
    return client


def test_debug_logging_level():
    CryptoClient('https://debug.com', debug=True)
    assert logger.level == logging.DEBUG


@pytest.mark.parametrize("url", ["", "example.com", "ftp://example.com"])
def test_base_url_requires_scheme(url: str) -> None:
    with pytest.raises(ValueError):
        CryptoClient(url)


def test_send_chat_message_list_input_uses_api_v1_envelope():
    client = _prep_client()
    msgs = [{'role': 'user', 'content': 'hi'}]
    with patch.object(client, 'fetch_server_public_key', return_value=True), \
         patch('utils.crypto_helpers.uuid.uuid4') as mock_uuid4, \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'send_encrypted_message', return_value={'success': True}), \
         patch('utils.crypto_helpers.requests.post', return_value=_FakeResponse(status_code=200, payload={'chat_history': 'c', 'cipherkey': 'k', 'iv': 'i'})), \
         patch.object(client, 'decrypt_message', return_value={
             'protocol': 'tokenplace_api_v1_relay_e2ee',
             'request_id': 'crypto-client-abc123',
             'api_v1_response': {'message': {'role': 'assistant', 'content': 'ok'}},
         }), \
         patch('utils.crypto_helpers.time.sleep'):
        mock_uuid4.return_value.hex = 'abc123'
        assert client.send_chat_message(msgs) == msgs + [{'role': 'assistant', 'content': 'ok'}]


def test_retrieve_chat_response_error_list():
    client = _prep_client()
    with patch('utils.crypto_helpers.requests.post', return_value=_FakeResponse(status_code=200, payload={'error': ['boom']})), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_send_api_request_old_format_decrypt_exception():
    client = _prep_client()
    enc = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    with patch.object(client, 'send_encrypted_message', return_value={'encrypted': True, 'encrypted_content': enc}), \
         patch('utils.crypto_helpers.uuid.uuid4') as mock_uuid4, \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'decrypt_message', side_effect=Exception('fail')):
        assert client.send_api_request([{'role': 'user', 'content': 'hi'}]) is None


def test_decrypt_message_empty_plaintext():
    client = CryptoClient('https://empty.test')
    cipher_dict, enc_key, _ = encrypt(b'', client.client_public_key)
    encrypted = {
        'ciphertext': base64.b64encode(cipher_dict['ciphertext']).decode(),
        'cipherkey': base64.b64encode(enc_key).decode(),
        'iv': base64.b64encode(cipher_dict['iv']).decode(),
    }
    assert client.decrypt_message(encrypted) == ''
