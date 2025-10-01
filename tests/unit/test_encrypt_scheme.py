from unittest.mock import MagicMock, patch

import pytest

from encrypt import encrypt, decrypt, generate_keys
from cryptography.hazmat.primitives.asymmetric import padding


def test_encrypt_uses_oaep():
    plaintext = b'data'
    public_key = MagicMock()
    with patch('encrypt.serialization.load_pem_public_key', return_value=public_key):
        encrypt(plaintext, b'pub')
        args, kwargs = public_key.encrypt.call_args
        assert isinstance(args[1], padding.OAEP)


def test_decrypt_uses_oaep():
    private_key = MagicMock()
    ciphertext = {"ciphertext": b'c', "iv": b'i'}
    with patch('encrypt.serialization.load_pem_private_key', return_value=private_key):
        private_key.decrypt.return_value = b'ZGRh'  # base64 of fake key
        result = decrypt(ciphertext, b'enc', b'priv')
        args, kwargs = private_key.decrypt.call_args
        assert isinstance(args[1], padding.OAEP)


def test_encrypt_decrypt_supports_aes_gcm_roundtrip():
    private_key, public_key = generate_keys()
    associated_data = b"model-weights"

    ciphertext_dict, encrypted_key, _ = encrypt(
        b"sensitive inference payload",
        public_key,
        cipher_mode="GCM",
        associated_data=associated_data,
    )

    assert ciphertext_dict.get("mode") == "GCM"
    assert "tag" in ciphertext_dict

    decrypted = decrypt(
        ciphertext_dict,
        encrypted_key,
        private_key,
        associated_data=associated_data,
    )

    assert decrypted == b"sensitive inference payload"


def test_encrypt_gcm_rejects_non_bytes_associated_data():
    _, public_key = generate_keys()

    with pytest.raises(TypeError):
        encrypt(
            b"payload",
            public_key,
            cipher_mode="GCM",
            associated_data="not-bytes",
        )


def test_decrypt_gcm_rejects_non_bytes_associated_data():
    ciphertext = {"ciphertext": b"c", "iv": b"i", "tag": b"t"}

    with pytest.raises(TypeError):
        decrypt(
            ciphertext,
            b"enc",
            b"priv",
            associated_data="not-bytes",
            cipher_mode="GCM",
        )


def test_decrypt_auto_detects_gcm_mode_from_tag():
    private_key, public_key = generate_keys()

    ciphertext_dict, encrypted_key, _ = encrypt(
        b"auto-detect",
        public_key,
        cipher_mode="GCM",
    )

    ciphertext_dict.pop("mode", None)

    decrypted = decrypt(
        ciphertext_dict,
        encrypted_key,
        private_key,
    )

    assert decrypted == b"auto-detect"
