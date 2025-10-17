# Made with help from https://www.youtube.com/watch?v=pOx2TYwR590
# Github: https://github.com/cgossi/fundamental_cryptography_with_python

import os
import base64
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Optional, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import CBC, GCM
from cryptography.hazmat.primitives.hashes import SHA256
import secrets

# Import config, but use fallback values if import fails
# This allows encrypt.py to work standalone but also in the larger application
try:
    import config as _config  # type: ignore[import-not-found]
except ImportError:
    # Default values if config.py is not available
    RSA_KEY_SIZE = 2048
    AES_KEY_SIZE = 32  # 256 bits
    IV_SIZE = 16  # 128 bits
    GCM_IV_SIZE = 12  # 96 bits recommended for GCM
else:
    RSA_KEY_SIZE = getattr(_config, "RSA_KEY_SIZE", 2048)
    AES_KEY_SIZE = getattr(_config, "AES_KEY_SIZE", 32)
    IV_SIZE = getattr(_config, "IV_SIZE", 16)
    GCM_IV_SIZE = getattr(_config, "GCM_IV_SIZE", 12)

logger = logging.getLogger(__name__)

MIN_RSA_KEY_SIZE = 2048


@lru_cache(maxsize=8)
def _load_private_key_cached(private_key_pem: bytes):
    """Deserialize a PEM private key while caching the result for reuse."""

    return serialization.load_pem_private_key(
        private_key_pem,
        password=None,
        backend=default_backend(),
    )


def _ensure_bytes(value: Optional[bytes | bytearray], field_name: str) -> bytes:
    """Validate that *value* is bytes-like and return it as ``bytes``."""

    if not isinstance(value, (bytes, bytearray)):
        raise TypeError(f"{field_name} must be bytes-like")
    return bytes(value)


def _normalise_mode(cipher_mode: Optional[str]) -> str:
    """Return a canonical symmetric mode name."""

    mode = (cipher_mode or "CBC").upper()
    if mode not in {"CBC", "GCM"}:
        raise ValueError(f"Unsupported cipher_mode: {cipher_mode}")
    return mode


