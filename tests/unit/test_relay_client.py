"""
Unit tests for the relay client module.
"""
import base64
import json
import pytest
import sys
import requests
from unittest.mock import MagicMock, patch, call
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.networking.relay_client import RelayClient

class TestRelayClient:
    """Test class for RelayClient."""
    
    @pytest.fixture
    def mock_crypto_manager(self):
        """Fixture for a mock crypto manager."""
        mock = MagicMock()
        mock.public_key_b64 = 'mock_public_key_b64'
        mock.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv'
        }
        mock.decrypt_message.return_value = [
            {"role": "user", "content": "What is the capital of France?"}
        ]
        return mock
    
    @pytest.fixture
    def mock_model_manager(self):
        """Fixture for a mock model manager."""
        mock = MagicMock()
        mock.llama_cpp_get_response.return_value = [
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": "The capital of France is Paris."}
        ]
        return mock
    
    @pytest.fixture
    def relay_client(self, mock_crypto_manager, mock_model_manager):
        """Fixture that returns a relay client instance with mocked dependencies."""
        with patch('utils.networking.relay_client.get_config') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_get_config.return_value = mock_config
            
            client = RelayClient(
                base_url="http://localhost",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager
            )
            return client
    
    def test_initialization(self, relay_client, mock_crypto_manager, mock_model_manager):
        """Test RelayClient initialization."""
        assert relay_client.base_url == "http://localhost"
        assert relay_client.port == 5000
        assert relay_client.crypto_manager == mock_crypto_manager
        assert relay_client.model_manager == mock_model_manager
        assert relay_client.relay_url == "http://localhost:5000"
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_success(self, mock_post, relay_client, mock_crypto_manager):
        """Test successful ping to relay."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'next_ping_in_x_seconds': 5
        }
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert result == {'next_ping_in_x_seconds': 5}
        
        # Verify mock calls
        mock_post.assert_called_once_with(
            'http://localhost:5000/sink',
            json={'server_public_key': 'mock_public_key_b64'},
            timeout=10
        )
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_http_error(self, mock_post, relay_client):
        """Test ping to relay with HTTP error."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "HTTP 500"
        assert result['next_ping_in_x_seconds'] == 10
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_request_exception(self, mock_post, relay_client):
        """Test ping to relay with request exception."""
        # Setup mock to raise an exception
        mock_post.side_effect = requests.RequestException("Test connection error")
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "Test connection error"
        assert result['next_ping_in_x_seconds'] == 10
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_generic_exception(self, mock_post, relay_client):
        """Test ping to relay with generic exception."""
        # Setup mock to raise an exception
        mock_post.side_effect = Exception("Unexpected error")
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "Unexpected error"
        assert result['next_ping_in_x_seconds'] == 10
    
    def test_process_client_request_missing_fields(self, relay_client):
        """Test processing a client request with missing fields."""
        # Setup request data with missing fields
        request_data = {
            'client_public_key': 'client_key',
            # Missing 'chat_history'
            'cipherkey': 'key',
            'iv': 'iv'
        }
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    def test_process_client_request_decryption_failure(self, relay_client, mock_crypto_manager):
        """Test processing a client request with decryption failure."""
        # Setup
        request_data = {
            'client_public_key': 'client_key',
            'chat_history': 'encrypted_data',
            'cipherkey': 'key',
            'iv': 'iv'
        }
        
        # Mock decryption failure
        mock_crypto_manager.decrypt_message.return_value = None
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_success(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager):
        """Test successful processing of a client request."""
        # Setup
        request_data = {
            'client_public_key': 'client_key_b64',
            'chat_history': 'encrypted_data',
            'cipherkey': 'key',
            'iv': 'iv'
        }
        
        # Mock successful request to /source
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Success"
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is True
        
        # Verify mock calls
        mock_crypto_manager.decrypt_message.assert_called_once_with(request_data)
        mock_model_manager.llama_cpp_get_response.assert_called_once_with(
            mock_crypto_manager.decrypt_message.return_value
        )
        
        # Check the encryption and post to /source
        mock_crypto_manager.encrypt_message.assert_called_once_with(
            mock_model_manager.llama_cpp_get_response.return_value,
            base64.b64decode('client_key_b64')
        )
        
        expected_payload = {
            'client_public_key': 'client_key_b64',
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv'
        }
        mock_post.assert_called_once_with(
            'http://localhost:5000/source',
            json=expected_payload,
            timeout=10
        )
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_source_error(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager):
        """Test processing a client request with error from /source endpoint."""
        # Setup
        request_data = {
            'client_public_key': 'client_key_b64',
            'chat_history': 'encrypted_data',
            'cipherkey': 'key',
            'iv': 'iv'
        }
        
        # Mock error response from /source
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_exception(self, mock_post, relay_client, mock_crypto_manager):
        """Test processing a client request with an exception."""
        # Setup
        request_data = {
            'client_public_key': 'client_key_b64',
            'chat_history': 'encrypted_data',
            'cipherkey': 'key',
            'iv': 'iv'
        }
        
        # Mock to raise an exception
        mock_crypto_manager.decrypt_message.side_effect = Exception("Test exception")
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
        
        # Verify post was not called
        mock_post.assert_not_called()
    
    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_client_request(self, mock_sleep, mock_process, mock_ping, relay_client):
        """Test the continuous polling with a client request."""
        # Setup mock ping to return one response with client request, then an error to exit the loop
        mock_ping.side_effect = [
            {
                'client_public_key': 'client_key',
                'chat_history': 'encrypted_data',
                'cipherkey': 'key',
                'iv': 'iv',
                'next_ping_in_x_seconds': 5
            },
            Exception("Stop the test")  # To break out of the infinite loop
        ]
        
        # Mock sleep to avoid actually sleeping
        mock_sleep.return_value = None
        
        # Call the method - this would normally run forever
        try:
            relay_client.poll_relay_continuously()
        except Exception as e:
            assert str(e) == "Stop the test"
        
        # Verify mock calls
        assert mock_ping.call_count == 2
        mock_process.assert_called_once_with({
            'client_public_key': 'client_key',
            'chat_history': 'encrypted_data',
            'cipherkey': 'key',
            'iv': 'iv',
            'next_ping_in_x_seconds': 5
        })
        mock_sleep.assert_called_once_with(5)
    
    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_no_client_request(self, mock_sleep, mock_process, mock_ping, relay_client):
        """Test the continuous polling without a client request."""
        # Setup mock ping to return one response without client request, then an error to exit the loop
        mock_ping.side_effect = [
            {
                'next_ping_in_x_seconds': 5
            },
            Exception("Stop the test")  # To break out of the infinite loop
        ]
        
        # Mock sleep to avoid actually sleeping
        mock_sleep.return_value = None
        
        # Call the method - this would normally run forever
        try:
            relay_client.poll_relay_continuously()
        except Exception as e:
            assert str(e) == "Stop the test"
        
        # Verify mock calls
        assert mock_ping.call_count == 2
        mock_process.assert_not_called()
        mock_sleep.assert_called_once_with(5)
    
    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_error(self, mock_sleep, mock_ping, relay_client):
        """Test the continuous polling with an error in the response."""
        # Setup mock ping to return an error, then an exception to exit the loop
        mock_ping.side_effect = [
            {
                'error': 'Connection refused',
                'next_ping_in_x_seconds': 10
            },
            Exception("Stop the test")  # To break out of the infinite loop
        ]
        
        # Mock sleep to avoid actually sleeping
        mock_sleep.return_value = None
        
        # Call the method - this would normally run forever
        try:
            relay_client.poll_relay_continuously()
        except Exception as e:
            assert str(e) == "Stop the test"
        
        # Verify mock calls
        assert mock_ping.call_count == 2
        mock_sleep.assert_called_once_with(10) 