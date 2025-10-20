"""Tests for the CryptoClient helper class."""

import base64
import json
from typing import Iterator

import pytest
import requests
from unittest.mock import MagicMock, patch

from encrypt import StreamSession, generate_keys

from utils.crypto_helpers import CryptoClient

@pytest.fixture
def mock_crypto_client():
    """Create a CryptoClient with mocked server communication"""
    with patch('utils.crypto_helpers.requests') as mock_requests:
        # Mock the server's public key response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'server_public_key': base64.b64encode(b'mock_server_public_key').decode('utf-8')
        }
        mock_requests.get.return_value = mock_response

        # Mock POST requests
        mock_post_response = MagicMock()
        mock_post_response.status_code = 200
        mock_post_response.json.return_value = {
            'success': True,
            'chat_history': base64.b64encode(b'{"mock": "response"}').decode('utf-8'),
            'cipherkey': base64.b64encode(b'mock_cipherkey').decode('utf-8'),
            'iv': base64.b64encode(b'mock_iv').decode('utf-8')
        }
        mock_requests.post.return_value = mock_post_response

        # Create client and return with mocks
        client = CryptoClient('https://mock-server.com')
        yield client, mock_requests

def test_crypto_client_initialization():
    """Test that the CryptoClient initializes correctly"""
    client = CryptoClient('https://test-server.com')
    assert client.base_url == 'https://test-server.com'
    assert client.client_private_key is not None
    assert client.client_public_key is not None
    assert client.client_public_key_b64 is not None
    assert client.server_public_key is None

def test_fetch_server_public_key(mock_crypto_client):
    """Test fetching the server's public key"""
    client, mock_requests = mock_crypto_client

    # Test the default endpoint
    result = client.fetch_server_public_key()
    assert result is True
    mock_requests.get.assert_called_with('https://mock-server.com/next_server', timeout=10)

    # Test a custom endpoint
    result = client.fetch_server_public_key('/api/v1/public-key')
    assert result is True
    mock_requests.get.assert_called_with('https://mock-server.com/api/v1/public-key', timeout=10)

    # Test server key was set
    assert client.server_public_key is not None
    assert client.server_public_key_b64 is not None

def test_encrypt_message():
    """Test encrypting a message"""
    client = CryptoClient('https://test-server.com')

    # Set a mock server public key
    _, public_key = generate_keys()
    client.server_public_key = public_key

    # Test with dictionary
    test_dict = {'test': 'message', 'num': 123}
    encrypted = client.encrypt_message(test_dict)
    assert 'ciphertext' in encrypted
    assert 'cipherkey' in encrypted
    assert 'iv' in encrypted

    # Test with string
    test_str = "Hello, world!"
    encrypted = client.encrypt_message(test_str)
    assert 'ciphertext' in encrypted
    assert 'cipherkey' in encrypted
    assert 'iv' in encrypted

    # Test with bytes
    test_bytes = b"binary data\x00"
    encrypted = client.encrypt_message(test_bytes)
    assert 'ciphertext' in encrypted
    assert 'cipherkey' in encrypted
    assert 'iv' in encrypted

def test_send_encrypted_message(mock_crypto_client):
    """Test sending an encrypted message"""
    client, mock_requests = mock_crypto_client

    # Test sending a message
    payload = {'test': 'data'}
    response = client.send_encrypted_message('/test-endpoint', payload)

    mock_requests.post.assert_called_with(
        'https://mock-server.com/test-endpoint', json=payload, timeout=10
    )
    assert response is not None
    assert response['success'] is True

