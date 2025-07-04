from encrypt import generate_keys, encrypt, decrypt


def test_decrypt_pkcs1v15_fallback():
    priv, pub = generate_keys()
    message = b"fallback test"
    ciphertext_dict, cipherkey, _ = encrypt(message, pub, use_pkcs1v15=True)
    decrypted = decrypt(ciphertext_dict, cipherkey, priv)
    assert decrypted == message
