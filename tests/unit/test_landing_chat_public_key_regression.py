from pathlib import Path


def test_landing_chat_normalizes_base64_public_keys_to_pem():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert "normalizeServerPublicKey" in chat_js
    assert "public_key_pem" in chat_js
    assert "-----BEGIN PUBLIC KEY-----" in chat_js
    assert "-----END PUBLIC KEY-----" in chat_js
