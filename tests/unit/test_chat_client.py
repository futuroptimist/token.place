import base64
import json
from unittest.mock import patch, MagicMock

from client import (
    ChatClient,
    UNKNOWN_REQUEST_ID,
    SHORT_OPERATIONAL_TIMEOUT_SECONDS,
    call_chat_completions_encrypted,
)
from utils.inference_timeout import DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS


def test_chat_client_splits_operational_and_inference_timeouts():
    assert SHORT_OPERATIONAL_TIMEOUT_SECONDS == 10.0
    assert ChatClient.retrieve_response.__defaults__[0] == DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS


def test_get_server_public_key():
    client = ChatClient('http://testserver', relay_port=5000)
    with patch('client.requests.get') as mock_get:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'server_public_key': base64.b64encode(b'k').decode()}
        mock_get.return_value = resp
        key = client.get_server_public_key()
        assert key == b'k'
        mock_get.assert_called_with(
            'http://testserver:5000/api/v1/relay/servers/next',
            timeout=SHORT_OPERATIONAL_TIMEOUT_SECONDS,
        )


def test_send_message_flow():
    client = ChatClient('http://test', relay_port=5000)
    with patch.object(client, 'get_server_public_key', return_value=b'server_key') as m_get, \
         patch('client.uuid.uuid4') as m_uuid4, \
         patch('client.encrypt') as m_enc, \
         patch('client.decrypt') as m_dec, \
         patch.object(client, 'send_request_to_relay_requests') as m_faucet, \
         patch.object(client, 'retrieve_response') as m_retrieve:
        m_uuid4.return_value.hex = 'abc123'
        m_enc.return_value = ({'ciphertext': b'data', 'iv': b'iv'}, b'cipher', b'iv')
        m_dec.return_value = b'[{"role":"user","content":"hi"},{"role":"assistant","content":"ok"}]'
        m_faucet.return_value = MagicMock(status_code=200)
        m_retrieve.return_value = [
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'ok'}
        ]
        resp = client.send_message('hi')
        assert resp[1]['content'] == 'ok'
        assert client.chat_history == resp
        encrypted_plaintext = json.loads(m_enc.call_args.args[0].decode('utf-8'))
        assert encrypted_plaintext == {
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'version': 1,
            'request_id': 'chat-client-abc123',
            'client_public_key': client.public_key_b64,
            'api_v1_request': {
                'model': 'llama-3-8b-instruct',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'options': {},
            },
        }
        m_faucet.assert_called_once_with(
            base64.b64encode(b'data').decode('utf-8'),
            base64.b64encode(b'iv').decode('utf-8'),
            base64.b64encode(b'server_key').decode('utf-8'),
            base64.b64encode(b'cipher').decode('utf-8'),
            request_id='chat-client-abc123',
        )
        m_retrieve.assert_called_once_with(
            request_id='chat-client-abc123',
            chat_history=[{'role': 'user', 'content': 'hi'}],
        )


def test_send_message_uses_exactly_one_response_budget():
    client = ChatClient('http://test', relay_port=5000)
    with patch.object(client, 'get_server_public_key', return_value=b'server_key'), \
         patch('client.encrypt', return_value=({'ciphertext': b'data'}, b'cipher', b'iv')), \
         patch.object(
             client,
             'send_request_to_relay_requests',
             return_value=MagicMock(status_code=200),
         ), \
         patch.object(client, 'retrieve_response', return_value=None) as m_retrieve:
        assert client.send_message('hi') is None

    m_retrieve.assert_called_once()


def test_encrypted_completion_uses_short_connect_and_long_read_timeout():
    response = MagicMock()
    response.json.return_value = {'encrypted': False}
    with patch('client.encrypt', return_value=({'ciphertext': b'data'}, b'key', b'iv')), \
         patch('client.requests.post', return_value=response) as m_post:
        call_chat_completions_encrypted(
            base64.b64encode(b'server-key').decode(),
            MagicMock(),
            b'client-key',
        )

    assert m_post.call_args.kwargs['timeout'] == (
        SHORT_OPERATIONAL_TIMEOUT_SECONDS,
        DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS,
    )


def test_send_message_returns_none_when_no_server_public_key():
    client = ChatClient('http://test', relay_port=5000)
    with patch.object(client, 'get_server_public_key', return_value=None), \
         patch('client.encrypt') as m_enc:
        assert client.send_message('hi') is None
        m_enc.assert_not_called()


def test_retrieve_response_decodes_api_v1_response_for_request_id():
    client = ChatClient('http://test', relay_port=5000)
    encrypted_response = {
        'chat_history': base64.b64encode(b'ciphertext').decode('utf-8'),
        'cipherkey': base64.b64encode(b'cipherkey').decode('utf-8'),
        'iv': base64.b64encode(b'iv').decode('utf-8'),
    }
    decrypted_envelope = {
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'request_id': 'req-1',
        'api_v1_response': {
            'message': {'role': 'assistant', 'content': 'ok'},
        },
    }
    with patch('client.requests.post') as m_post, patch('client.decrypt') as m_dec:
        m_post.return_value = MagicMock(status_code=200)
        m_post.return_value.json.return_value = encrypted_response
        m_dec.return_value = json.dumps(decrypted_envelope).encode('utf-8')
        response = client.retrieve_response(
            timeout=0.1,
            request_id='req-1',
            chat_history=[{'role': 'user', 'content': 'hi'}],
        )

    assert response == [
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'ok'},
    ]
    assert 0 < m_post.call_args.kwargs['timeout'] <= 0.1


def test_retrieve_response_bounds_each_request_by_remaining_budget():
    client = ChatClient('http://test', relay_port=5000)
    with patch('client.time.time', return_value=100.0), \
         patch('client.requests.post', return_value=MagicMock(status_code=404)) as m_post:
        assert client.retrieve_response(timeout=3.0, request_id='req-1') is UNKNOWN_REQUEST_ID

    assert m_post.call_args.kwargs['timeout'] == 3.0
