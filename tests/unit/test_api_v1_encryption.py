import base64
import json

from api.v1.encryption import (
    _normalize_client_public_key_bytes,
    encryption_manager,
)
from encrypt import decrypt, generate_keys


def test_normalize_client_public_key_base64_body_to_pem():
    _, public_key = generate_keys()
    public_key_body = "".join(public_key.decode("utf-8").splitlines()[1:-1])

    normalized = _normalize_client_public_key_bytes(public_key_body)

    normalized_text = normalized.decode("utf-8")
    assert normalized_text.startswith("-----BEGIN PUBLIC KEY-----")
    assert normalized_text.endswith("-----END PUBLIC KEY-----\n")


def test_encrypt_message_accepts_base64_body_public_key():
    private_key, public_key = generate_keys()
    public_key_body = "".join(public_key.decode("utf-8").splitlines()[1:-1])

    encrypted = encryption_manager.encrypt_message({"ok": True}, public_key_body)

    assert encrypted is not None
    ciphertext = base64.b64decode(encrypted["ciphertext"])
    cipherkey = base64.b64decode(encrypted["cipherkey"])
    iv = base64.b64decode(encrypted["iv"])
    decrypted = decrypt({"ciphertext": ciphertext, "iv": iv}, cipherkey, private_key)
    assert json.loads(decrypted.decode("utf-8"))["ok"] is True
