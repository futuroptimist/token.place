import json
import base64
import pytest
from unittest.mock import patch, MagicMock

from utils.crypto_helpers import CryptoClient

# Helper to create simple CryptoClient with debug enabled for coverage
@pytest.fixture
def client():
    return CryptoClient('https://test', debug=True)


def make_mock_response(status=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.text = 'err'
    return resp


def test_fetch_server_public_key_error_and_missing_key(client):
    # Error field in response
    with patch('utils.crypto_helpers.requests.get', return_value=make_mock_response(json_data={'error': {'message': 'no'}})) as get:
        assert client.fetch_server_public_key() is False
        get.assert_called_once()

    # Missing key field
    with patch('utils.crypto_helpers.requests.get', return_value=make_mock_response(json_data={'foo': 'bar'})):
        assert client.fetch_server_public_key() is False

    # Exception path
    with patch('utils.crypto_helpers.requests.get', side_effect=Exception('boom')):
        assert client.fetch_server_public_key() is False


def test_decrypt_message_paths(client):
    encrypted = {'ciphertext': base64.b64encode(b'{}').decode(),
                 'cipherkey': base64.b64encode(b'k').decode(),
                 'iv': base64.b64encode(b'i').decode()}

    # client_private_key missing
    client.client_private_key = None
    with pytest.raises(ValueError):
        client.decrypt_message(encrypted)

    client._generate_client_keys()  # restore keys

    # decrypt returns None
    with patch('utils.crypto_helpers.decrypt', return_value=None):
        assert client.decrypt_message(encrypted) is None

    # decrypt returns text that is not JSON
    with patch('utils.crypto_helpers.decrypt', return_value=b'notjson'):
        assert client.decrypt_message(encrypted) == 'notjson'

    # decrypt returns JSON
    with patch('utils.crypto_helpers.decrypt', return_value=b'{"a": 1}'):
        assert client.decrypt_message(encrypted) == {'a': 1}


def test_send_encrypted_message_error_and_exception(client):
    payload = {'a': 'b'}
    with patch('utils.crypto_helpers.requests.post', return_value=make_mock_response(status=500)):
        assert client.send_encrypted_message('/x', payload) is None
    with patch('utils.crypto_helpers.requests.post', side_effect=Exception('fail')):
        assert client.send_encrypted_message('/x', payload) is None


def test_send_chat_message_failure_paths(client):
    # public key fetch failure
    with patch.object(client, 'fetch_server_public_key', return_value=False):
        assert client.send_chat_message('m') is None

    # encryption failure
    client.server_public_key = b'k'
    with patch.object(client, 'fetch_server_public_key', return_value=True), \
         patch('utils.crypto_helpers.encrypt', side_effect=Exception('enc')):
        assert client.send_chat_message('m') is None

    # faucet failure
    with patch.object(client, 'fetch_server_public_key', return_value=True), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'send_encrypted_message', return_value=None):
        assert client.send_chat_message('m') is None

    # unexpected faucet response
    with patch.object(client, 'fetch_server_public_key', return_value=True), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'send_encrypted_message', return_value={'foo': 'bar'}):
        assert client.send_chat_message('m') is None


def test_retrieve_chat_response_paths(client):
    encrypted_resp = {'chat_history': 'c', 'cipherkey': 'k', 'iv': 'i'}
    # send_encrypted_message returns None first, then error, then success
    with patch.object(client, 'send_encrypted_message', side_effect=[None, {'error': 'No response available'}, encrypted_resp]), \
         patch('utils.crypto_helpers.time.sleep'), \
         patch.object(client, 'decrypt_message', return_value=[{'role': 'assistant', 'content': 'hi'}]):
        assert client.retrieve_chat_response(max_retries=3, retry_delay=0) == [{'role': 'assistant', 'content': 'hi'}]

    # error not recoverable
    with patch.object(client, 'send_encrypted_message', return_value={'error': {'message': 'boom'}}), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None

    # invalid decrypted format
    with patch.object(client, 'send_encrypted_message', return_value=encrypted_resp), \
         patch.object(client, 'decrypt_message', return_value='not list'):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None

    # invalid message entry structure
    with patch.object(client, 'send_encrypted_message', return_value=encrypted_resp), \
         patch.object(client, 'decrypt_message', return_value=['bad']):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None

    # exhausting retries
    with patch.object(client, 'send_encrypted_message', return_value=None), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.retrieve_chat_response(max_retries=2, retry_delay=0) is None


def test_send_api_request_formats(client):
    client.server_public_key = b'k'
    encrypted = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    with patch.object(client, 'send_encrypted_message', return_value={'data': {'encrypted': True, **encrypted}}), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'decrypt_message', return_value={'choices': [1]}):
        assert client.send_api_request([], model='m') == {'choices': [1]}

    with patch.object(client, 'send_encrypted_message', return_value={'encrypted': True, 'encrypted_content': encrypted}), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'decrypt_message', return_value={'choices': [2]}):
        assert client.send_api_request([], model='m') == {'choices': [2]}

    with patch.object(client, 'send_encrypted_message', return_value={'foo': 'bar'}), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')):
        assert client.send_api_request([], model='m') is None

    # encrypt failure
    with patch('utils.crypto_helpers.encrypt', side_effect=Exception('enc')):
        assert client.send_api_request([], model='m') is None

    # fetch server key failure when key missing
    client.server_public_key = None
    with patch.object(client, 'fetch_server_public_key', return_value=False):
        assert client.send_api_request([], model='m') is None


def test_send_chat_message_list_success(client):
    client.server_public_key = b'k'
    messages = [{'role': 'user', 'content': 'hi'}]
    with patch.object(client, 'fetch_server_public_key', return_value=True), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'send_encrypted_message', side_effect=[{'success': True}, {'chat_history': 'c', 'cipherkey': 'k', 'iv': 'i'}]), \
         patch.object(client, 'decrypt_message', return_value=messages), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.send_chat_message(messages) == messages


def test_retrieve_chat_response_error_type_other(client):
    with patch.object(client, 'send_encrypted_message', return_value={'error': ['weird']}), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_retrieve_chat_response_decrypt_exception(client):
    resp = {'chat_history': 'c', 'cipherkey': 'k', 'iv': 'i'}
    with patch.object(client, 'send_encrypted_message', return_value=resp), \
         patch.object(client, 'decrypt_message', side_effect=Exception('bad')):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_retrieve_chat_response_unexpected_fields(client):
    with patch.object(client, 'send_encrypted_message', return_value={'foo': 'bar'}), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_send_api_request_failure_cases(client):
    # no response when server key already known
    client.server_public_key = b'k'
    with patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'send_encrypted_message', return_value=None):
        assert client.send_api_request([]) is None

    # decrypt exception (new format)
    client.server_public_key = b'k'
    encrypted = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    with patch.object(client, 'send_encrypted_message', return_value={'data': {'encrypted': True, **encrypted}}), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'decrypt_message', side_effect=Exception('boom')):
        assert client.send_api_request([], model='m') is None

    # decrypt exception (old format)
    with patch.object(client, 'send_encrypted_message', return_value={'encrypted': True, 'encrypted_content': encrypted}), \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'decrypt_message', side_effect=Exception('boom')):
        assert client.send_api_request([], model='m') is None

