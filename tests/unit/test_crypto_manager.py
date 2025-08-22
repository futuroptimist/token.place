"""
Unit tests for the crypto manager module.
"""
import base64
import json
import pytest
import sys
from unittest.mock import MagicMock, patch
from pathlib import Path
from encrypt import encrypt

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

    @patch('utils.crypto.crypto_manager.generate_keys', side_effect=[(b'priv1', b'pub1'), (b'priv2', b'pub2')])
    def test_rotate_keys_generates_new_pair(self, mock_generate_keys):
        """rotate_keys should replace the existing key pair."""
        with patch('utils.crypto.crypto_manager.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_get_config.return_value = mock_config

            manager = CryptoManager()

        original_key = manager.public_key
        manager.rotate_keys()

        assert manager.public_key != original_key
        assert manager.public_key == b'pub2'
        assert manager.public_key_b64 == base64.b64encode(b'pub2').decode('utf-8')
        assert mock_generate_keys.call_count == 2

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

    def test_encrypt_message_none_raises(self, crypto_manager):
        """encrypt_message should reject None input."""
        with pytest.raises(ValueError, match="Message cannot be None"):
            crypto_manager.encrypt_message(None, b'client_public_key')

    def test_encrypt_message_missing_public_key(self, crypto_manager):
        """encrypt_message should reject None public key."""
        with pytest.raises(ValueError, match="Client public key cannot be None"):
            crypto_manager.encrypt_message("hi", None)

    def test_encrypt_message_invalid_type(self, crypto_manager):
        """encrypt_message should reject unsupported message types."""
        with pytest.raises(TypeError, match="Unsupported message type: int"):
            crypto_manager.encrypt_message(123, crypto_manager.public_key)

    @patch('utils.crypto.crypto_manager.encrypt')
    def test_encrypt_message_accepts_base64_key(self, mock_encrypt, crypto_manager):
        """Public key may be provided as a base64 string."""
        message = "hi"
        b64_key = base64.b64encode(b'client_public_key').decode('utf-8')

        mock_encrypted_data = {'ciphertext': b'encrypted_content', 'iv': b'iv_value'}
        mock_encrypted_key = b'encrypted_key'
        mock_iv = b'iv_value'
        mock_encrypt.return_value = (mock_encrypted_data, mock_encrypted_key, mock_iv)

        crypto_manager.encrypt_message(message, b64_key)

        mock_encrypt.assert_called_once_with(b'hi', b'client_public_key')

    def test_encrypt_message_invalid_base64_key(self, crypto_manager):
        """Invalid base64 strings raise a helpful error."""
        message = "hi"
        invalid_key = "not_base64!!"

        with pytest.raises(ValueError, match="Invalid base64-encoded public key"):
            crypto_manager.encrypt_message(message, invalid_key)

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
    def test_decrypt_message_invalid_input(self, mock_decrypt, crypto_manager):
        """Decrypting None should return None without calling decrypt."""
        result = crypto_manager.decrypt_message(None)
        assert result is None
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

    def test_log_functions_raise_keyboard_interrupt(self, monkeypatch):
        """log_info/log_error should not swallow KeyboardInterrupt."""
        from utils.crypto import crypto_manager as cm
        monkeypatch.setattr(cm, 'get_config_lazy', MagicMock(side_effect=KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            cm.log_info('hi')

        with pytest.raises(KeyboardInterrupt):
            cm.log_error('bye')


def test_log_error_logs_in_production(monkeypatch, caplog):
    """log_error should emit messages in production without tracebacks."""
    from utils.crypto import crypto_manager as cm
    monkeypatch.setattr(
        cm,
        'get_config_lazy',
        MagicMock(return_value=MagicMock(is_production=True)),
    )
    with caplog.at_level('ERROR', logger='crypto_manager'):
        try:
            raise ValueError('boom')
        except ValueError:
            cm.log_error('boom', exc_info=True)
    assert 'boom' in caplog.text
    assert 'ValueError' not in caplog.text


def test_decrypt_message_returns_bytes_for_non_utf8():
    """CryptoManager.decrypt_message returns raw bytes for non UTF-8 content."""
    manager = CryptoManager()
    message = b"\xff\xfe\xfd"
    ciphertext_dict, encrypted_key, iv = encrypt(message, manager.public_key)
    encrypted_data = {
        'chat_history': base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
        'cipherkey': base64.b64encode(encrypted_key).decode('utf-8'),
        'iv': base64.b64encode(iv).decode('utf-8'),
    }
    result = manager.decrypt_message(encrypted_data)
    assert result == message
