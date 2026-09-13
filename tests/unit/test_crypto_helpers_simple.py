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
        mock_requests.get.assert_called_with('https://example.com/api/v1/relay/servers/next', timeout=10)
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
        server_key = base64.b64encode(b'k').decode()
        get_resp.json.return_value = {
            'server_public_key': server_key,
            'reservation_token': 'reservation-proof',
            'requested_model': 'qwen3-8b-instruct',
            'requested_context_tier': '8k-fast',
            'request_deadline_epoch': 2_000_000_000,
        }
        post_resp = MagicMock(status_code=200)
        post_resp.json.side_effect = [
            {'success': True, 'retrieval_credential': 'retrieval-proof'},
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
        selection_params = mock_requests.get.call_args.kwargs['params']
        assert selection_params['client_public_key']
        assert selection_params['request_id']
        assert selection_params['cancel_token']
        assert selection_params['model'] == 'qwen3-8b-instruct'
        assert selection_params['context_tier'] == '8k-fast'
        enqueue_payload = mock_requests.post.call_args_list[0].kwargs['json']
        assert enqueue_payload['request_id'] == selection_params['request_id']
        assert enqueue_payload['cancel_token'] == selection_params['cancel_token']
        assert enqueue_payload['reservation_token'] == 'reservation-proof'
        assert enqueue_payload['requested_model'] == selection_params['model']
        assert enqueue_payload['requested_context_tier'] == selection_params['context_tier']
        assert enqueue_payload['request_deadline_epoch'] == 2_000_000_000
        assert enqueue_payload['server_public_key'] == server_key
        retrieval_payload = mock_requests.post.call_args_list[1].kwargs['json']
        assert retrieval_payload['retrieval_credential'] == 'retrieval-proof'

def test_has_server_public_key():
    client = CryptoClient('https://example.com')
    assert not client.has_server_public_key()
    with patch('utils.crypto_helpers.requests') as mock_requests:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'server_public_key': base64.b64encode(b'k').decode()}
        mock_requests.get.return_value = resp
        assert client.fetch_server_public_key()
    assert client.has_server_public_key()
