import json
import pytest
from encrypt import generate_keys, encrypt, decrypt

def test_encrypt_decrypt():
    # Generate RSA key pair
    private_key, public_key = generate_keys()

    # Plaintext data
    plaintext_data = {
        "message": "Hello, World!",
        "timestamp": "2023-05-27T12:34:56Z",
        "user_id": 123
    }

    # Convert plaintext data to bytes
    plaintext_bytes = json.dumps(plaintext_data).encode('utf-8')

    # Encrypt the plaintext
    ciphertext_dict, cipherkey, iv = encrypt(plaintext_bytes, public_key)

    # Assert that the ciphertext and cipherkey are generated
    assert 'iv' in ciphertext_dict
    assert 'ciphertext' in ciphertext_dict
    assert cipherkey is not None
    assert iv is not None

    # Decrypt the ciphertext
    decrypted_bytes = decrypt(ciphertext_dict, cipherkey, private_key)

    # Assert that the decrypted plaintext matches the original plaintext
    assert decrypted_bytes is not None
    decrypted_data = json.loads(decrypted_bytes.decode('utf-8'))
    assert decrypted_data == plaintext_data

def test_encrypt_decrypt_long_plaintext():
    # Generate RSA key pair
    private_key, public_key = generate_keys()

    # Long plaintext data
    plaintext_data = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 100

    # Convert plaintext data to bytes
    plaintext_bytes = plaintext_data.encode('utf-8')

    # Encrypt the plaintext
    ciphertext_dict, cipherkey, iv = encrypt(plaintext_bytes, public_key)

    # Assert that the ciphertext and cipherkey are generated
    assert 'iv' in ciphertext_dict
    assert 'ciphertext' in ciphertext_dict
    assert cipherkey is not None
    assert iv is not None

    # Decrypt the ciphertext
    decrypted_bytes = decrypt(ciphertext_dict, cipherkey, private_key)

    # Assert that the decrypted plaintext matches the original plaintext
    assert decrypted_bytes is not None
    decrypted_data = decrypted_bytes.decode('utf-8')
    assert decrypted_data == plaintext_data

def test_encrypt_decrypt_empty_plaintext():
    # Generate RSA key pair
    private_key, public_key = generate_keys()

    # Empty plaintext data
    plaintext_bytes = b""

    # Encrypt the plaintext
    ciphertext_dict, cipherkey, iv = encrypt(plaintext_bytes, public_key)

    # Assert that the ciphertext and cipherkey are generated
    assert 'iv' in ciphertext_dict
    assert 'ciphertext' in ciphertext_dict
    assert cipherkey is not None
    assert iv is not None

    # Decrypt the ciphertext
    decrypted_bytes = decrypt(ciphertext_dict, cipherkey, private_key)

    # Assert that the decrypted plaintext matches the original plaintext
    assert decrypted_bytes is not None
    assert decrypted_bytes == plaintext_bytes


def test_encrypt_rejects_non_bytes_inputs():
    """encrypt should raise TypeError when given non-bytes arguments."""
    _, public_key = generate_keys()
    with pytest.raises(TypeError, match="plaintext must be bytes-like"):
        encrypt("not-bytes", public_key)
    with pytest.raises(TypeError, match="public_key_pem must be bytes-like"):
        encrypt(b"data", "not-bytes")


def test_decrypt_rejects_non_bytes_inputs():
    """decrypt should raise TypeError on malformed inputs."""
    private_key, public_key = generate_keys()
    ciphertext_dict, cipherkey, _ = encrypt(b"hello", public_key)
    with pytest.raises(TypeError, match="ciphertext_dict must be a dict"):
        decrypt("not-dict", cipherkey, private_key)
    with pytest.raises(TypeError, match="encrypted_key must be bytes-like"):
        decrypt(ciphertext_dict, "not-bytes", private_key)
    with pytest.raises(TypeError, match="private_key_pem must be bytes-like"):
        decrypt(ciphertext_dict, cipherkey, "not-bytes")