def test_error_handling():
    """Test error handling in the CryptoClient"""
    client = CryptoClient('https://test-server.com')

    # Test encrypt_message without server key
    with pytest.raises(ValueError):
        client.encrypt_message("This should fail")

    # Test encrypt_message with None message
    _, public_key = generate_keys()
    client.server_public_key = public_key
    with pytest.raises(ValueError):
        client.encrypt_message(None)

    # Mock a failed server key fetch
    with patch('utils.crypto_helpers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        result = client.fetch_server_public_key()
        assert result is False

    # Set a server key for additional validation
    client.server_public_key = generate_keys()[1]

    # Unsupported message types should raise TypeError
    with pytest.raises(TypeError):
        client.encrypt_message(123)

    # None message should raise ValueError after server key is set
    with pytest.raises(ValueError, match="message cannot be None"):
        client.encrypt_message(None)

@patch('utils.crypto_helpers.requests')
@patch('utils.crypto_helpers.time')  # Mock time.sleep
def test_send_chat_message(_mock_time, mock_requests):
    """Test sending a chat message"""
    # Mock responses
    mock_faucet_response = MagicMock()
    mock_faucet_response.status_code = 200
    mock_faucet_response.json.return_value = {'success': True}

    mock_retrieve_response = MagicMock()
    mock_retrieve_response.status_code = 200
    chat_history = json.dumps([
        {"role": "user", "content": "Test message"},
        {"role": "assistant", "content": "Mock Response: Test reply"}
    ])
    mock_retrieve_response.json.return_value = {
        'chat_history': base64.b64encode(chat_history.encode()).decode(),
        'cipherkey': base64.b64encode(b'mock_key').decode(),
        'iv': base64.b64encode(b'mock_iv').decode()
    }

    # Set up the request.post side effects to return different responses
    mock_requests.post.side_effect = [mock_faucet_response, mock_retrieve_response]

    # Create client with mocked encryption/decryption
    with patch('utils.crypto_helpers.encrypt') as mock_encrypt, \
         patch('utils.crypto_helpers.decrypt') as mock_decrypt:

        # Mock encrypt to return predictable values
        mock_encrypt.return_value = (
            {'ciphertext': b'mock_ciphertext', 'iv': b'mock_iv'},
            b'mock_cipherkey',
            b'mock_iv'
        )

        # Mock decrypt to return the chat history
        mock_decrypt.return_value = chat_history.encode()

        # Create client and set server key
        client = CryptoClient('https://test-server.com')
        client.server_public_key = b'mock_public_key'

        # Test sending a message
        response = client.send_chat_message("Test message")

        # Verify the proper calls were made
        assert mock_encrypt.called
        assert mock_decrypt.called
        assert mock_requests.post.call_count == 2
        assert len(response) == 2
        assert response[0]['role'] == 'user'
        assert response[1]['role'] == 'assistant'


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_yields_chunks(mock_post):
    """Streaming helper should yield parsed SSE chunks in order."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
    mock_response.iter_lines.return_value = iter([
        b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n',
        b'data: {"choices": [{"delta": {"content": "Hi there!"}}]}\n',
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_post.assert_called_with(
        'https://stream.test/api/v1/chat/completions',
        json={
            'model': 'llama-3-8b-instruct',
            'messages': messages,
            'stream': True,
        },
        timeout=30,
        stream=True,
    )

    assert chunks == [
        {
            'event': 'chunk',
            'data': {'choices': [{'delta': {'role': 'assistant'}}]},
        },
        {
            'event': 'chunk',
            'data': {'choices': [{'delta': {'content': 'Hi there!'}}]},
        },
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_json_fallback(mock_post):
    """If the server falls back to JSON, expose the parsed payload once."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {"choices": [{"message": {"content": "done"}}]}
    mock_response.text = json.dumps(mock_response.json.return_value)
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'response',
            'data': {"choices": [{"message": {"content": "done"}}]},
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_encrypted_chunks(mock_post, mock_decrypt_stream):
    """Encrypted SSE chunks should be decrypted before yielding."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY=',
        'mode': 'CBC',
    }

    session = StreamSession(aes_key=b'a' * 32)
    mock_decrypt_stream.return_value = (json.dumps({'delta': 'hi'}).encode(), session)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'event': 'delta', 'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': {'delta': 'hi'},
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_flat_encrypted_payload(mock_post, mock_decrypt_stream):
    """Decrypt when the encrypted payload lives directly under `data`."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'encrypted': True,
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY='
    }

    mock_decrypt_stream.return_value = (json.dumps({'delta': 'flat'}).encode(), StreamSession(aes_key=b'b' * 32))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'event': 'delta', 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': {'delta': 'flat'},
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_nested_encrypted_payload(mock_post, mock_decrypt_stream):
    """Decrypt when the encrypted payload is nested under an inner `data` key."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY='
    }

    nested_encrypted_body = {
        'encrypted': True,
        'data': encrypted_payload,
    }

    mock_decrypt_stream.return_value = (json.dumps({'delta': 'nested'}).encode(), StreamSession(aes_key=b'c' * 32))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'event': 'delta', 'data': nested_encrypted_body})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': {'delta': 'nested'},
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_invalid_encrypted_payload(mock_post, mock_decrypt_stream):
    """An encrypted chunk without a mapping payload should raise an error event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'encrypted': True, 'data': 'oops'})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'invalid_encrypted_chunk',
                'message': 'Received an encrypted streaming chunk without payload data.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_decrypt_failures(mock_post, mock_decrypt_stream):
    """If decryption fails the client should surface an error event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY='
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    mock_decrypt_stream.side_effect = RuntimeError('boom')

    chunks = list(client.stream_chat_completion(messages))
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_reuses_stream_sessions(mock_post, mock_decrypt_stream):
    """Streaming decrypt sessions should persist across chunks when IDs match."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    first_payload = {
        'ciphertext': base64.b64encode(b'{"delta": {"content": "part 1"}}').decode(),
        'cipherkey': base64.b64encode(b'key-one').decode(),
        'iv': base64.b64encode(b'iv-one').decode(),
        'mode': 'CBC',
        'associated_data': base64.b64encode(b'context').decode(),
    }
    second_payload = {
        'ciphertext': base64.b64encode(b'plain text segment').decode(),
        'iv': base64.b64encode(b'iv-two').decode(),
    }

    stream_events = [
        {
            'event': 'delta',
            'encrypted': True,
            'stream_session_id': 'sess-1',
            'data': first_payload,
        },
        {
            'event': 'chunk',
            'data': {
                'encrypted': True,
                'stream_session_id': 'sess-1',
                'data': second_payload,
            },
        },
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}

    def iter_lines(decode_unicode: bool = True) -> Iterator[bytes]:
        _ = decode_unicode
        for entry in stream_events:
            yield f"data: {json.dumps(entry)}\n".encode()
        yield b'data: [DONE]\n'

    mock_response.iter_lines.side_effect = iter_lines
    mock_post.return_value = mock_response

    created_session: dict[str, StreamSession] = {}

    def fake_decrypt(ciphertext_dict, _priv, *, session, encrypted_key, cipher_mode, associated_data):
        call_index = len(created_session)
        if session is None:
            assert encrypted_key == base64.b64decode(first_payload['cipherkey'])
            assert cipher_mode == 'CBC'
            assert associated_data == base64.b64decode(first_payload['associated_data'])
            new_session = StreamSession(aes_key=b'd' * 32, cipher_mode='CBC', associated_data=associated_data)
            created_session['sess'] = new_session
            return json.dumps({'delta': {'content': 'part 1'}}).encode(), new_session

        assert session is created_session['sess']
        assert encrypted_key is None
        assert cipher_mode is None
        assert associated_data is None
        return b'plain text segment', session

    mock_decrypt_stream.side_effect = fake_decrypt

    chunks = list(client.stream_chat_completion(messages))

    assert mock_decrypt_stream.call_count == 2
    assert chunks == [
        {
            'event': 'delta',
            'data': {'delta': {'content': 'part 1'}},
        },
        {
            'event': 'chunk',
            'data': 'plain text segment',
        },
    ]