def _encrypt_with_key(
    plaintext: bytes,
    aes_key: bytes,
    *,
    cipher_mode: str,
    associated_data: Optional[bytes] = None,
) -> Dict[str, bytes]:
    """Encrypt *plaintext* using an existing AES session key."""

    plaintext_bytes = _ensure_bytes(plaintext, "plaintext")
    aes_bytes = _ensure_bytes(aes_key, "aes_key")
    mode = _normalise_mode(cipher_mode)

    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data must be bytes-like when provided")

    if mode == "CBC":
        iv = secrets.token_bytes(IV_SIZE)
        cipher = Cipher(AES(aes_bytes), CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        padded_data = pkcs7_pad(plaintext_bytes, 16)
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        return {"ciphertext": ciphertext, "iv": iv}

    iv = secrets.token_bytes(GCM_IV_SIZE)
    encryptor = Cipher(AES(aes_bytes), GCM(iv), backend=default_backend()).encryptor()
    if associated_data:
        encryptor.authenticate_additional_data(bytes(associated_data))
    ciphertext = encryptor.update(plaintext_bytes) + encryptor.finalize()
    return {
        "ciphertext": ciphertext,
        "iv": iv,
        "tag": encryptor.tag,
        "mode": "GCM",
    }


def _decrypt_with_key(
    ciphertext_dict: Mapping[str, bytes],
    aes_key: bytes,
    *,
    cipher_mode: str,
    associated_data: Optional[bytes] = None,
) -> bytes:
    """Decrypt a payload using an existing AES session key."""

    aes_bytes = _ensure_bytes(aes_key, "aes_key")
    mode = _normalise_mode(cipher_mode)

    required_fields = ["ciphertext", "iv"]
    if mode == "GCM":
        required_fields.append("tag")

    for field in required_fields:
        if field not in ciphertext_dict:
            raise ValueError(f"Missing required field: {field}")
        if not isinstance(ciphertext_dict[field], (bytes, bytearray)):
            raise TypeError(f"{field} must be bytes-like")

    ciphertext = bytes(ciphertext_dict["ciphertext"])
    iv = bytes(ciphertext_dict["iv"])

    if mode == "CBC":
        cipher = Cipher(AES(aes_bytes), CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return pkcs7_unpad(padded_plaintext, 16)

    tag = bytes(ciphertext_dict["tag"])
    decryptor = Cipher(AES(aes_bytes), GCM(iv, tag), backend=default_backend()).decryptor()
    if associated_data:
        decryptor.authenticate_additional_data(bytes(associated_data))
    return decryptor.update(ciphertext) + decryptor.finalize()


def _encrypt_session_key(
    aes_key: bytes,
    public_key_pem: bytes,
    *,
    use_pkcs1v15: bool,
) -> bytes:
    """Encrypt the AES session key with the provided RSA public key."""

    public_key = serialization.load_pem_public_key(bytes(public_key_pem), backend=default_backend())
    aes_key_b64 = base64.b64encode(_ensure_bytes(aes_key, "aes_key"))

    if use_pkcs1v15:
        return public_key.encrypt(aes_key_b64, asymmetric_padding.PKCS1v15())

    return public_key.encrypt(
        aes_key_b64,
        asymmetric_padding.OAEP(
            mgf=asymmetric_padding.MGF1(algorithm=SHA256()),
            algorithm=SHA256(),
            label=None,
        ),
    )


def _decrypt_session_key(encrypted_key: bytes, private_key_pem: bytes) -> bytes:
    """Decrypt an RSA-wrapped AES session key."""

    private_key = _load_private_key_cached(bytes(private_key_pem))

    try:
        aes_key_b64 = private_key.decrypt(
            encrypted_key,
            asymmetric_padding.OAEP(
                mgf=asymmetric_padding.MGF1(algorithm=SHA256()),
                algorithm=SHA256(),
                label=None,
            ),
        )
    except Exception:
        aes_key_b64 = private_key.decrypt(
            encrypted_key,
            asymmetric_padding.PKCS1v15(),
        )

    return base64.b64decode(aes_key_b64)


@dataclass(slots=True)
class StreamSession:
    """Hold symmetric context for encrypted streaming payloads."""

    aes_key: bytes
    cipher_mode: str = "CBC"
    associated_data: Optional[bytes] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aes_key", _ensure_bytes(self.aes_key, "aes_key"))
        mode = _normalise_mode(self.cipher_mode)
        object.__setattr__(self, "cipher_mode", mode)
        if self.associated_data is not None:
            object.__setattr__(self, "associated_data", bytes(self.associated_data))

def generate_keys() -> Tuple[bytes, bytes]:
    """
    Generate RSA keys for encryption and decryption.
    Returns private_key_pem, public_key_pem
    """
    try:
        key_size = int(RSA_KEY_SIZE)
    except (TypeError, ValueError) as exc:
        raise ValueError("RSA_KEY_SIZE must be an integer number of bits") from exc

    if key_size < MIN_RSA_KEY_SIZE:
        raise ValueError("RSA_KEY_SIZE must be at least {min_bits} bits for secure operation".format(min_bits=MIN_RSA_KEY_SIZE))
    if key_size % 256 != 0:
        raise ValueError("RSA_KEY_SIZE must be a multiple of 256 bits")

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend()
    )

    # Get private key in PEM format
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    # Get public key in PEM format
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    return private_key_pem, public_key_pem

def encrypt(
    plaintext: bytes,
    public_key_pem: bytes,
    use_pkcs1v15: bool = False,
    cipher_mode: str = "CBC",
    associated_data: Optional[bytes] = None,
) -> Tuple[Dict[str, bytes], bytes, bytes]:
    """
    Encrypt plaintext using AES with a random key, then encrypt that key with RSA.

    Args:
        plaintext: The data to encrypt
        public_key_pem: RSA public key in PEM format
        use_pkcs1v15: Use PKCS#1 v1.5 padding instead of OAEP for RSA key encryption
            (for compatibility with legacy JavaScript clients)
        cipher_mode: Symmetric encryption mode to use. Defaults to "CBC" but also accepts
            "GCM" for authenticated encryption of model weights or inference payloads.
        associated_data: Optional bytes that should be authenticated (but not encrypted)
            alongside the ciphertext when using AES-GCM.

    Returns:
        Tuple (ciphertext_dict, encrypted_key, iv)
        - ciphertext_dict: Dictionary with 'ciphertext' and 'iv' keys (and 'tag' when using GCM)
        - encrypted_key: The AES key encrypted with RSA
        - iv: Initialization vector used for AES-CBC
    """
    if not isinstance(public_key_pem, (bytes, bytearray)):
        raise TypeError("public_key_pem must be bytes-like")

    mode = _normalise_mode(cipher_mode)
    aes_key = secrets.token_bytes(AES_KEY_SIZE)
    ciphertext_dict = _encrypt_with_key(
        plaintext,
        aes_key,
        cipher_mode=mode,
        associated_data=associated_data,
    )
    encrypted_key = _encrypt_session_key(
        aes_key,
        bytes(public_key_pem),
        use_pkcs1v15=use_pkcs1v15,
    )
    iv = ciphertext_dict.get("iv", b"")
    return ciphertext_dict, encrypted_key, iv


def encrypt_stream_chunk(
    plaintext: bytes,
    public_key_pem: bytes,
    *,
    session: Optional[StreamSession] = None,
    use_pkcs1v15: bool = False,
    cipher_mode: str = "CBC",
    associated_data: Optional[bytes] = None,
) -> Tuple[Dict[str, bytes], Optional[bytes], StreamSession]:
    """Encrypt a streaming chunk while maintaining a reusable session key."""

    if session is not None and not isinstance(session, StreamSession):
        raise TypeError("session must be a StreamSession instance")
    if not isinstance(public_key_pem, (bytes, bytearray)):
        raise TypeError("public_key_pem must be bytes-like")
    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data must be bytes-like when provided")

    if session is None:
        mode = _normalise_mode(cipher_mode)
        aes_key = secrets.token_bytes(AES_KEY_SIZE)
        normalized_ad = bytes(associated_data) if isinstance(associated_data, bytearray) else associated_data
        session = StreamSession(aes_key=aes_key, cipher_mode=mode, associated_data=normalized_ad)
        encrypted_key: Optional[bytes] = _encrypt_session_key(
            session.aes_key,
            bytes(public_key_pem),
            use_pkcs1v15=use_pkcs1v15,
        )
    else:
        mode = session.cipher_mode
        normalized_ad = session.associated_data
        if cipher_mode is not None and _normalise_mode(cipher_mode) != mode:
            raise ValueError("cipher_mode mismatch for existing streaming session")
        if associated_data is not None and bytes(associated_data) != (normalized_ad or b""):
            raise ValueError("associated_data mismatch for existing streaming session")
        encrypted_key = None

    ciphertext_dict = _encrypt_with_key(
        plaintext,
        session.aes_key,
        cipher_mode=mode,
        associated_data=normalized_ad,
    )
    return ciphertext_dict, encrypted_key, session


def decrypt_stream_chunk(
    ciphertext_dict: Mapping[str, bytes],
    private_key_pem: bytes,
    *,
    session: Optional[StreamSession] = None,
    encrypted_key: Optional[bytes] = None,
    cipher_mode: Optional[str] = None,
    associated_data: Optional[bytes] = None,
) -> Tuple[bytes, StreamSession]:
    """Decrypt a streaming chunk while keeping the session key alive."""

    if session is not None and not isinstance(session, StreamSession):
        raise TypeError("session must be a StreamSession instance")
    if not isinstance(private_key_pem, (bytes, bytearray)):
        raise TypeError("private_key_pem must be bytes-like")
    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data must be bytes-like when provided")

    if session is None:
        if encrypted_key is None:
            raise ValueError("encrypted_key is required for the first streaming chunk")
        mode = _normalise_mode(cipher_mode or ciphertext_dict.get("mode"))
        normalized_ad = bytes(associated_data) if isinstance(associated_data, bytearray) else associated_data
        aes_key = _decrypt_session_key(bytes(encrypted_key), bytes(private_key_pem))
        session = StreamSession(aes_key=aes_key, cipher_mode=mode, associated_data=normalized_ad)
    else:
        mode = session.cipher_mode
        normalized_ad = session.associated_data
        if cipher_mode is not None and _normalise_mode(cipher_mode) != mode:
            raise ValueError("cipher_mode mismatch for existing streaming session")
        if associated_data is not None and bytes(associated_data) != (normalized_ad or b""):
            raise ValueError("associated_data mismatch for existing streaming session")
        if encrypted_key is not None:
            raise ValueError("encrypted_key should be omitted when reusing a streaming session")

    plaintext = _decrypt_with_key(
        ciphertext_dict,
        session.aes_key,
        cipher_mode=mode,
        associated_data=normalized_ad,
    )
    return plaintext, session

def decrypt(
    ciphertext_dict: Mapping[str, bytes],
    encrypted_key: bytes,
    private_key_pem: bytes,
    cipher_mode: Optional[str] = None,
    associated_data: Optional[bytes] = None,
) -> Optional[bytes]:
    """
    Decrypt ciphertext that was encrypted with the encrypt function.

    Args:
        ciphertext_dict: Dictionary with 'ciphertext' and 'iv'
            (and 'tag' when decrypting AES-GCM payloads)
        encrypted_key: The AES key encrypted with RSA
        private_key_pem: RSA private key in PEM format
        cipher_mode: Optional override for the symmetric mode (defaults to autodetect)
        associated_data: Authenticated data expected when decrypting AES-GCM payloads

    Returns:
        Decrypted plaintext or None if decryption fails

    Raises:
        TypeError: If inputs are not bytes-like or ciphertext_dict is not a mapping.
        ValueError: If required fields are missing from ciphertext_dict.
    """
    if not isinstance(ciphertext_dict, Mapping):
        raise TypeError("ciphertext_dict must be a mapping with ciphertext and iv entries")
    if not isinstance(encrypted_key, (bytes, bytearray)):
        raise TypeError("encrypted_key must be bytes-like")
    if not isinstance(private_key_pem, (bytes, bytearray)):
        raise TypeError("private_key_pem must be bytes-like")
    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data must be bytes-like when provided")

    mode_hint = cipher_mode or ciphertext_dict.get("mode")
    if mode_hint is None and "tag" in ciphertext_dict:
        mode_hint = "GCM"
    mode = _normalise_mode(mode_hint)

    required_fields = ["ciphertext", "iv"]
    if mode == "GCM":
        required_fields.append("tag")
    for field in required_fields:
        if field not in ciphertext_dict:
            raise ValueError(f"Missing required field: {field}")
        if not isinstance(ciphertext_dict[field], (bytes, bytearray)):
            raise TypeError(f"{field} must be bytes-like")

    try:
        aes_key = _decrypt_session_key(bytes(encrypted_key), bytes(private_key_pem))
        plaintext = _decrypt_with_key(
            ciphertext_dict,
            aes_key,
            cipher_mode=mode,
            associated_data=bytes(associated_data) if isinstance(associated_data, bytearray) else associated_data,
        )
        return plaintext
    except Exception:
        # Avoid leaking sensitive details in logs
        logger.warning("Decryption failed")
        return None

def pkcs7_pad(data: bytes, block_size: int) -> bytes:
    """Pad *data* to a multiple of *block_size* using PKCS#7 padding.

    Args:
        data: Input bytes to pad.
        block_size: Size of each block in bytes (1-255).

    Returns:
        Padded byte string whose length is a multiple of *block_size*.

    Raises:
        ValueError: If *block_size* is not between 1 and 255 (inclusive).
        TypeError: If *data* is not bytes-like or *block_size* is not an ``int``.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes-like")
    if not isinstance(block_size, int):
        raise TypeError("block_size must be an integer")
    if block_size <= 0 or block_size > 255:
        raise ValueError("Block size must be between 1 and 255")
    padding_length = block_size - (len(data) % block_size)
    padding = bytes([padding_length] * padding_length)
    return data + padding

def pkcs7_unpad(padded_data: bytes, block_size: int) -> bytes:
    """Remove PKCS#7 padding from *padded_data*.

    Args:
        padded_data: Input bytes to unpad.
        block_size: Size of each block in bytes (1-255).

    Returns:
        Unpadded byte string.

    Raises:
        ValueError: If *block_size* is not between 1 and 255 (inclusive) or padding is invalid.
        TypeError: If *padded_data* is not bytes-like or *block_size* is not an ``int``.
    """
    if not isinstance(padded_data, (bytes, bytearray)):
        raise TypeError("padded_data must be bytes-like")
    if not isinstance(block_size, int):
        raise TypeError("block_size must be an integer")
    if block_size <= 0 or block_size > 255:
        raise ValueError("Block size must be between 1 and 255")
    if not padded_data:
        raise ValueError("Invalid padding")
    if len(padded_data) % block_size != 0:
        raise ValueError("Invalid padding length")
    padding_length = padded_data[-1]
    if padding_length == 0 or padding_length > block_size:
        raise ValueError("Invalid padding")
    if padded_data[-padding_length:] != bytes([padding_length]) * padding_length:
        raise ValueError("Invalid padding")
    return padded_data[:-padding_length]
