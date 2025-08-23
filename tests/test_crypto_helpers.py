"""
Tests for the CryptoClient helper class
"""
import pytest
import json
import base64
from utils.crypto_helpers import CryptoClient
from encrypt import generate_keys
from unittest.mock import patch, MagicMock

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


def test_clear_server_public_key(mock_crypto_client):
    """Clearing the server key resets client state"""
    client, _ = mock_crypto_client
    assert client.fetch_server_public_key() is True
    assert client.has_server_public_key() is True
    client.clear_server_public_key()
    assert client.server_public_key is None
    assert client.server_public_key_b64 is None
    assert client.has_server_public_key() is False

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

    # Mock a failed server key fetch
    with patch('utils.crypto_helpers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        result = client.fetch_server_public_key()
        assert result is False

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
