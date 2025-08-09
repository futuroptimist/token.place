"""
Encryption manager for token.place API v1
"""

import json
import base64
import logging
from typing import Dict, Any, Union, Optional

from encrypt import generate_keys, encrypt, decrypt

try:
    from config import RSA_KEY_SIZE
except ImportError:
    # Default value if config.py is not available
    RSA_KEY_SIZE = 2048

logger = logging.getLogger(__name__)


class EncryptionManager:
    """
    Manages encryption/decryption operations for the API
    """

    def __init__(self):
        """Initialize with new RSA key pair"""
        self._private_key_pem, self._public_key_pem = generate_keys()
        self.public_key_b64 = base64.b64encode(self._public_key_pem).decode('utf-8')

    def encrypt_message(self,
                       data: Dict[str, Any],
                       client_public_key: Union[str, bytes]) -> Optional[Dict[str, str]]:
        """
        Encrypt data for transmission to a client

        Args:
            data: Dictionary to encrypt
            client_public_key: Client's public key (Base64 string or bytes)

        Returns:
            Dictionary with encrypted data suitable for API response
        """
        try:
            # Convert data to JSON
            json_data = json.dumps(data).encode('utf-8')

            # Make sure client_public_key is bytes
            if isinstance(client_public_key, str):
                client_public_key = base64.b64decode(client_public_key)

            # Encrypt the data
            ciphertext_dict, cipherkey, iv = encrypt(json_data, client_public_key)

            # Encode to base64 for JSON
            ciphertext_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
            cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')
            iv_b64 = base64.b64encode(iv).decode('utf-8')

            # Return as dictionary that can be sent in API response
            return {
                "encrypted": True,
                "ciphertext": ciphertext_b64,
                "cipherkey": cipherkey_b64,
                "iv": iv_b64
            }
        except Exception:
            logger.error("Error encrypting message", exc_info=True)
            return None

    def decrypt_message(self,
                       ciphertext_dict: Dict[str, bytes],
                       cipherkey: bytes) -> Optional[bytes]:
        """
        Decrypt a message from a client

        Args:
            ciphertext_dict: Dictionary with ciphertext and iv
            cipherkey: Encrypted AES key

        Returns:
            Decrypted data as bytes, or None if failed
        """
        try:
            # Decrypt the data
            return decrypt(ciphertext_dict, cipherkey, self._private_key_pem)
        except Exception:
            logger.error("Error decrypting message", exc_info=True)
            return None

# Create singleton instance
encryption_manager = EncryptionManager()
