"""
Integration tests for the refactored server components.
Tests that the different modules can work together properly.
"""
import os
import pytest
import sys
import json
import base64
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch
from pathlib import Path
from flask import Flask

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import our refactored modules
from utils.llm.model_manager import ModelManager
from utils.crypto.crypto_manager import CryptoManager
from utils.networking.relay_client import RelayClient
from server.server_app import ServerApp

class TestServerIntegration:
    """Integration tests for the server components."""

    @pytest.fixture
    def mock_config(self):
        """Fixture that provides a mock configuration."""
        # Create a common mock config directly without patching
        mock_config = MagicMock()
        mock_config.is_production = False

        # Set up model-specific config values
        mock_config.get.side_effect = lambda key, default=None: {
            'model.filename': 'test_model.gguf',
            'model.url': 'https://example.com/model.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': self.temp_dir.name,
            'model.use_mock': True,  # Always use mock for testing
            'model.context_size': 2048,
            'model.chat_format': 'llama-3',
            'model.max_tokens': 256,
            'model.temperature': 0.8,
            'model.top_p': 0.95,
            'model.stop_tokens': [],
            'version': 'test'
        }.get(key, default)

        yield mock_config

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup temporary directories and resources."""
        # Create a temporary directory for test files
        self.temp_dir = tempfile.TemporaryDirectory()

        # Create a models directory
        models_dir = os.path.join(self.temp_dir.name, 'models')
        os.makedirs(models_dir, exist_ok=True)

        yield

        # Cleanup
        self.temp_dir.cleanup()

    @pytest.fixture
    def actual_model_manager(self, mock_config):
        """Fixture that provides a real ModelManager instance."""
        model_manager = ModelManager(mock_config)
        return model_manager

    @pytest.fixture
    def actual_crypto_manager(self, mock_config):
        """Fixture that provides a real CryptoManager instance."""
        with patch('utils.crypto.crypto_manager.get_config_lazy', return_value=mock_config):
            crypto_manager = CryptoManager()
            return crypto_manager

    @pytest.fixture
    def test_client(self, mock_config, actual_model_manager, actual_crypto_manager):
        """
        Fixture that provides a test client for the server with real components.
        The LLM is still mocked due to the model.use_mock setting.
        """
        # Create a ServerApp instance
        with patch('config.get_config', return_value=mock_config), \
             patch('utils.llm.model_manager.get_model_manager', return_value=actual_model_manager), \
             patch('utils.crypto.crypto_manager.get_crypto_manager', return_value=actual_crypto_manager):

            server_app = ServerApp(
                server_port=9000,  # Use a different port for testing
                relay_port=9001,
                relay_url="http://localhost"
            )

            # Return the test client
            with server_app.app.test_client() as client:
                yield client

    def test_health_endpoint(self, test_client):
        """Test the health endpoint."""
        response = test_client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        assert 'version' in data  # Just check that version field exists
        assert data['mock_mode'] is True

    def test_crypto_manager_integration(self, actual_crypto_manager):
        """Test that the CryptoManager properly encrypts and decrypts messages."""
        # Create a test message
        test_message = {
            "role": "user",
            "content": "What is the capital of France?"
        }

        # Get the public key for encryption
        public_key = actual_crypto_manager.public_key

        # Encrypt the message
        encrypted = actual_crypto_manager.encrypt_message(test_message, public_key)

        # Verify the encrypted message format
        assert 'chat_history' in encrypted
        assert 'cipherkey' in encrypted
        assert 'iv' in encrypted

        # Decrypt the message
        decrypted = actual_crypto_manager.decrypt_message(encrypted)

        # Verify the decryption worked correctly
        assert decrypted == test_message

    def test_model_manager_mock_mode(self, actual_model_manager):
        """Test that ModelManager works in mock mode."""
        # Ensure mock mode is enabled
        actual_model_manager.use_mock_llm = True

        # Create a test chat history
        chat_history = [
            {"role": "user", "content": "What is the capital of France?"}
        ]

        # Get a response
        response_history = actual_model_manager.llama_cpp_get_response(chat_history)

        # Verify the response
        assert len(response_history) == 2
        assert response_history[0] == chat_history[0]
        assert response_history[1]['role'] == 'assistant'
        assert 'Mock Response' in response_history[1]['content']

    @patch('utils.networking.relay_client.requests.post')
    def test_relay_client_integration(self, mock_post, actual_crypto_manager, actual_model_manager, mock_config):
        """Test that RelayClient properly integrates with CryptoManager and ModelManager."""
        # Create a RelayClient instance with patched config
        with patch('utils.networking.relay_client.get_config_lazy', return_value=mock_config):
            relay_client = RelayClient(
                base_url="http://localhost",
                port=9001,
                crypto_manager=actual_crypto_manager,
                model_manager=actual_model_manager
            )

            # Mock the response from the relay server
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = "Success"
            mock_post.return_value = mock_response

            # Create a test client public key and encrypt a request with it
            client_private_key, client_public_key = actual_crypto_manager._private_key, actual_crypto_manager._public_key
            client_public_key_b64 = base64.b64encode(client_public_key).decode('utf-8')

            # Create a test message and encrypt it with the server's public key
            test_message = [
                {"role": "user", "content": "What is the capital of France?"}
            ]
            encrypted = actual_crypto_manager.encrypt_message(test_message, actual_crypto_manager.public_key)

            # Create a request similar to what would come from the relay
            request_data = {
                'client_public_key': client_public_key_b64,
                'chat_history': encrypted['chat_history'],
                'cipherkey': encrypted['cipherkey'],
                'iv': encrypted['iv']
            }

            # Process the request
            result = relay_client.process_client_request(request_data)

            # Verify the result
            assert result is True

            # Check that we properly POSTed to /source
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert args[0] == 'http://localhost:9001/source'

            # Verify that the payload contains the expected keys
            assert 'json' in kwargs
            payload = kwargs['json']
            assert 'client_public_key' in payload
            assert 'chat_history' in payload
            assert 'cipherkey' in payload
            assert 'iv' in payload

    def test_server_initialization(self, mock_config):
        """Test that the ServerApp initializes properly."""
        with patch('config.get_config', return_value=mock_config), \
             patch('utils.llm.model_manager.get_model_manager') as mock_model_manager, \
             patch('utils.crypto.crypto_manager.get_crypto_manager') as mock_crypto_manager:

            # Mock the manager instances
            mock_model_manager.return_value = MagicMock()
            mock_model_manager.return_value.use_mock_llm = True
            mock_model_manager.return_value.download_model_if_needed.return_value = True

            mock_crypto_manager.return_value = MagicMock()

            # Create the server app
            server_app = ServerApp(
                server_port=9000,
                relay_port=9001,
                relay_url="http://localhost"
            )

            # Check that the server app was created successfully
            assert server_app.server_port == 9000
            assert server_app.relay_port == 9001
            assert server_app.relay_url == "http://localhost"
            assert server_app.app is not None
            assert server_app.relay_client is not None
