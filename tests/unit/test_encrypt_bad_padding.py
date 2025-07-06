from encrypt import generate_keys, encrypt, decrypt
import encrypt as enc


def test_decrypt_bad_padding(monkeypatch):
    priv, pub = generate_keys()
    cipher, key, iv = encrypt(b'hi', pub)
    enc_dict = {'ciphertext': cipher['ciphertext'], 'iv': iv}
    def boom(*a, **k):
        raise ValueError('bad padding')
    monkeypatch.setattr(enc, 'pkcs7_unpad', boom)
    out = decrypt(enc_dict, key, priv)
    assert out is None
