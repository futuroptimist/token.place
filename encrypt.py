# Made with help from https://www.youtube.com/watch?v=pOx2TYwR590
# Github: https://github.com/cgossi/fundamental_cryptography_with_python

import os
import base64
import logging
from collections.abc import Mapping
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asymmetric_padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key, load_pem_private_key
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import CBC, GCM
from cryptography.hazmat.primitives.hashes import SHA256
import secrets
from typing import Tuple, Dict, Optional

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
    # Generate a random AES key
    aes_key = secrets.token_bytes(AES_KEY_SIZE)  # 256-bit key

    mode = cipher_mode.upper()

    if mode == "CBC":
        iv = secrets.token_bytes(IV_SIZE)  # 128-bit IV for AES-CBC
        cipher = Cipher(AES(aes_key), CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        padded_data = pkcs7_pad(plaintext, 16)
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        ciphertext_dict: Dict[str, bytes] = {"ciphertext": ciphertext, "iv": iv}
    elif mode == "GCM":
        iv = secrets.token_bytes(GCM_IV_SIZE)
        encryptor = Cipher(AES(aes_key), GCM(iv), backend=default_backend()).encryptor()
        if associated_data:
            if not isinstance(associated_data, (bytes, bytearray)):
                raise TypeError("associated_data must be bytes-like when provided")
            encryptor.authenticate_additional_data(bytes(associated_data))
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()
        ciphertext_dict = {
            "ciphertext": ciphertext,
            "iv": iv,
            "tag": encryptor.tag,
            "mode": "GCM",
        }
    else:
        raise ValueError(f"Unsupported cipher_mode: {cipher_mode}")

    # Now encrypt the AES key with RSA
    public_key = serialization.load_pem_public_key(
        public_key_pem,
        backend=default_backend()
    )

    # For compatibility with JSEncrypt, convert the AES key to Base64 first
    aes_key_b64 = base64.b64encode(aes_key)

    # Encrypt the Base64 representation of the key
    if use_pkcs1v15:
        encrypted_key = public_key.encrypt(
            aes_key_b64,
            asymmetric_padding.PKCS1v15()
        )
    else:
        encrypted_key = public_key.encrypt(
            aes_key_b64,
            asymmetric_padding.OAEP(
                mgf=asymmetric_padding.MGF1(algorithm=SHA256()),
                algorithm=SHA256(),
                label=None,
            )
        )

    return ciphertext_dict, encrypted_key, iv

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

    mode_hint = cipher_mode or ciphertext_dict.get("mode")
    if mode_hint is None and "tag" in ciphertext_dict:
        mode_hint = "GCM"
    mode = (mode_hint or "CBC").upper()

    required_fields = ["ciphertext", "iv"]
    if mode == "GCM":
        required_fields.append("tag")
    for field in required_fields:
        if field not in ciphertext_dict:
            raise ValueError(f"Missing required field: {field}")
        value = ciphertext_dict[field]
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError(f"{field} must be bytes-like")

    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data must be bytes-like when provided")

    if not isinstance(encrypted_key, (bytes, bytearray)):
        raise TypeError("encrypted_key must be bytes-like")
    if not isinstance(private_key_pem, (bytes, bytearray)):
        raise TypeError("private_key_pem must be bytes-like")

    try:
        # First decrypt the AES key using RSA
        private_key = serialization.load_pem_private_key(
            bytes(private_key_pem),
            password=None,
            backend=default_backend()
        )

        # Decrypt the encrypted AES key. Try OAEP first, then fall back to PKCS1v15 for JS compatibility
        try:
            aes_key_b64 = private_key.decrypt(
                encrypted_key,
                asymmetric_padding.OAEP(
                    mgf=asymmetric_padding.MGF1(algorithm=SHA256()),
                    algorithm=SHA256(),
                    label=None,
                )
            )
        except Exception:
            aes_key_b64 = private_key.decrypt(
                encrypted_key,
                asymmetric_padding.PKCS1v15()
            )

        # Decode the Base64 to get the actual AES key
        aes_key = base64.b64decode(aes_key_b64)

        ciphertext = bytes(ciphertext_dict['ciphertext'])
        iv = bytes(ciphertext_dict['iv'])

        if mode == "CBC":
            cipher = Cipher(AES(aes_key), CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
            plaintext = pkcs7_unpad(padded_plaintext, 16)
            return plaintext

        if mode != "GCM":
            raise ValueError(f"Unsupported cipher_mode: {mode}")

        tag = bytes(ciphertext_dict['tag'])
        decryptor = Cipher(AES(aes_key), GCM(iv, tag), backend=default_backend()).decryptor()
        if associated_data:
            decryptor.authenticate_additional_data(bytes(associated_data))
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
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
