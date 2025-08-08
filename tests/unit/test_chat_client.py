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
            'http://testserver:5000/next_server', timeout=REQUEST_TIMEOUT
        )


def test_send_message_flow():
    client = ChatClient('http://test', relay_port=5000)
    with patch.object(client, 'get_server_public_key', return_value=b'server_key') as m_get, \
         patch('client.encrypt') as m_enc, \
         patch('client.decrypt') as m_dec, \
         patch.object(client, 'send_request_to_faucet') as m_faucet, \
         patch.object(client, 'retrieve_response') as m_retrieve:
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