@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_reports_invalid_associated_data(mock_post, mock_decrypt_stream):
    """Invalid base64 associated data should surface a decrypt error."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': base64.b64encode(b'{}').decode(),
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
        'associated_data': '!!not-base64!!',
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]



@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_non_string_cipherkey(mock_post, mock_decrypt_stream):
    """Cipherkey values must be strings for new streaming sessions."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': base64.b64encode(b'{}').decode(),
        'cipherkey': 123,
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'encrypted': True, 'stream_session_id': 'sess', 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]



@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_requires_cipherkey_for_new_session(mock_post, mock_decrypt_stream):
    """New streaming sessions must provide a cipherkey."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': base64.b64encode(b'{}').decode(),
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'encrypted': True, 'stream_session_id': 'sess', 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_stream.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]



@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_invalid_utf8_payload(mock_post, mock_decrypt_stream):
    """Decrypted payloads that are not UTF-8 should surface an error."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': base64.b64encode(b'{}').decode(),
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/event-stream"}
    mock_response.iter_lines.return_value = iter([
        f"data: {json.dumps({'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])
    mock_post.return_value = mock_response

    mock_decrypt_stream.return_value = (b'\xff\xfe', StreamSession(aes_key=b'e' * 32))

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]



@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_emits_partial_events_on_fallback(mock_post):
    """Cached streaming updates should flush before falling back to JSON."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    stream_response = MagicMock()
    stream_response.status_code = 200
    stream_response.headers = {"Content-Type": "text/event-stream"}

    def iter_lines(decode_unicode=True):
        payload = {'event': 'chunk', 'data': {'choices': [{'delta': {'content': 'Hi'}}]}}
        yield f"data: {json.dumps(payload)}\n"
        raise requests.RequestException('disconnect')

    stream_response.iter_lines.side_effect = iter_lines
    stream_response.close = MagicMock()

    fallback_response = MagicMock()
    fallback_response.status_code = 200
    fallback_response.headers = {"Content-Type": "application/json"}
    fallback_response.json.return_value = {"choices": [{"message": {"content": "full"}}]}
    fallback_response.text = json.dumps(fallback_response.json.return_value)

    mock_post.side_effect = [stream_response, fallback_response]

    chunks = list(client.stream_chat_completion(messages, max_retries=0, retry_delay=0))

    assert chunks == [
        {
            'event': 'chunk',
            'data': {
                'event': 'chunk',
                'data': {'choices': [{'delta': {'content': 'Hi'}}]},
            },
        },
        {
            'event': 'partial_response',
            'data': {
                'chunks': [
                    {
                        'event': 'chunk',
                        'data': {
                            'event': 'chunk',
                            'data': {'choices': [{'delta': {'content': 'Hi'}}]},
                        },
                    }
                ],
                'text': 'Hi',
            },
        },
        {
            'event': 'fallback',
            'data': {
                'reason': 'connection_lost',
                'message': 'Streaming connection dropped; requesting full response instead.',
            },
        },
        {
            'event': 'response',
            'data': {"choices": [{"message": {"content": "full"}}]},
        },
    ]
