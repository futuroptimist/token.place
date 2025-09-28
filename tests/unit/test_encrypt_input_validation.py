"""Negative input validation tests for the low-level decrypt helper."""
import pytest

from encrypt import decrypt, encrypt, generate_keys


def _generate_encrypted_payload():
    """Helper to produce a valid ciphertext payload."""
    private_key, public_key = generate_keys()
    ciphertext_dict, cipherkey, _ = encrypt(b"hello", public_key)
    return private_key, ciphertext_dict, cipherkey


def test_decrypt_missing_iv_raises_value_error():
    """decrypt should raise ValueError when required fields are absent."""
    private_key, ciphertext_dict, cipherkey = _generate_encrypted_payload()
    bad_payload = {"ciphertext": ciphertext_dict["ciphertext"]}

    with pytest.raises(ValueError, match="Missing required field: iv"):
        decrypt(bad_payload, cipherkey, private_key)


def test_decrypt_rejects_non_mapping_ciphertext_dict():
    """decrypt should require ciphertext_dict to behave like a mapping."""
    private_key, _, cipherkey = _generate_encrypted_payload()

    with pytest.raises(TypeError, match="ciphertext_dict must be a mapping"):
        decrypt(None, cipherkey, private_key)


def test_decrypt_rejects_non_bytes_payloads():
    """decrypt should reject ciphertext or IV values that are not bytes-like."""
    private_key, ciphertext_dict, cipherkey = _generate_encrypted_payload()
    bad_payload = {
        "ciphertext": "not-bytes",
        "iv": ciphertext_dict["iv"],
    }

    with pytest.raises(TypeError, match="ciphertext must be bytes-like"):
        decrypt(bad_payload, cipherkey, private_key)
