"""
Crypto manager module for handling encryption/decryption operations.
"""
import base64
import binascii
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
    """Log info only in non-production environments."""
    config = None
    try:
        config = get_config_lazy()
    except Exception:
        pass
    if config is None or not config.is_production:
        logger.info(message)

def log_error(message, exc_info=False):
    """Log errors in all environments.

    In production, stack traces are suppressed even when ``exc_info`` is True to avoid
    leaking sensitive information.
    """
    config = None
    try:
        config = get_config_lazy()
    except Exception:
        pass
    show_exc = exc_info and (config is None or not config.is_production)
    logger.error(message, exc_info=show_exc)

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

    def rotate_keys(self):
        """Regenerate the RSA key pair for key rotation."""
        self.initialize_keys()

    def encrypt_message(self, message: Union[str, bytes, Dict, List],
                        client_public_key: Union[str, bytes]) -> Dict[str, str]:
        """
        Encrypt a message for a client using their public key.

        Args:
            message: The message to encrypt (string, bytes, dict, or list)
            client_public_key: The client's public key in bytes or base64 string

        Returns:
            Dict with 'chat_history' (base64 encoded ciphertext), 'cipherkey' (encrypted key),
            and 'iv' (initialization vector)

        Raises:
            ValueError: If ``message`` or ``client_public_key`` is ``None``.
            TypeError: If ``message`` is of an unsupported type.
        """
        try:
            if message is None:
                raise ValueError("Message cannot be None")
            if client_public_key is None:
                raise ValueError("Client public key cannot be None")

            # Convert message to bytes if it's not already
            if isinstance(message, (dict, list)):
                message_bytes = json.dumps(message).encode('utf-8')
            elif isinstance(message, str):
                message_bytes = message.encode('utf-8')
            elif isinstance(message, bytes):
                message_bytes = message
            else:
                raise TypeError(
                    f"Unsupported message type: {type(message).__name__}"
                )

            # Ensure client_public_key is bytes
            if isinstance(client_public_key, str):
                try:
                    client_public_key = base64.b64decode(client_public_key)
                except (binascii.Error, ValueError) as e:
                    raise ValueError("Invalid base64-encoded public key") from e

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
            if not isinstance(encrypted_data, dict):
                log_error("Encrypted data must be a dict")
                return None

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
