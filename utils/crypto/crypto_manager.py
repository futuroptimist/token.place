"""
Crypto manager module for handling encryption/decryption operations.
"""
import base64
import binascii
import json
import logging
import re
from functools import lru_cache
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple, Union, cast

# Import from the existing encrypt.py
from encrypt import encrypt, decrypt, generate_keys
from utils.performance import get_encryption_monitor

# Configure logging
logger = logging.getLogger('crypto_manager')


MessagePayload = Union[str, bytes, Dict[str, Any], List[Any]]
ClientKeyInput = Union[str, bytes]
EncryptedPayload = Dict[str, str]


@lru_cache(maxsize=256)
def _decode_client_public_key(cleaned_key: str) -> bytes:
    """Decode a base64 public key string with whitespace removed."""

    return base64.b64decode(cleaned_key, validate=True)


def _coerce_message_bytes(message: MessagePayload) -> bytes:
    """Convert supported message payloads into a UTF-8 encoded byte string."""

    if isinstance(message, (dict, list)):
        return json.dumps(message).encode("utf-8")
    if isinstance(message, str):
        return message.encode("utf-8")
    if isinstance(message, bytes):
        return message

    raise TypeError(f"Unsupported message type: {type(message).__name__}")


def _normalize_client_public_key(client_public_key: ClientKeyInput) -> bytes:
    """Convert a client-provided public key to raw bytes."""

    if isinstance(client_public_key, bytes):
        return client_public_key
    if isinstance(client_public_key, str):
        if "-----BEGIN" in client_public_key:
            return client_public_key.encode("utf-8")
        cleaned_key = re.sub(r"\s+", "", client_public_key)
        return _decode_client_public_key(cleaned_key)

    raise TypeError(
        f"Unsupported client public key type: {type(client_public_key).__name__}"
    )


def _coerce_encrypted_payload(
    encrypted_data: Union[EncryptedPayload, str]
) -> Optional[EncryptedPayload]:
    """Return an encrypted payload dict from either a dict or JSON string."""

    if isinstance(encrypted_data, str):
        try:
            loaded: Any = json.loads(encrypted_data)
        except json.JSONDecodeError:
            log_error("Encrypted data string is not valid JSON")
            return None
    elif isinstance(encrypted_data, dict):
        loaded = encrypted_data
    else:
        log_error("Encrypted data must be a dict or JSON string")
        return None

    if not isinstance(loaded, dict):
        log_error("Encrypted data JSON must decode to a dictionary")
        return None

    return cast(EncryptedPayload, loaded)


def _deserialize_encrypted_payload(
    payload: EncryptedPayload,
) -> Optional[Tuple[Dict[str, bytes], bytes]]:
    """Decode the base64 payload into ciphertext and cipher key components."""

    required_fields = ("chat_history", "cipherkey", "iv")
    missing_fields = [field for field in required_fields if not payload.get(field)]
    if missing_fields:
        log_error("Missing required encryption fields")
        return None

    try:
        iv = base64.b64decode(payload["iv"])
        ciphertext = base64.b64decode(payload["chat_history"])
        cipherkey = base64.b64decode(payload["cipherkey"])
    except (binascii.Error, ValueError):
        log_error("Encrypted payload contains invalid base64 data")
        return None

    return {"ciphertext": ciphertext, "iv": iv}, cipherkey


def get_config_lazy():
    """Lazy import of config to avoid circular imports"""
    from config import get_config
    return get_config()

def _log(level: str, message: str, *, exc_info: bool = False) -> None:
    """Internal helper to log messages based on environment settings.

    Info logs are suppressed in production; error logs always emit but hide
    stack traces in production environments.
    """
    try:
        config = get_config_lazy()
        is_production = config.is_production
    except Exception:
        is_production = False

    logger_func = getattr(logger, level)
    if level == "info":
        if not is_production:
            logger_func(message)
    else:
        logger_func(message, exc_info=exc_info and not is_production)


def log_info(message: str) -> None:
    """Log info only in non-production environments."""
    _log("info", message)


def log_error(message: str, exc_info: bool = False) -> None:
    """Log errors in all environments.

    In production, stack traces are suppressed even when ``exc_info`` is True
    to avoid leaking sensitive information.
    """
    _log("error", message, exc_info=exc_info)

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

    def encrypt_message(
        self,
        message: MessagePayload,
        client_public_key: ClientKeyInput,
    ) -> Dict[str, str]:
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
            message_bytes = _coerce_message_bytes(message)
            try:
                client_public_key_bytes = _normalize_client_public_key(
                    client_public_key
                )
            except (binascii.Error, ValueError) as e:
                raise ValueError("Invalid base64-encoded public key") from e

            monitor = get_encryption_monitor()
            record_metrics = monitor.is_enabled
            start_time = perf_counter() if record_metrics else None

            # Encrypt the message
            encrypted_data, encrypted_key, iv = encrypt(
                message_bytes, client_public_key_bytes
            )

            # Base64 encode for JSON compatibility
            encrypted_data_b64 = base64.b64encode(encrypted_data['ciphertext']).decode('utf-8')
            encrypted_key_b64 = base64.b64encode(encrypted_key).decode('utf-8')
            iv_b64 = base64.b64encode(iv).decode('utf-8')

            response = {
                'chat_history': encrypted_data_b64,
                'cipherkey': encrypted_key_b64,
                'iv': iv_b64
            }

            if record_metrics and start_time is not None:
                monitor.record('encrypt', len(message_bytes), perf_counter() - start_time)

            return response
        except Exception as e:
            log_error(f"Error encrypting message: {e}", exc_info=True)
            raise

    def decrypt_message(
        self, encrypted_data: Union[EncryptedPayload, str]
    ) -> Optional[Union[Dict, str, bytes]]:
        """Decrypt a message using the server's private key.

        Args:
            encrypted_data: Dict or JSON string containing 'chat_history', 'cipherkey', and 'iv' in
                base64

        Returns:
            Decrypted message as a dict, string, or raw bytes when the content is not UTF-8.
            Returns None if decryption fails.
        """
        try:
            payload = _coerce_encrypted_payload(encrypted_data)
            if payload is None:
                return None

            monitor = get_encryption_monitor()
            record_metrics = monitor.is_enabled
            start_time = perf_counter() if record_metrics else None

            # Extract and decode the encrypted data
            parsed_payload = _deserialize_encrypted_payload(payload)
            if parsed_payload is None:
                return None

            encrypted_chat_history_dict, cipherkey = parsed_payload

            # Decrypt the message
            decrypted_bytes = decrypt(encrypted_chat_history_dict, cipherkey, self._private_key)

            if record_metrics and start_time is not None and decrypted_bytes is not None:
                monitor.record(
                    'decrypt',
                    len(encrypted_chat_history_dict['ciphertext']),
                    perf_counter() - start_time,
                )

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
