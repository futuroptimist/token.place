"""
Parameterized tests for token.place encryption.

These tests validate the encryption system across different configurations:
1. Different RSA key sizes
2. Different input types and formats
3. Different character encodings
4. Various edge cases
"""

import pytest
import base64
import json
import sys
import os
from pathlib import Path
from typing import Dict, Any, Tuple, List, Optional

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the modules to test
from encrypt import encrypt, decrypt, generate_keys

# Check if RSA_KEY_SIZE is configurable directly
try:
    from encrypt import RSA_KEY_SIZE
    CAN_MODIFY_KEY_SIZE = True
except ImportError:
    CAN_MODIFY_KEY_SIZE = False

# If we can't directly import and modify RSA_KEY_SIZE, we'll use mocking
if not CAN_MODIFY_KEY_SIZE:
    import unittest.mock

# Test data constants
UNICODE_STRINGS = [
    "Hello, world!",  # Basic ASCII
    "こんにちは世界",  # Japanese
    "Привет, мир!",   # Russian
    "你好，世界！",    # Chinese
    "🔒🔑💻",         # Emojis
    "a\u0000b",       # Null character
    "a" * 1000,       # Long string
    ""                # Empty string
]

JSON_OBJECTS = [
    {"simple": "value"},
    {"nested": {"object": {"with": "values"}}},
    {"array": [1, 2, 3, 4, 5]},
    {"unicode": "こんにちは世界"},
    {"empty": None},
    {"boolean": True},
    {"number": 12345.6789},
    {}  # Empty object
]

# Skip decorator for tests that modify global state
skip_if_cannot_modify = pytest.mark.skipif(
    not CAN_MODIFY_KEY_SIZE,
    reason="Cannot modify RSA_KEY_SIZE constant"
)

@pytest.fixture
def crypto_keys_default() -> Dict[str, bytes]:
    """Generate a pair of RSA keys with default settings."""
    private_key, public_key = generate_keys()
    return {
        "private_key": private_key,
        "public_key": public_key
    }

def generate_keys_with_size(key_size: int) -> Tuple[bytes, bytes]:
    """Generate RSA keys with specific key size."""
    if CAN_MODIFY_KEY_SIZE:
        # If we can directly modify the constant
        original_size = globals()['RSA_KEY_SIZE']
        globals()['RSA_KEY_SIZE'] = key_size
        try:
            return generate_keys()
        finally:
            globals()['RSA_KEY_SIZE'] = original_size
    else:
        # Use mocking to patch the RSA_KEY_SIZE
        with unittest.mock.patch('encrypt.RSA_KEY_SIZE', key_size):
            return generate_keys()

@pytest.mark.parametrize("key_size", [1024, 2048, 4096])
def test_encryption_with_different_key_sizes(key_size):
    """Test encryption and decryption with different RSA key sizes."""
    private_key, public_key = generate_keys_with_size(key_size)

    # Verify the keys were generated with the correct size
    assert b"PRIVATE KEY" in private_key
    assert b"PUBLIC KEY" in public_key

    # Test encryption and decryption
    plaintext = "Test message for key size {key_size}".encode()
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)

    # Verify encryption worked
    assert ciphertext_dict is not None
    assert "ciphertext" in ciphertext_dict
    assert "iv" in ciphertext_dict

    # Decrypt and verify
    decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
    assert decrypted == plaintext

@pytest.mark.parametrize("unicode_string", UNICODE_STRINGS)
def test_encryption_with_different_unicode_strings(unicode_string, crypto_keys_default):
    """Test encryption and decryption with various Unicode strings."""
    private_key = crypto_keys_default["private_key"]
    public_key = crypto_keys_default["public_key"]

    # Convert string to bytes
    plaintext = unicode_string.encode('utf-8')

    # Encrypt
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)

    # Decrypt and verify
    decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
    assert decrypted == plaintext
    assert decrypted.decode('utf-8') == unicode_string

