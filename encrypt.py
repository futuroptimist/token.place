# Made with help from https://www.youtube.com/watch?v=pOx2TYwR590
# Github: https://github.com/cgossi/fundamental_cryptography_with_python

import os
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asymmetric_padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key, load_pem_private_key
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import CBC
from cryptography.hazmat.primitives.hashes import SHA256

def generate_keys():
    """
    Generates an RSA private/public key pair.

    Returns:
        pem_private_key (bytes): The private key in PEM format.
        pem_public_key (bytes): The public key in PEM format.
    """

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    public_key = private_key.public_key()

    # Return the keys in PEM format
    pem_private_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    pem_public_key = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    return pem_private_key, pem_public_key

def encrypt(plaintext, public_key_bytes):
    """
    Encrypt the plaintext using the provided public key. RSA encryption is used to share the
    AES key and IV, and AES encryption is used to encrypt the plaintext.

    Args:
        plaintext (bytes): The plaintext to encrypt.
        public_key_bytes (bytes): The public key in PEM format.

    Returns:
        ciphertext (Dict): A dictionary containing the IV, and ciphertext.
        cipherkey (bytes): The AES key encrypted with the public key.
    """

    public_key = load_pem_public_key(public_key_bytes)

    pkcs7_padder = padding.PKCS7(AES.block_size).padder()
    padded_plaintext = pkcs7_padder.update(plaintext) + pkcs7_padder.finalize()

    # Generate new random AES-256 key
    key = os.urandom(256 // 8)

    # Generate new random 128 IV required for CBC mode
    iv = os.urandom(128 // 8)

    # AES CBC Cipher
    aes_cbc_cipher = Cipher(AES(key), CBC(iv))

    # Encrypt padded plaintext
    ciphertext = aes_cbc_cipher.encryptor().update(padded_plaintext)

    # Encrypt AES key
    oaep_padding = asymmetric_padding.OAEP(mgf=asymmetric_padding.MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None)
    cipherkey = public_key.encrypt(key, oaep_padding)

    return {'iv': iv, 'ciphertext': ciphertext}, cipherkey

def decrypt(ciphertext, cipherkey, private_key_bytes):
    """
    Decrypt the ciphertext using the provided cipherkey and private key.

    Args:
        ciphertext (Dict): A dictionary containing the IV, and ciphertext.
        cipherkey (bytes): The AES key encrypted with the public key.
        private_key_bytes (bytes): The private key in PEM format.

    Returns:
        plaintext (bytes): The decrypted plaintext.
    """

    try:
        private_key = load_pem_private_key(private_key_bytes, password=None)

        # Decrypt AES key
        oaep_padding = asymmetric_padding.OAEP(mgf=asymmetric_padding.MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None)
        key = private_key.decrypt(cipherkey, oaep_padding)

        # AES CBC Cipher
        aes_cbc_cipher = Cipher(AES(key), CBC(ciphertext['iv']))

        # Decrypt ciphertext
        padded_plaintext = aes_cbc_cipher.decryptor().update(ciphertext['ciphertext'])

        # Remove padding
        pkcs7_unpadder = padding.PKCS7(AES.block_size).unpadder()
        plaintext = pkcs7_unpadder.update(padded_plaintext) + pkcs7_unpadder.finalize()

        return plaintext
    except Exception as e:
        print(f"Exception during decryption: {e}")
        return None