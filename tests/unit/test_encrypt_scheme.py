from unittest.mock import MagicMock, patch
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