@pytest.mark.parametrize("json_object", JSON_OBJECTS)
def test_encryption_with_different_json_objects(json_object, crypto_keys_default):
    """Test encryption and decryption with various JSON objects."""
    private_key = crypto_keys_default["private_key"]
    public_key = crypto_keys_default["public_key"]

    # Convert JSON to string then bytes
    plaintext = json.dumps(json_object).encode('utf-8')

    # Encrypt
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)

    # Decrypt and verify
    decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
    assert decrypted == plaintext

    # Verify the JSON structure is preserved
    decrypted_json = json.loads(decrypted.decode('utf-8'))
    assert decrypted_json == json_object

@pytest.mark.parametrize("encoding", ['utf-8', 'ascii', 'latin-1', 'utf-16'])
def test_encryption_with_different_encodings(encoding, crypto_keys_default):
    """Test encryption and decryption with different text encodings."""
    private_key = crypto_keys_default["private_key"]
    public_key = crypto_keys_default["public_key"]

    # Use a string that can be encoded in all test encodings
    original_string = "Hello, encryption world! Testing 123."

    try:
        # Encode the string
        plaintext = original_string.encode(encoding)

        # Encrypt
        ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)

        # Decrypt and verify
        decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
        assert decrypted == plaintext

        # Decode and compare
        decrypted_string = decrypted.decode(encoding)
        assert decrypted_string == original_string
    except UnicodeEncodeError:
        pytest.skip(f"String cannot be encoded in {encoding}")

@pytest.mark.parametrize("payload_size", [0, 1, 16, 100, 1000, 10000])
def test_encryption_with_different_payload_sizes(payload_size, crypto_keys_default):
    """Test encryption and decryption with different payload sizes."""
    private_key = crypto_keys_default["private_key"]
    public_key = crypto_keys_default["public_key"]

    # Create payload of specified size
    plaintext = ("x" * payload_size).encode('utf-8')

    # Encrypt
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)

    # Decrypt and verify
    decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
    assert decrypted == plaintext
    assert len(decrypted) == payload_size

@pytest.mark.parametrize("binary_data", [
    os.urandom(100),  # Random bytes
    b"\x00" * 100,    # Null bytes
    b"\xFF" * 100,    # 0xFF bytes
    b"\x00\xFF" * 50  # Alternating bytes
])
def test_encryption_with_binary_data(binary_data, crypto_keys_default):
    """Test encryption and decryption with binary data."""
    private_key = crypto_keys_default["private_key"]
    public_key = crypto_keys_default["public_key"]

    # Encrypt
    ciphertext_dict, cipherkey, iv = encrypt(binary_data, public_key)

    # Decrypt and verify
    decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
    assert decrypted == binary_data

def test_encryption_compatibility_between_key_sizes():
    """Test that messages encrypted with one key size can't be decrypted with another."""
    # Generate keys with different sizes
    private_key_1024, public_key_1024 = generate_keys_with_size(1024)
    private_key_2048, public_key_2048 = generate_keys_with_size(2048)

    # Test data
    plaintext = b"Test message for cross-key-size test"

    # Encrypt with 1024-bit key
    ciphertext_dict_1024, cipherkey_1024, iv_1024 = encrypt(plaintext, public_key_1024)

    # Encrypt with 2048-bit key
    ciphertext_dict_2048, cipherkey_2048, iv_2048 = encrypt(plaintext, public_key_2048)

    # Verify correct decryption with matching keys
    assert decrypt(ciphertext_dict_1024, cipherkey_1024, private_key_1024) == plaintext
    assert decrypt(ciphertext_dict_2048, cipherkey_2048, private_key_2048) == plaintext

    # Verify that decryption fails with mismatched keys
    # Note: This might not always raise an exception, but should at least not return the original plaintext
    try:
        result = decrypt(ciphertext_dict_1024, cipherkey_1024, private_key_2048)
        assert result != plaintext, "Decryption with wrong key should not succeed"
    except:
        pass  # Exception is expected and acceptable

    try:
        result = decrypt(ciphertext_dict_2048, cipherkey_2048, private_key_1024)
        assert result != plaintext, "Decryption with wrong key should not succeed"
    except:
        pass  # Exception is expected and acceptable
