"""Cross-browser Playwright tests for token.place crypto compatibility."""

import base64
import json
from pathlib import Path

import pytest

from encrypt import generate_keys, encrypt

pytestmark = pytest.mark.browser

BROWSERS = ("chromium", "firefox", "webkit")


@pytest.mark.parametrize("target_browser", BROWSERS)
def test_python_encrypt_js_decrypt_cross_browser(playwright, target_browser, web_server):
    """Verify browsers can decrypt payloads encrypted by Python."""
    browser_type = getattr(playwright, target_browser, None)
    if browser_type is None:
        pytest.skip(f"Playwright is missing browser type: {target_browser}")

    try:
        browser = browser_type.launch()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Unable to launch {target_browser}: {exc}")

    page = browser.new_page()

    try:
        private_key, public_key = generate_keys()
        payload = {
            "message": f"Hello from Python on {target_browser}!",
            "path": str(Path("tests/crypto_runner.html").resolve()),
        }
        plaintext = json.dumps(payload).encode("utf-8")
        ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key, use_pkcs1v15=True)

        ciphertext_b64 = base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8")
        cipherkey_b64 = base64.b64encode(cipherkey).decode("utf-8")
        iv_b64 = base64.b64encode(iv).decode("utf-8")
        private_key_pem = private_key.decode("utf-8")

        page.goto(f"{web_server}/tests/crypto_runner.html")
        page.wait_for_load_state("networkidle")
        page.wait_for_function("typeof window.JSEncrypt === 'function'", timeout=60000)
        page.wait_for_function(
            "typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'",
            timeout=60000,
        )

        decrypted = page.evaluate(
            """
            (args) => {
                const { ciphertext_b64, encryptedKey_b64, iv_b64, privateKeyPem } = args;
                const jsEncrypt = new window.JSEncrypt();
                jsEncrypt.setPrivateKey(privateKeyPem);
                const decryptedKey_b64 = jsEncrypt.decrypt(encryptedKey_b64);
                if (!decryptedKey_b64) {
                    throw new Error('RSA decryption failed in browser');
                }
                const aesKey = CryptoJS.enc.Base64.parse(decryptedKey_b64);
                const iv = CryptoJS.enc.Base64.parse(iv_b64);
                const ciphertext = CryptoJS.enc.Base64.parse(ciphertext_b64);
                const result = CryptoJS.AES.decrypt(
                    { ciphertext },
                    aesKey,
                    { iv, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 }
                );
                return CryptoJS.enc.Utf8.stringify(result);
            }
            """,
            {
                "ciphertext_b64": ciphertext_b64,
                "encryptedKey_b64": cipherkey_b64,
                "iv_b64": iv_b64,
                "privateKeyPem": private_key_pem,
            },
        )

        assert json.loads(decrypted) == payload
    finally:
        browser.close()
