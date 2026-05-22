import base64
import json
from unittest.mock import patch, MagicMock

from client import ChatClient, REQUEST_TIMEOUT


def test_get_server_public_key():
    client = ChatClient('http://testserver', relay_port=5000)
    with patch('client.requests.get') as mock_get:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'server_public_key': base64.b64encode(b'k').decode()}
        mock_get.return_value = resp
        key = client.get_server_public_key()
        assert key == b'k'
        mock_get.assert_called_with(
            'http://testserver:5000/api/v1/relay/servers/next', timeout=REQUEST_TIMEOUT
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
    m_post.assert_called_once_with(
        'http://test:5000/api/v1/relay/responses/retrieve',
        json={'client_public_key': client.public_key_b64, 'request_id': 'req-1'},
        timeout=REQUEST_TIMEOUT,
    )


def test_retrieve_response_retries_when_status_is_pending():
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
        'api_v1_response': {'message': {'role': 'assistant', 'content': 'ok'}},
    }
    with patch('client.requests.post') as m_post, patch('client.decrypt') as m_dec, patch('client.time.sleep'):
        pending_resp = MagicMock(status_code=202)
        pending_resp.json.return_value = {'status': 'pending'}
        done_resp = MagicMock(status_code=200)
        done_resp.json.return_value = encrypted_response
        m_post.side_effect = [pending_resp, done_resp]
        m_dec.return_value = json.dumps(decrypted_envelope).encode('utf-8')
        response = client.retrieve_response(timeout=0.2, request_id='req-1')

    assert response[-1] == {'role': 'assistant', 'content': 'ok'}
    assert m_post.call_count == 2
