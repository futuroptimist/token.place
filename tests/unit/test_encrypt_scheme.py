from unittest.mock import MagicMock, patch
from encrypt import encrypt, decrypt
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
