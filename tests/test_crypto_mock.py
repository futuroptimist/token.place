"""
Mock tests for the CryptoClient helper class with mocked server responses
"""
import pytest
import json
import base64
from utils.crypto_helpers import CryptoClient
from unittest.mock import patch, MagicMock

# Test messages
TEST_USER_MESSAGE = "Hello, this is a test message!"
TEST_CHAT_HISTORY = [{"role": "user", "content": TEST_USER_MESSAGE}]
TEST_RESPONSE = [
    {"role": "user", "content": TEST_USER_MESSAGE},
    {"role": "assistant", "content": "Mock Response: Hello! This is a test response."}
]

@pytest.fixture
def mock_server_responses():
    """Mock server responses for testing the CryptoClient"""
    with patch('utils.crypto_helpers.requests') as mock_requests:
        # Mock next_server endpoint
        next_server_response = MagicMock()
        next_server_response.status_code = 200
        next_server_response.json.return_value = {
            'server_public_key': base64.b64encode(b'mock_server_public_key').decode('utf-8')
        }
        
        # Mock faucet endpoint
        faucet_response = MagicMock()
        faucet_response.status_code = 200
        faucet_response.json.return_value = {
            'success': True,
            'message': 'Request received'
        }
        
        # Mock retrieve endpoint
        retrieve_response = MagicMock()
        retrieve_response.status_code = 200
        retrieve_response.json.return_value = {
            'chat_history': base64.b64encode(json.dumps(TEST_RESPONSE).encode()).decode(),
            'cipherkey': base64.b64encode(b'mock_cipherkey').decode('utf-8'),
            'iv': base64.b64encode(b'mock_iv').decode('utf-8')
        }
        
        # Mock API public key endpoint
        api_key_response = MagicMock()
        api_key_response.status_code = 200
        api_key_response.json.return_value = {
            'public_key': base64.b64encode(b'mock_api_public_key').decode('utf-8')
        }
        
        # Mock API completions endpoint
        api_completions_response = MagicMock()
        api_completions_response.status_code = 200
        api_completions_response.json.return_value = {
            'encrypted': True,
            'encrypted_content': {
                'ciphertext': base64.b64encode(json.dumps({
                    'choices': [{
                        'message': {
                            'role': 'assistant',
                            'content': 'Mock Response: This is an API response.'
                        }
                    }]
                }).encode()).decode(),
                'cipherkey': base64.b64encode(b'mock_api_cipherkey').decode('utf-8'),
                'iv': base64.b64encode(b'mock_api_iv').decode('utf-8')
            }
        }
        
        # Configure the mock requests
        def get_side_effect(url, *args, **kwargs):
            if '/next_server' in url:
                return next_server_response
            elif '/api/v1/public-key' in url:
                return api_key_response
            else:
                response = MagicMock()
                response.status_code = 404
                return response
        
        def post_side_effect(url, *args, **kwargs):
            if '/faucet' in url:
                return faucet_response
            elif '/retrieve' in url:
                return retrieve_response
            elif '/api/v1/chat/completions' in url:
                return api_completions_response
            else:
                response = MagicMock()
                response.status_code = 404
                return response
        
        mock_requests.get.side_effect = get_side_effect
        mock_requests.post.side_effect = post_side_effect
        
        yield mock_requests

def test_e2e_chat_flow(mock_server_responses):
    """Test the complete end-to-end chat flow with mocked server responses"""
    # Mock the encrypt/decrypt functions
    with patch('utils.crypto_helpers.encrypt') as mock_encrypt, \
         patch('utils.crypto_helpers.decrypt') as mock_decrypt:
        
        # Configure the mocks
        mock_encrypt.return_value = (
            {'ciphertext': b'mock_ciphertext', 'iv': b'mock_iv'},
            b'mock_cipherkey',
            b'mock_iv'
        )
        
        mock_decrypt.return_value = json.dumps(TEST_RESPONSE).encode()
        
        # Create a client and test the flow
        client = CryptoClient('http://localhost:5010', debug=True)
        
        # Test fetching the server key
        assert client.fetch_server_public_key() is True
        assert client.server_public_key == b'mock_server_public_key'
        
        # Test sending a chat message
        response = client.send_chat_message(TEST_CHAT_HISTORY)
        
        # Verify the response
        assert response is not None
        assert len(response) == 2
        assert response[0]['role'] == 'user'
        assert response[0]['content'] == TEST_USER_MESSAGE
        assert response[1]['role'] == 'assistant'
        assert 'Mock Response' in response[1]['content']
        
        # Verify the mock functions were called
        assert mock_encrypt.called
        assert mock_decrypt.called
        
        # Verify API requests were made
        assert mock_server_responses.get.called
        assert mock_server_responses.post.called

def test_api_encryption_flow(mock_server_responses):
    """Test the API encryption flow with mocked server responses"""
    # Mock the encrypt/decrypt functions
    with patch('utils.crypto_helpers.encrypt') as mock_encrypt, \
         patch('utils.crypto_helpers.decrypt') as mock_decrypt:
        
        # Configure the mocks
        mock_encrypt.return_value = (
            {'ciphertext': b'mock_ciphertext', 'iv': b'mock_iv'},
            b'mock_cipherkey',
            b'mock_iv'
        )
        
        mock_decrypt.return_value = json.dumps({
            'choices': [{
                'message': {
                    'role': 'assistant',
                    'content': 'Mock Response: This is an API response.'
                }
            }]
        }).encode()
        
        # Create a client and test the flow
        client = CryptoClient('http://localhost:5010', debug=True)
        
        # Test fetching the API key
        assert client.fetch_server_public_key('/api/v1/public-key') is True
        
        # Test sending an API request
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Tell me a joke."}
        ]
        
        response = client.send_api_request(messages)
        
        # Verify the response
        assert response is not None
        assert 'choices' in response
        assert len(response['choices']) > 0
        assert 'message' in response['choices'][0]
        assert response['choices'][0]['message']['role'] == 'assistant'
        assert 'Mock Response' in response['choices'][0]['message']['content']
        
        # Verify the mock functions were called
        assert mock_encrypt.called
        assert mock_decrypt.called 
