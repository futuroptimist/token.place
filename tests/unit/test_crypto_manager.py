"""
Unit tests for the crypto manager module.
"""
import base64
import json
import pytest
import sys
from unittest.mock import MagicMock, patch
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.crypto.crypto_manager import CryptoManager

class TestCryptoManager:
    """Test class for CryptoManager."""
    
    @pytest.fixture
    def crypto_manager(self):
        """Fixture that returns a crypto manager instance with mocked dependencies."""
        with patch('utils.crypto.crypto_manager.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_get_config.return_value = mock_config
            
            with patch('utils.crypto.crypto_manager.generate_keys') as mock_generate_keys:
                # Mock the key generation to return predictable test keys
                mock_private_key = b'test_private_key'
                mock_public_key = b'test_public_key'
                mock_generate_keys.return_value = (mock_private_key, mock_public_key)
                
                manager = CryptoManager()
                yield manager
    
    def test_initialization(self, crypto_manager):
        """Test that the CryptoManager initializes with keys."""
        assert crypto_manager._private_key == b'test_private_key'
        assert crypto_manager._public_key == b'test_public_key'
        assert crypto_manager._public_key_b64 == 'dGVzdF9wdWJsaWNfa2V5'  # base64 encoded 'test_public_key'
    
    def test_property_getters(self, crypto_manager):
        """Test the property getters for public keys."""
        assert crypto_manager.public_key == b'test_public_key'
        assert crypto_manager.public_key_b64 == 'dGVzdF9wdWJsaWNfa2V5'
    
    @patch('utils.crypto.crypto_manager.encrypt')
    def test_encrypt_message_dict(self, mock_encrypt, crypto_manager):
        """Test encrypting a dictionary message."""
        # Setup
        message = {"test": "data"}
        client_public_key = b'client_public_key'
        
        # Mock encrypt to return predictable values
        mock_encrypted_data = {'ciphertext': b'encrypted_content', 'iv': b'iv_value'}
        mock_encrypted_key = b'encrypted_key'
        mock_iv = b'iv_value'
        mock_encrypt.return_value = (mock_encrypted_data, mock_encrypted_key, mock_iv)
        
        # Call the method
        result = crypto_manager.encrypt_message(message, client_public_key)
        
        # Check the result
        assert 'chat_history' in result
        assert 'cipherkey' in result
        assert 'iv' in result
        assert result['chat_history'] == base64.b64encode(b'encrypted_content').decode('utf-8')
        assert result['cipherkey'] == base64.b64encode(b'encrypted_key').decode('utf-8')
        assert result['iv'] == base64.b64encode(b'iv_value').decode('utf-8')
        
        # Verify mock calls
        expected_json = json.dumps(message).encode('utf-8')
        mock_encrypt.assert_called_once_with(expected_json, client_public_key)
    
    @patch('utils.crypto.crypto_manager.encrypt')
    def test_encrypt_message_string(self, mock_encrypt, crypto_manager):
        """Test encrypting a string message."""
        # Setup
        message = "test message"
        client_public_key = b'client_public_key'
        
        # Mock encrypt to return predictable values
        mock_encrypted_data = {'ciphertext': b'encrypted_content', 'iv': b'iv_value'}
        mock_encrypted_key = b'encrypted_key'
        mock_iv = b'iv_value'
        mock_encrypt.return_value = (mock_encrypted_data, mock_encrypted_key, mock_iv)
        
        # Call the method
        result = crypto_manager.encrypt_message(message, client_public_key)
        
        # Check the result
        assert 'chat_history' in result
        assert 'cipherkey' in result
        assert 'iv' in result
        
        # Verify mock calls
        mock_encrypt.assert_called_once_with(b'test message', client_public_key)
    
    @patch('utils.crypto.crypto_manager.encrypt')
    def test_encrypt_message_bytes(self, mock_encrypt, crypto_manager):
        """Test encrypting a bytes message."""
        # Setup
        message = b'test bytes'
        client_public_key = b'client_public_key'
        
        # Mock encrypt to return predictable values
        mock_encrypted_data = {'ciphertext': b'encrypted_content', 'iv': b'iv_value'}
        mock_encrypted_key = b'encrypted_key'
        mock_iv = b'iv_value'
        mock_encrypt.return_value = (mock_encrypted_data, mock_encrypted_key, mock_iv)
        
        # Call the method
        result = crypto_manager.encrypt_message(message, client_public_key)
        
        # Check the result
        assert 'chat_history' in result
        assert 'cipherkey' in result
        assert 'iv' in result
        
        # Verify mock calls
        mock_encrypt.assert_called_once_with(b'test bytes', client_public_key)
    
    @patch('utils.crypto.crypto_manager.encrypt')
    def test_encrypt_message_exception(self, mock_encrypt, crypto_manager):
        """Test handling of exceptions during encryption."""
        # Setup
        message = {"test": "data"}
        client_public_key = b'client_public_key'
        
        # Mock encrypt to raise an exception
        mock_encrypt.side_effect = Exception("Test encryption error")
        
        # Call the method and check for exception
        with pytest.raises(Exception, match="Test encryption error"):
            crypto_manager.encrypt_message(message, client_public_key)
    
    @patch('utils.crypto.crypto_manager.decrypt')
    def test_decrypt_message_json(self, mock_decrypt, crypto_manager):
        """Test decrypting a message that contains JSON."""
        # Setup
        encrypted_data = {
            'chat_history': base64.b64encode(b'encrypted_content').decode('utf-8'),
            'cipherkey': base64.b64encode(b'encrypted_key').decode('utf-8'),
            'iv': base64.b64encode(b'iv_value').decode('utf-8')
        }
        
        # Mock decrypt to return JSON content
        decrypted_content = b'{"message": "decrypted content"}'
        mock_decrypt.return_value = decrypted_content
        
        # Call the method
        result = crypto_manager.decrypt_message(encrypted_data)
        
        # Check the result
        assert result == {"message": "decrypted content"}
        
        # Verify mock calls
        expected_encrypted_dict = {'ciphertext': b'encrypted_content', 'iv': b'iv_value'}
        mock_decrypt.assert_called_once_with(expected_encrypted_dict, b'encrypted_key', b'test_private_key')
    
    @patch('utils.crypto.crypto_manager.decrypt')
    def test_decrypt_message_non_json(self, mock_decrypt, crypto_manager):
        """Test decrypting a message that is not JSON."""
        # Setup
        encrypted_data = {
            'chat_history': base64.b64encode(b'encrypted_content').decode('utf-8'),
            'cipherkey': base64.b64encode(b'encrypted_key').decode('utf-8'),
            'iv': base64.b64encode(b'iv_value').decode('utf-8')
        }
        
        # Mock decrypt to return non-JSON content
        decrypted_content = b'just plain text'
        mock_decrypt.return_value = decrypted_content
        
        # Call the method
        result = crypto_manager.decrypt_message(encrypted_data)
        
        # Check the result
        assert result == "just plain text"
        
        # Verify mock calls
        expected_encrypted_dict = {'ciphertext': b'encrypted_content', 'iv': b'iv_value'}
        mock_decrypt.assert_called_once_with(expected_encrypted_dict, b'encrypted_key', b'test_private_key')
    
    @patch('utils.crypto.crypto_manager.decrypt')
    def test_decrypt_message_missing_fields(self, mock_decrypt, crypto_manager):
        """Test decrypting a message with missing fields."""
        # Setup - missing 'iv'
        encrypted_data = {
            'chat_history': base64.b64encode(b'encrypted_content').decode('utf-8'),
            'cipherkey': base64.b64encode(b'encrypted_key').decode('utf-8')
            # Missing 'iv'
        }
        
        # Call the method
        result = crypto_manager.decrypt_message(encrypted_data)
        
        # Check the result
        assert result is None
        
        # Verify mock was not called
        mock_decrypt.assert_not_called()
    
    @patch('utils.crypto.crypto_manager.decrypt')
    def test_decrypt_message_decrypt_returns_none(self, mock_decrypt, crypto_manager):
        """Test when the decrypt function returns None."""
        # Setup
        encrypted_data = {
            'chat_history': base64.b64encode(b'encrypted_content').decode('utf-8'),
            'cipherkey': base64.b64encode(b'encrypted_key').decode('utf-8'),
            'iv': base64.b64encode(b'iv_value').decode('utf-8')
        }
        
        # Mock decrypt to return None
        mock_decrypt.return_value = None
        
        # Call the method
        result = crypto_manager.decrypt_message(encrypted_data)
        
        # Check the result
        assert result is None
    
    @patch('utils.crypto.crypto_manager.decrypt')
    def test_decrypt_message_exception(self, mock_decrypt, crypto_manager):
        """Test handling of exceptions during decryption."""
        # Setup
        encrypted_data = {
            'chat_history': base64.b64encode(b'encrypted_content').decode('utf-8'),
            'cipherkey': base64.b64encode(b'encrypted_key').decode('utf-8'),
            'iv': base64.b64encode(b'iv_value').decode('utf-8')
        }
        
        # Mock decrypt to raise an exception
        mock_decrypt.side_effect = Exception("Test decryption error")
        
        # Call the method
        result = crypto_manager.decrypt_message(encrypted_data)
        
        # Check the result - should return None on exception
        assert result is None
    
    @patch('utils.crypto.crypto_manager.generate_keys')
    def test_initialize_keys_exception(self, mock_generate_keys):
        """Test handling of exceptions during key initialization."""
        # Mock generate_keys to raise an exception
        mock_generate_keys.side_effect = Exception("Test key generation error")
        
        # Check for exception when initializing CryptoManager
        with pytest.raises(RuntimeError):
            CryptoManager() 

    def test_log_functions_fallback(self, monkeypatch):
        """Ensure log helpers log when config retrieval fails."""
        from utils.crypto import crypto_manager as cm
        logger = MagicMock()
        monkeypatch.setattr(cm, 'logger', logger)
        monkeypatch.setattr(cm, 'get_config_lazy', MagicMock(side_effect=RuntimeError()))

        cm.log_info('hi')
        logger.info.assert_called_with('hi')

        cm.log_error('bye')
        logger.error.assert_called_with('bye', exc_info=False)
