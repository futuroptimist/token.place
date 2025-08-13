"""
Crypto manager module for handling encryption/decryption operations.
"""
import base64
import json
import logging
from typing import Dict, Tuple, Any, Optional, Union, List

# Import from the existing encrypt.py
from encrypt import encrypt, decrypt, generate_keys

# Configure logging
logger = logging.getLogger('crypto_manager')

def get_config_lazy():
    """Lazy import of config to avoid circular imports"""
    from config import get_config
    return get_config()

def log_info(message):
    """Log info only in non-production environments"""
    try:
        config = get_config_lazy()
        if not config.is_production:
            logger.info(message)
    except Exception:
        # Fallback to always log if config is not available
        logger.info(message)

def log_error(message, exc_info=False):
    """Log errors only in non-production environments"""
    try:
        config = get_config_lazy()
        if not config.is_production:
            logger.error(message, exc_info=exc_info)
    except Exception:
        # Fallback to always log if config is not available
        logger.error(message, exc_info=exc_info)

class CryptoManager:
    """
    Manages encryption and decryption operations for server-client communication.
    """
    def __init__(self):
        """Initialize the CryptoManager with configuration."""
        self._private_key = None
        self._public_key = None
        self._public_key_b64 = None

        # Initialize keys
        self.initialize_keys()

    def initialize_keys(self):
        """Generate RSA key pair for secure communication."""
        try:
            self._private_key, self._public_key = generate_keys()
            self._public_key_b64 = base64.b64encode(self._public_key).decode('utf-8')
            log_info("Crypto keys generated successfully.")
        except Exception as e:
            log_error(f"Failed to generate crypto keys: {e}", exc_info=True)
            raise RuntimeError("Failed to initialize cryptography keys") from e

    @property
    def public_key(self):
        """Get the public key in raw bytes."""
        return self._public_key

    @property
    def public_key_b64(self):
        """Get the base64-encoded public key."""
        return self._public_key_b64

    def encrypt_message(self, message: Union[str, bytes, Dict, List], client_public_key: bytes) -> Dict[str, str]:
        """
        Encrypt a message for a client using their public key.

        Args:
            message: The message to encrypt (string, bytes, dict, or list)
            client_public_key: The client's public key in bytes

        Returns:
            Dict with 'chat_history' (base64 encoded ciphertext), 'cipherkey' (encrypted key),
            and 'iv' (initialization vector)

        Raises:
            ValueError: If ``message`` is ``None``.
        """
        try:
            if message is None:
                raise ValueError("Message cannot be None")

            # Convert message to bytes if it's not already
            if isinstance(message, (dict, list)):
                message_bytes = json.dumps(message).encode('utf-8')
            elif isinstance(message, str):
                message_bytes = message.encode('utf-8')
            else:
                message_bytes = message

            # Encrypt the message
            encrypted_data, encrypted_key, iv = encrypt(message_bytes, client_public_key)

            # Base64 encode for JSON compatibility
            encrypted_data_b64 = base64.b64encode(encrypted_data['ciphertext']).decode('utf-8')
            encrypted_key_b64 = base64.b64encode(encrypted_key).decode('utf-8')
            iv_b64 = base64.b64encode(iv).decode('utf-8')

            return {
                'chat_history': encrypted_data_b64,
                'cipherkey': encrypted_key_b64,
                'iv': iv_b64
            }
        except Exception as e:
            log_error(f"Error encrypting message: {e}", exc_info=True)
            raise

    def decrypt_message(self, encrypted_data: Dict[str, str]) -> Optional[Union[Dict, str, bytes]]:
        """
        Decrypt a message using the server's private key.

        Args:
            encrypted_data: Dict containing 'chat_history', 'cipherkey', and 'iv' in base64

        Returns:
            Decrypted message as a dict, string, or raw bytes when the content is not UTF-8.
            Returns None if decryption fails.
        """
        try:
            # Extract and decode the encrypted data
            encrypted_chat_history_b64 = encrypted_data.get('chat_history')
            cipherkey_b64 = encrypted_data.get('cipherkey')
            iv_b64 = encrypted_data.get('iv')

            if not all([encrypted_chat_history_b64, cipherkey_b64, iv_b64]):
                log_error("Missing required encryption fields")
                return None

            iv = base64.b64decode(iv_b64)
            encrypted_chat_history_dict = {'ciphertext': base64.b64decode(encrypted_chat_history_b64), 'iv': iv}
            cipherkey = base64.b64decode(cipherkey_b64)

            # Decrypt the message
            decrypted_bytes = decrypt(encrypted_chat_history_dict, cipherkey, self._private_key)

            if decrypted_bytes is None:
                log_error("Decryption failed, returning None")
                return None

            # Parse the decrypted data
            try:
                text = decrypted_bytes.decode('utf-8')
            except UnicodeDecodeError:
                return decrypted_bytes

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

        except Exception as e:
            log_error(f"Error decrypting message: {e}", exc_info=True)
            return None

# Delay instantiation to avoid circular imports
crypto_manager = None

def get_crypto_manager():
    """Get the global crypto manager instance, creating it if necessary."""
    global crypto_manager
    if crypto_manager is None:
        crypto_manager = CryptoManager()
    return crypto_manager
