import base64
import json
from unittest.mock import patch, MagicMock

from utils.crypto_helpers import CryptoClient
from encrypt import generate_keys, encrypt


def test_fetch_server_public_key():
    with patch('utils.crypto_helpers.requests') as mock_requests:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'server_public_key': base64.b64encode(b'k').decode()}
        mock_requests.get.return_value = resp
        client = CryptoClient('https://example.com')
        assert client.fetch_server_public_key()
        mock_requests.get.assert_called_with('https://example.com/next_server', timeout=10)
        assert client.server_public_key is not None


def test_encrypt_decrypt_message():
    client = CryptoClient('https://example.com')
    # Encrypt using the client's own public key so decrypt_message can succeed
    data = {'msg': 'hi'}
    cipher, key, iv = encrypt(json.dumps(data).encode(), client.client_public_key)
    enc = {
        'ciphertext': base64.b64encode(cipher['ciphertext']).decode(),
        'cipherkey': base64.b64encode(key).decode(),
        'iv': base64.b64encode(iv).decode()
    }
    dec = client.decrypt_message(enc)
    assert dec == data


def test_send_chat_message():
    with patch('utils.crypto_helpers.encrypt') as mock_enc, \
         patch('utils.crypto_helpers.decrypt') as mock_dec, \
         patch('utils.crypto_helpers.requests') as mock_requests:
        mock_enc.return_value = ({'ciphertext': b'd', 'iv': b'i'}, b'k', b'i')
        mock_dec.return_value = json.dumps([
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hey'}
        ]).encode()
        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {'server_public_key': base64.b64encode(b'k').decode()}
        post_resp = MagicMock(status_code=200)
        post_resp.json.side_effect = [
            {'success': True},
            {
                'chat_history': base64.b64encode(b'd').decode(),
                'cipherkey': base64.b64encode(b'k').decode(),
                'iv': base64.b64encode(b'i').decode()
            }
        ]
        mock_requests.get.return_value = get_resp
        mock_requests.post.return_value = post_resp

        client = CryptoClient('https://example.com')
        resp = client.send_chat_message('hi')
        assert isinstance(resp, list) and resp[1]['role'] == 'assistant'
        assert mock_requests.post.call_count == 2
