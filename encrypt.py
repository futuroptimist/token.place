from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from os import urandom

def generate_keys():
    """
    Generates an RSA private/public key pair.
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

def encrypt_message_with_public_key(message, pem_public_key):
    """
    Encrypts a message with the recipient's public RSA key.
    """
    public_key = load_pem_public_key(pem_public_key, backend=default_backend())
    encrypted_message = public_key.encrypt(
        message,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return encrypted_message

def decrypt_message_with_private_key(encrypted_message, pem_private_key):
    """
    Decrypts an encrypted message with the recipient's private RSA key.
    """
    private_key = load_pem_private_key(pem_private_key, password=None, backend=default_backend())
    decrypted_message = private_key.decrypt(
        encrypted_message,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return decrypted_message

def encrypt_longer_message_with_aes(message, pem_public_key):
    """
    Encrypts a longer message using AES for the message and RSA for the AES key.
    """
    # Generate a random AES key and IV
    aes_key = urandom(32)  # AES-256 key
    iv = urandom(16)  # AES block size is 128 bits

    # Encrypt the message with AES
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    padded_message = message + b" " * (16 - len(message) % 16)  # PKCS#7 padding
    encrypted_message = encryptor.update(padded_message) + encryptor.finalize()

    # Encrypt the AES key with the recipient's public RSA key
    encrypted_aes_key = encrypt_message_with_public_key(aes_key, pem_public_key)

    return encrypted_aes_key, iv, encrypted_message

def decrypt_aes_encrypted_message(encrypted_aes_key, iv, encrypted_message, pem_private_key):
    """
    Decrypts an AES encrypted message, first decrypting the AES key with RSA.
    """
    aes_key = decrypt_message_with_private_key(encrypted_aes_key, pem_private_key)
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_message = decryptor.update(encrypted_message) + decryptor.finalize()

    # Remove PKCS#7 padding
    unpadded_message = decrypted_message.rstrip(b" ")
    return unpadded_message
