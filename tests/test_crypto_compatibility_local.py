import pytest
import base64
import json
import sys
import os
from pathlib import Path
import subprocess
import tempfile

# Add the project root to the path for imports
project_root = str(Path(__file__).parent.parent)
sys.path.insert(0, project_root)

from encrypt import generate_keys, encrypt, decrypt
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('crypto_tests')


def test_python_encrypt_js_decrypt():
    """
    Test that data encrypted in Python can be decrypted in Node.js JavaScript
    This test doesn't require a browser.
    """
    logger.info("Starting Python encrypt -> JS decrypt test")

    # Generate keys in Python
    private_key, public_key = generate_keys()
    private_key_pem = private_key.decode('utf-8')
    public_key_pem = public_key.decode('utf-8')
    logger.info("Generated RSA keys in Python")

    # Test data to encrypt
    test_data = {
        "message": "Hello from Python!",
        "timestamp": "2023-05-27T12:34:56Z",
        "chat_history": [
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": "The capital of France is Paris."}
        ]
    }

    # Encrypt with Python
    plaintext = json.dumps(test_data).encode('utf-8')
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)
    logger.info(f"Encrypted data in Python, ciphertext size: {len(ciphertext_dict['ciphertext'])} bytes")

    # Convert encrypted data to Base64 strings for JS
    ciphertext_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
    cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')
    iv_b64 = base64.b64encode(iv).decode('utf-8')
    logger.info("Prepared encrypted data for JS (Base64 encoded)")

    # Get absolute path to the js_test_shim.js file
    shim_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'js_test_shim.js'))
    # Convert Windows backslashes to forward slashes for JavaScript
    shim_path_js = shim_path.replace('\\', '/')
    logger.info(f"Using shim at: {shim_path_js}")

    # Create temporary JavaScript file to test decryption
    with tempfile.NamedTemporaryFile(suffix='.js', mode='w', delete=False) as temp_js:
        # Replace Windows backslashes with forward slashes for JavaScript paths
        project_root_js = project_root.replace('\\', '/')

        js_script = f"""
// Load the shim first to create browser environment objects
require('{shim_path_js}');

const crypto = require('crypto');
const cryptoJs = require('{project_root_js}/node_modules/crypto-js');

// Get encrypted data from Python
const encryptedData = {{
    ciphertext: "{ciphertext_b64}",
    cipherkey: "{cipherkey_b64}",
    iv: "{iv_b64}"
}};

// Private key from Python
const privateKeyPem = `{private_key_pem}`;

async function decryptData() {{
    try {{
        const decryptedKeyBuffer = crypto.privateDecrypt({{
            key: privateKeyPem,
            padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
            oaepHash: "sha256"
        }},
            Buffer.from(encryptedData.cipherkey, "base64")
        );
        const decryptedKeyBase64 = decryptedKeyBuffer.toString("utf8");
        const aesKey = cryptoJs.enc.Base64.parse(decryptedKeyBase64);

        // Convert the Base64 IV to a WordArray
        const iv = cryptoJs.enc.Base64.parse(encryptedData.iv);

        // Decrypt the ciphertext with AES
        const decrypted = cryptoJs.AES.decrypt(
            encryptedData.ciphertext,
            aesKey,
            {{
                iv: iv,
                mode: cryptoJs.mode.CBC,
                padding: cryptoJs.pad.Pkcs7
            }}
        );

        // Convert the decrypted WordArray to a string
        const decryptedString = cryptoJs.enc.Utf8.stringify(decrypted);

        console.log(decryptedString);
        return decryptedString;
    }} catch (error) {{
        console.error('Decryption error:', error);
        process.exit(1);
    }}
}}

decryptData();
"""

        temp_js.write(js_script)
        temp_js_path = temp_js.name

    try:
        # Run the Node.js script to decrypt the data
        logger.info(f"Running Node.js script: {temp_js_path}")
        result = subprocess.run(['node', temp_js_path], capture_output=True, text=True, check=True, cwd=project_root)
        js_output = result.stdout.strip()
        logger.info(f"Node.js decryption output: {js_output}")

        # Verify the decrypted data matches the original
        js_decrypted = json.loads(js_output)
        assert js_decrypted == test_data, "Node.js JavaScript decryption produced different result"
        logger.info("Python encrypt -> Node.js JS decrypt: Success!")
        print("Python encrypt -> Node.js JS decrypt: Success!")
    except subprocess.CalledProcessError as e:
        logger.error(f"Node.js script error: {e.stderr}")
        assert False, f"Node.js script error: {e.stderr}"
    finally:
        # Clean up the temporary file
        os.unlink(temp_js_path)

