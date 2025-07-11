# Made with help from https://www.youtube.com/watch?v=pOx2TYwR590
# Github: https://github.com/cgossi/fundamental_cryptography_with_python

import os
import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asymmetric_padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key, load_pem_private_key
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import CBC
from cryptography.hazmat.primitives.hashes import SHA256
import secrets
from typing import Tuple, Dict, Optional

# Import config, but use fallback values if import fails
# This allows encrypt.py to work standalone but also in the larger application
try:
    from config import RSA_KEY_SIZE, AES_KEY_SIZE, IV_SIZE
except ImportError:
    # Default values if config.py is not available
    RSA_KEY_SIZE = 2048
    AES_KEY_SIZE = 32  # 256 bits
    IV_SIZE = 16  # 128 bits

def generate_keys() -> Tuple[bytes, bytes]:
    """
    Generate RSA keys for encryption and decryption.
    Returns private_key_pem, public_key_pem
    """
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=RSA_KEY_SIZE,
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

def encrypt(plaintext: bytes, public_key_pem: bytes, use_pkcs1v15: bool = False) -> Tuple[Dict[str, bytes], bytes, bytes]:
    """
    Encrypt plaintext using AES-CBC with a random key, then encrypt that key with RSA.
    
    Args:
        plaintext: The data to encrypt
        public_key_pem: RSA public key in PEM format
        
    Returns:
        Tuple (ciphertext_dict, encrypted_key, iv)
        - ciphertext_dict: Dictionary with 'ciphertext' and 'iv' keys
        - encrypted_key: The AES key encrypted with RSA
        - iv: Initialization vector used for AES-CBC
    """
    # Generate a random AES key
    aes_key = secrets.token_bytes(AES_KEY_SIZE)  # 256-bit key
    
    # Generate a random IV
    iv = secrets.token_bytes(IV_SIZE)  # 128-bit IV for AES
    
    # Encrypt the plaintext with AES-CBC
    cipher = Cipher(AES(aes_key), CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    
    # We need to pad the plaintext to a multiple of 16 bytes
    padded_data = pkcs7_pad(plaintext, 16)
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    
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
    
    return {'ciphertext': ciphertext, 'iv': iv}, encrypted_key, iv

def decrypt(ciphertext_dict: Dict[str, bytes], encrypted_key: bytes, private_key_pem: bytes) -> Optional[bytes]:
    """
    Decrypt ciphertext that was encrypted with the encrypt function.
    
    Args:
        ciphertext_dict: Dictionary with 'ciphertext' and 'iv'
        encrypted_key: The AES key encrypted with RSA
        private_key_pem: RSA private key in PEM format
        
    Returns:
        Decrypted plaintext or None if decryption fails
    """
    try:
        # First decrypt the AES key using RSA
        private_key = serialization.load_pem_private_key(
            private_key_pem,
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
        
        # Get the ciphertext and IV
        ciphertext = ciphertext_dict['ciphertext']
        iv = ciphertext_dict['iv']
        
        # Decrypt the ciphertext using AES-CBC
        cipher = Cipher(AES(aes_key), CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        
        # Remove padding
        plaintext = pkcs7_unpad(padded_plaintext, 16)
        return plaintext
    except Exception as e:
        print(f"Decryption error: {e}")
        return None

def pkcs7_pad(data: bytes, block_size: int) -> bytes:
    """
    Pad data using PKCS#7 padding scheme
    """
    padding_length = block_size - (len(data) % block_size)
    padding = bytes([padding_length] * padding_length)
    return data + padding

def pkcs7_unpad(padded_data: bytes, block_size: int) -> bytes:
    """
    Remove PKCS#7 padding
    """
    padding_length = padded_data[-1]
    if padding_length > block_size:
        raise ValueError("Invalid padding")
    # Verify padding
    for i in range(1, padding_length + 1):
        if padded_data[-i] != padding_length:
            raise ValueError("Invalid padding")
    return padded_data[:-padding_length]
