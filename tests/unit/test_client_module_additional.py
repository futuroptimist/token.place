import base64
import json
from unittest.mock import MagicMock, patch

import client as client_mod
from encrypt import generate_keys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend


def _write_keys(tmp_path):
    priv_pem, pub_pem = generate_keys()
    priv_file = tmp_path / "client_private.pem"
    pub_file = tmp_path / "client_public.pem"
    priv_file.write_bytes(priv_pem)
    pub_file.write_bytes(pub_pem)
    priv_key = serialization.load_pem_private_key(priv_pem, password=None, backend=default_backend())
    return priv_key, pub_pem, priv_file, pub_file


def test_load_or_generate_existing_keys(tmp_path, monkeypatch):
    priv, pub, priv_file, pub_file = _write_keys(tmp_path)
    monkeypatch.setattr(client_mod, "CLIENT_KEYS_DIR", str(tmp_path))
    monkeypatch.setattr(client_mod, "CLIENT_PRIVATE_KEY_FILE", str(priv_file))
    monkeypatch.setattr(client_mod, "CLIENT_PUBLIC_KEY_FILE", str(pub_file))

    loaded_priv, loaded_pub = client_mod.load_or_generate_client_keys()

    assert loaded_priv.private_numbers() == priv.private_numbers()
    assert loaded_pub == pub


def test_call_chat_completions_encrypted_success(monkeypatch):
    server_key = b"server"
    server_b64 = base64.b64encode(server_key).decode()
    priv, pub = generate_keys()

    with patch.object(client_mod, "encrypt") as mock_enc, \
         patch.object(client_mod, "decrypt") as mock_dec, \
         patch.object(client_mod.requests, "post") as mock_post:
        mock_enc.return_value = ({"ciphertext": b"ct", "iv": b"iv"}, b"ck", b"iv")
        mock_dec.return_value = json.dumps([{"role": "assistant", "content": "ok"}]).encode()
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            "encrypted": True,
            "data": {
                "ciphertext": base64.b64encode(b"respct").decode(),
                "cipherkey": base64.b64encode(b"respkey").decode(),
                "iv": base64.b64encode(b"resviv").decode(),
            },
        })

        result = client_mod.call_chat_completions_encrypted(server_b64, priv, pub)

        assert result[0]["role"] == "assistant"
        mock_enc.assert_called()
        mock_dec.assert_called()
        mock_post.assert_called()


def test_call_chat_completions_encrypted_bad_key():
    result = client_mod.call_chat_completions_encrypted("not-base64", None, None)
    assert result is None
