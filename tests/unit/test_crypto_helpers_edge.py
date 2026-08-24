import base64
import logging
from unittest.mock import MagicMock, patch
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


def test_send_chat_message_list_branch():
    client = _prep_client()
    msgs = [{'role': 'user', 'content': 'hi'}]
    server_key = base64.b64encode(b'k').decode()
    selection = {
        'server_public_key': server_key,
        'reservation_token': 'reservation-proof',
        'requested_model': 'qwen3-8b-instruct',
        'requested_context_tier': '8k-fast',
        'request_deadline_epoch': 2_000_000_000,
    }
    with patch('utils.crypto_helpers.requests.get', return_value=_FakeResponse(payload=selection)) as mock_get, \
         patch('utils.crypto_helpers.encrypt', return_value=({'ciphertext': b'c', 'iv': b'i'}, b'k', b'i')), \
         patch.object(client, 'send_encrypted_message', return_value={
             'success': True,
             'retrieval_credential': 'retrieval-proof',
         }) as mock_enqueue, \
         patch('utils.crypto_helpers.requests.post', return_value=_FakeResponse(status_code=200, payload={'chat_history': 'c', 'cipherkey': 'k', 'iv': 'i'})) as mock_retrieve, \
         patch.object(client, 'decrypt_message', return_value=msgs), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.send_chat_message(msgs) == msgs

    selection_params = mock_get.call_args.kwargs['params']
    assert selection_params['client_public_key']
    assert selection_params['request_id']
    assert selection_params['cancel_token']
    assert selection_params['model'] == 'qwen3-8b-instruct'
    assert selection_params['context_tier'] == '8k-fast'
    enqueue_payload = mock_enqueue.call_args.args[1]
    assert enqueue_payload['request_id'] == selection_params['request_id']
    assert enqueue_payload['cancel_token'] == selection_params['cancel_token']
    assert enqueue_payload['reservation_token'] == 'reservation-proof'
    assert enqueue_payload['requested_model'] == selection_params['model']
    assert enqueue_payload['requested_context_tier'] == selection_params['context_tier']
    assert enqueue_payload['request_deadline_epoch'] == 2_000_000_000
    assert enqueue_payload['server_public_key'] == server_key
    assert mock_retrieve.call_args.kwargs['json']['retrieval_credential'] == 'retrieval-proof'


@pytest.mark.parametrize(
    ('failure', 'expected_log'),
    [
        ('selection_status', 'Failed to select relay server: 503'),
        ('selection_exception', 'Exception while selecting relay server: RuntimeError'),
        ('encryption_exception', 'Failed to encrypt API v1 relay envelope: RuntimeError'),
        ('enqueue_empty', 'Failed to send message to API v1 relay requests'),
        ('enqueue_unexpected', 'Unexpected response from API v1 relay requests'),
    ],
)
def test_send_chat_message_failures_are_covered_by_unit_suite(
    caplog,
    failure,
    expected_log,
):
    """Keep fail-closed relay branches covered by CI's tests/unit run."""
    client = _prep_client()
    selection = _FakeResponse(
        status_code=503 if failure == 'selection_status' else 200,
        payload={
            'server_public_key': base64.b64encode(b'k').decode(),
            'reservation_token': 'reservation-proof',
            'requested_model': 'qwen3-8b-instruct',
            'requested_context_tier': '8k-fast',
            'request_deadline_epoch': 2_000_000_000,
        },
    )
    selection_result = (
        MagicMock(side_effect=RuntimeError('selection failed'))
        if failure == 'selection_exception'
        else MagicMock(return_value=selection)
    )
    encryption_result = (
        MagicMock(side_effect=RuntimeError('encryption failed'))
        if failure == 'encryption_exception'
        else MagicMock(return_value={'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'})
    )
    enqueue_result = {
        'message': 'Request received',
        'retrieval_credential': 'retrieval-proof',
    }
    if failure == 'enqueue_empty':
        enqueue_result = None
    elif failure == 'enqueue_unexpected':
        enqueue_result = {'retrieval_credential': 'retrieval-proof'}

    with patch('utils.crypto_helpers.requests.get', selection_result), \
         patch.object(client, '_encrypt_message_for_key', encryption_result), \
         patch.object(client, 'send_encrypted_message', return_value=enqueue_result), \
         caplog.at_level(logging.ERROR, logger='crypto_client'):
        assert client.send_chat_message('hello') is None

    assert expected_log in caplog.text
    assert 'reservation-proof' not in caplog.text
    assert 'retrieval-proof' not in caplog.text


def test_retrieve_chat_response_error_list():
    client = _prep_client()
    with patch('utils.crypto_helpers.requests.post', return_value=_FakeResponse(status_code=200, payload={'error': ['boom']})), \
         patch('utils.crypto_helpers.time.sleep'):
        assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_send_api_request_old_format_decrypt_exception():
    client = _prep_client()
    enc = {'ciphertext': 'c', 'cipherkey': 'k', 'iv': 'i'}
    with patch.object(client, 'send_encrypted_message', return_value={'encrypted': True, 'encrypted_content': enc}), \
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