def test_js_encrypt_python_decrypt():
    """
    Test that data encrypted in Node.js JavaScript can be decrypted in Python
    This test doesn't require a browser.
    """
    logger.info("Starting JS encrypt -> Python decrypt test")

    # Test data to encrypt
    test_data = {
        "message": "Hello from JavaScript!",
        "numbers": [1, 2, 3, 4, 5],
        "nested": {
            "value": "Test nested object"
        }
    }
    test_data_json = json.dumps(test_data)

    # Get absolute path to the js_test_shim.js file
    shim_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'js_test_shim.js'))
    # Convert Windows backslashes to forward slashes for JavaScript
    shim_path_js = shim_path.replace('\\', '/')
    logger.info(f"Using shim at: {shim_path_js}")

    # Create temporary JavaScript file to test encryption
    with tempfile.NamedTemporaryFile(suffix='.js', mode='w', delete=False) as temp_js:
        # Replace Windows backslashes with forward slashes for JavaScript paths
        project_root_js = project_root.replace('\\', '/')

        js_script = f"""
// Load the shim first to create browser environment objects
require('{shim_path_js}');

const crypto = require('crypto');
const cryptoJs = require('{project_root_js}/node_modules/crypto-js');

// Test data
const testData = {test_data_json};

async function encryptData() {{
    try {{
        const {{ publicKey, privateKey }} = crypto.generateKeyPairSync('rsa', {{ modulusLength: 2048 }});
        const publicKeyPem = publicKey.export({{ type: 'pkcs1', format: 'pem' }});
        const privateKeyPem = privateKey.export({{ type: 'pkcs1', format: 'pem' }});

        const aesKeyBuf = crypto.randomBytes(32);
        const ivBuf = crypto.randomBytes(16);
        const aesKey = cryptoJs.lib.WordArray.create(aesKeyBuf);
        const iv = cryptoJs.lib.WordArray.create(ivBuf);

        const encrypted = cryptoJs.AES.encrypt(
            JSON.stringify(testData),
            aesKey,
            {{
                iv: iv,
                mode: cryptoJs.mode.CBC,
                padding: cryptoJs.pad.Pkcs7
            }}
        );

        const aesKeyBase64 = aesKeyBuf.toString('base64');
        const encryptedKeyBuffer = crypto.publicEncrypt(
            {{
                key: publicKeyPem,
                padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
                oaepHash: 'sha256'
            }},
            Buffer.from(aesKeyBase64)
        );

        const result = {{
            ciphertext: encrypted.toString(),
            cipherkey: encryptedKeyBuffer.toString('base64'),
            iv: cryptoJs.enc.Base64.stringify(iv),
            publicKey: publicKeyPem,
            privateKey: privateKeyPem
        }};

        console.log(JSON.stringify(result));
    }} catch (error) {{
        console.error('Encryption error:', error);
        process.exit(1);
    }}
}}

encryptData();
"""

        temp_js.write(js_script)
        temp_js_path = temp_js.name

    try:
        # Run the Node.js script to encrypt the data
        logger.info(f"Running Node.js script: {temp_js_path}")
        result = subprocess.run(['node', temp_js_path], capture_output=True, text=True, check=True, cwd=project_root)
        js_output = result.stdout.strip()
        logger.info("Node.js encryption successful")

        # Parse the encrypted data
        encrypted_data = json.loads(js_output)

        # Extract the needed components
        ciphertext_b64 = encrypted_data['ciphertext']
        cipherkey_b64 = encrypted_data['cipherkey']
        iv_b64 = encrypted_data['iv']
        private_key_pem = encrypted_data['privateKey'].encode('utf-8')

        # Convert Base64 strings to bytes for Python
        ciphertext = base64.b64decode(ciphertext_b64)
        cipherkey = base64.b64decode(cipherkey_b64)
        iv = base64.b64decode(iv_b64)

        # Decrypt with Python
        logger.info("Starting Python decryption")
        decrypted_bytes = decrypt({'ciphertext': ciphertext, 'iv': iv}, cipherkey, private_key_pem)

        # Verify the decryption worked
        assert decrypted_bytes is not None, "Python decryption failed"

        # Parse the decrypted data and compare with the original
        decrypted_data = json.loads(decrypted_bytes.decode('utf-8'))
        assert decrypted_data == test_data, "Python decryption produced different result"
        logger.info("JS encrypt -> Python decrypt: Success!")
        print("Node.js JS encrypt -> Python decrypt: Success!")
    except subprocess.CalledProcessError as e:
        logger.error(f"Node.js script error: {e.stderr}")
        assert False, f"Node.js script error: {e.stderr}"
    finally:
        # Clean up the temporary file
        os.unlink(temp_js_path)

if __name__ == "__main__":
    # Run the tests directly when script is executed
    test_python_encrypt_js_decrypt()
    test_js_encrypt_python_decrypt()
