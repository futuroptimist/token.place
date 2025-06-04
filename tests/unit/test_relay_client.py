"""
Unit tests for the relay client module.
"""
import base64
import json
import pytest
import sys
import requests
import jsonschema
from unittest.mock import MagicMock, patch, call
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.networking.relay_client import RelayClient, MESSAGE_SCHEMA, RELAY_RESPONSE_SCHEMA

# Common test data
TEST_VALID_RESPONSE = {
    'client_public_key': 'Y2xpZW50X2tleV9iNjQ=',  # Base64 encoded "client_key_b64"
    'chat_history': 'encrypted_data',
    'cipherkey': 'key',
    'iv': 'iv',
    'next_ping_in_x_seconds': 5
}

TEST_ERROR_RESPONSE = {
    'error': 'Connection refused',
    'next_ping_in_x_seconds': 10
}

TEST_NO_REQUEST_RESPONSE = {
    'next_ping_in_x_seconds': 5
}

# Create a better time mock with a context manager
class TimeMock:
    """A context manager for mocking time.sleep"""
    def __init__(self, mock_sleep):
        self.mock_sleep = mock_sleep
        self.sleep_calls = []
        
    def __enter__(self):
        # Save original side_effect if it exists
        self.original_side_effect = self.mock_sleep.side_effect
        
        # Create a wrapper to capture the sleep calls
        def wrapper(seconds):
            self.sleep_calls.append(seconds)
            # If there's an original side effect that's callable, call it
            if callable(self.original_side_effect):
                return self.original_side_effect(seconds)
            return None
            
        self.mock_sleep.side_effect = wrapper
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Restore original side_effect (if needed)
        if hasattr(self, 'original_side_effect'):
            self.mock_sleep.side_effect = self.original_side_effect
        
    def assert_slept_for(self, seconds):
        """Assert that sleep was called with the given duration"""
        assert seconds in self.sleep_calls, f"Expected sleep({seconds}), got {self.sleep_calls}"

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
    def config_values(self):
        """Fixture for mock config values."""
        return {
            'relay.request_timeout': 15
        }
    
    @pytest.fixture
    def relay_client(self, mock_crypto_manager, mock_model_manager, config_values):
        """Fixture that returns a relay client instance with mocked dependencies."""
        with patch('utils.networking.relay_client.get_config') as mock_get_config:
            # Create a MagicMock that also implements get method
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default: config_values.get(key, default)
            mock_get_config.return_value = mock_config
            
            client = RelayClient(
                base_url="http://localhost",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager
            )
            return client
    
    @pytest.fixture
    def mock_http_response(self):
        """Fixture for mock HTTP response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Success"
        return mock_response
        
    def test_initialization(self, relay_client, mock_crypto_manager, mock_model_manager, config_values):
        """Test RelayClient initialization."""
        assert relay_client.base_url == "http://localhost"
        assert relay_client.port == 5000
        assert relay_client.crypto_manager == mock_crypto_manager
        assert relay_client.model_manager == mock_model_manager
        assert relay_client.relay_url == "http://localhost:5000"
        assert relay_client.stop_polling is True  # Now initialized to True
        assert relay_client._request_timeout == 10  # Default value from RelayClient
    
    def test_start_stop_methods(self, relay_client):
        """Test start and stop methods."""
        # Client starts with stop_polling = True
        assert relay_client.stop_polling is True
        
        # After start(), stop_polling should be False
        relay_client.start()
        assert relay_client.stop_polling is False
        
        # After stop(), stop_polling should be True again
        relay_client.stop()
        assert relay_client.stop_polling is True
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_success(self, mock_post, relay_client, mock_crypto_manager):
        """Test successful ping to relay."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = TEST_NO_REQUEST_RESPONSE
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert result == TEST_NO_REQUEST_RESPONSE
        
        # Verify mock calls
        mock_post.assert_called_once_with(
            'http://localhost:5000/sink',
            json={'server_public_key': 'mock_public_key_b64'},
            timeout=relay_client._request_timeout
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
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_request_exception(self, mock_post, relay_client):
        """Test ping to relay with request exception."""
        # Setup mock to raise an exception
        mock_post.side_effect = requests.ConnectionError("Test connection error")
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "Test connection error"
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_json_decode_error(self, mock_post, relay_client):
        """Test ping to relay with JSON decode error."""
        # Setup mock to return invalid JSON
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "{", 0)
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert "Invalid JSON" in result['error']
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
    
    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_schema_validation_error(self, mock_post, relay_client):
        """Test ping to relay with schema validation error."""
        # Setup mock to return response that fails schema validation
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'invalid': 'response'}  # Missing required fields
        mock_post.return_value = mock_response
        
        # Call the method
        result = relay_client.ping_relay()
        
        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert "Invalid response format" in result['error']
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
    
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
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
    
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
        request_data = TEST_VALID_RESPONSE.copy()
        
        # Mock decryption failure
        mock_crypto_manager.decrypt_message.return_value = None
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_success(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager, mock_http_response):
        """Test successful processing of a client request."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()
        
        # Set up HTTP response
        mock_http_response.status_code = 200
        mock_post.return_value = mock_http_response
        
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
            base64.b64decode('Y2xpZW50X2tleV9iNjQ=')
        )
        
        expected_payload = {
            'client_public_key': 'Y2xpZW50X2tleV9iNjQ=',
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv'
        }
        mock_post.assert_called_once_with(
            'http://localhost:5000/source',
            json=expected_payload,
            timeout=relay_client._request_timeout
        )
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_source_error(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager, mock_http_response):
        """Test processing a client request with error from /source endpoint."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()
        
        # Mock error response from /source
        mock_http_response.status_code = 500
        mock_http_response.text = "Internal server error"
        mock_post.return_value = mock_http_response
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_empty_response(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager, mock_http_response):
        """Test processing a client request with empty response from /source."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()
        
        # Mock empty response from /source
        mock_http_response.status_code = 200
        mock_http_response.text = ""
        mock_post.return_value = mock_http_response
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_connection_error(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager):
        """Test processing a client request with connection error."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()
        
        # Mock to raise a connection error
        mock_post.side_effect = requests.ConnectionError("Test connection error")
        
        # Call the method
        result = relay_client.process_client_request(request_data)
        
        # Check the result
        assert result is False
    
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_exception(self, mock_post, relay_client, mock_crypto_manager):
        """Test processing a client request with an exception."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()
        
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
        # Setup to return a client request on first call
        mock_ping.side_effect = [TEST_VALID_RESPONSE]
        
        # Start polling
        relay_client.start()
        
        # Set up a callback that will stop polling after processing
        def stop_after_processing(*args, **kwargs):
            relay_client.stop()
            return True
            
        mock_process.side_effect = stop_after_processing
        
        # Call the method
        relay_client.poll_relay_continuously()
        
        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_process.assert_called_once_with(TEST_VALID_RESPONSE)
        mock_sleep.assert_called_once_with(5)  # Direct check of sleep call
        
        # Verify that polling was stopped
        assert relay_client.stop_polling is True
    
    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_no_client_request(self, mock_sleep, mock_process, mock_ping, relay_client):
        """Test the continuous polling without a client request."""
        # Setup mock ping to return response without client request
        mock_ping.side_effect = [TEST_NO_REQUEST_RESPONSE]
        
        # Start polling
        relay_client.start()
        
        # Set up a callback to stop after sleep
        def stop_after_sleep(seconds):
            relay_client.stop()
            return None
            
        mock_sleep.side_effect = stop_after_sleep
        
        # Call the method
        relay_client.poll_relay_continuously()
        
        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_process.assert_not_called()
        mock_sleep.assert_called_once_with(5)  # Direct check of sleep call
        
        # Verify that polling was stopped
        assert relay_client.stop_polling is True
    
    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_error(self, mock_sleep, mock_ping, relay_client):
        """Test the continuous polling with an error in the response."""
        # Setup mock ping to return an error response
        mock_ping.side_effect = [TEST_ERROR_RESPONSE]
        
        # Start polling
        relay_client.start()
        
        # Set up a callback to stop after sleep
        def stop_after_sleep(seconds):
            relay_client.stop()
            return None
            
        mock_sleep.side_effect = stop_after_sleep
        
        # Call the method
        relay_client.poll_relay_continuously()
        
        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_sleep.assert_called_once_with(10)  # Direct check of sleep call
        
        # Verify that polling was stopped
        assert relay_client.stop_polling is True
    
    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_invalid_response(self, mock_sleep, mock_ping, relay_client):
        """Test polling with an invalid response (missing required fields)."""
        # Setup mock ping to return an invalid response
        mock_ping.side_effect = [{'invalid': 'response'}]  # Missing next_ping_in_x_seconds
        
        # Start polling
        relay_client.start()
        
        # Set up a callback to stop after sleep
        def stop_after_sleep(seconds):
            relay_client.stop()
            return None
            
        mock_sleep.side_effect = stop_after_sleep
        
        # Call the method
        relay_client.poll_relay_continuously()
        
        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_sleep.assert_called_once_with(relay_client._request_timeout)  # Direct check of sleep call
        
        # Verify that polling was stopped
        assert relay_client.stop_polling is True 
