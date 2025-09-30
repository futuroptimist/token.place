"""Cross-browser crypto compatibility tests for token.place."""

import base64
import json
import pathlib

import pytest

from encrypt import generate_keys, encrypt


@pytest.mark.crypto
@pytest.mark.browser
@pytest.mark.parametrize("payload_text", [
    "Cross-browser crypto works!",
])
def test_python_encrypt_js_decrypt_across_browsers(browser_matrix, payload_text):
    """Ensure the JS crypto runner decrypts Python ciphertext in all supported browsers."""
    browser_name, page = browser_matrix

    private_key, public_key = generate_keys()

    message = {
        "message": payload_text,
        "browser": browser_name,
        "numbers": [1, 2, 3],
    }

    ciphertext, encrypted_key, iv = encrypt(
        json.dumps(message).encode("utf-8"),
        public_key,
        use_pkcs1v15=True,
    )

    args = {
        "ciphertext_b64": base64.b64encode(ciphertext["ciphertext"]).decode("utf-8"),
        "encryptedKey_b64": base64.b64encode(encrypted_key).decode("utf-8"),
        "iv_b64": base64.b64encode(iv).decode("utf-8"),
        "privateKeyPem": private_key.decode("utf-8"),
    }

    runner_url = pathlib.Path("tests/crypto_runner.html").resolve().as_uri()
    page.goto(runner_url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("typeof window.JSEncrypt === 'function'")
    page.wait_for_function(
        "typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'",
    )

    decrypted = page.evaluate(
        """
        (args) => {
            const { ciphertext_b64, encryptedKey_b64, iv_b64, privateKeyPem } = args;
            const jsEncrypt = new window.JSEncrypt();
            jsEncrypt.setPrivateKey(privateKeyPem);
            const decryptedKey_b64 = jsEncrypt.decrypt(encryptedKey_b64);
            if (!decryptedKey_b64) {
                throw new Error('RSA key unwrap failed');
            }
            const aesKey = CryptoJS.enc.Base64.parse(decryptedKey_b64);
            const iv = CryptoJS.enc.Base64.parse(iv_b64);
            const ciphertext = CryptoJS.enc.Base64.parse(ciphertext_b64);
            const decrypted = CryptoJS.AES.decrypt(
                { ciphertext },
                aesKey,
                { iv, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 },
            );
            return CryptoJS.enc.Utf8.stringify(decrypted);
        }
        """,
        args,
    )

    assert json.loads(decrypted) == message
