import base64
import json
from unittest.mock import patch

from api.v1.encryption import EncryptionManager
from encrypt import generate_keys, encrypt, decrypt


def test_encrypt_message_roundtrip():
    manager = EncryptionManager()
    client_priv, client_pub = generate_keys()
    data = {"hello": "world"}

    enc = manager.encrypt_message(data, base64.b64encode(client_pub).decode())
    assert enc and enc["encrypted"] is True

    ciphertext = base64.b64decode(enc["ciphertext"])
    cipherkey = base64.b64decode(enc["cipherkey"])
    iv = base64.b64decode(enc["iv"])
    plaintext = decrypt({"ciphertext": ciphertext, "iv": iv}, cipherkey, client_priv)
    assert json.loads(plaintext.decode()) == data


def test_decrypt_message_success():
    manager = EncryptionManager()
    data = {"foo": 1}
    ciphertext_dict, cipherkey, iv = encrypt(json.dumps(data).encode(), manager._public_key_pem)
    res = manager.decrypt_message({"ciphertext": ciphertext_dict["ciphertext"], "iv": iv}, cipherkey)
    assert res and json.loads(res.decode()) == data


def test_encrypt_message_error(monkeypatch):
    manager = EncryptionManager()
    monkeypatch.setattr("api.v1.encryption.encrypt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = manager.encrypt_message({"x": 1}, manager.public_key_b64)
    assert out is None


def test_decrypt_message_error(monkeypatch):
    manager = EncryptionManager()
    monkeypatch.setattr("api.v1.encryption.decrypt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = manager.decrypt_message({"ciphertext": b"x", "iv": b"y"}, b"k")
    assert out is None
