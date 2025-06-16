import pytest
import base64
import json
import sys
import os
import subprocess
import time
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from encrypt import generate_keys, encrypt, decrypt
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('crypto_tests')

jsencrypt_path = Path(__file__).resolve().parent.parent / 'node_modules' / 'jsencrypt'
if not jsencrypt_path.exists():
    pytest.skip("jsencrypt module not available", allow_module_level=True)

# Playwright fixture with web server configuration
@pytest.fixture(scope="module")
def browser_context_args(browser_context_args):
    """Modify the browser context arguments to include webServer configuration."""
    return {
        **browser_context_args,
        "ignore_https_errors": True,
    }

# Configure the web server fixture
@pytest.fixture(scope="module")
def web_server():
    """Start a web server for testing."""
    import socket
    
    # Find an available port
    def find_available_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', 0))
            return s.getsockname()[1]
    
    port = find_available_port()
    
    # Get the current working directory
    cwd = Path.cwd()
    
    # Start a HTTP server in a subprocess
    server_cmd = [sys.executable, "-m", "http.server", str(port)]
    server_process = subprocess.Popen(
        server_cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
        text=True
    )
    
    # Wait for the server to start
    server_url = f"http://localhost:{port}"
    logger.info(f"Started web server at {server_url}")
    
    # Wait a moment for the server to initialize
    time.sleep(2)
    
    # Yield the server URL for tests to use
    yield server_url
    
    # Shutdown the server after tests
    logger.info("Stopping web server")
    server_process.terminate()
    try:
        server_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_process.kill()

def test_python_encrypt_js_decrypt(page, web_server):
    """
    Test that data encrypted in Python can be decrypted in browser JavaScript
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
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key, use_pkcs1v15=True)
    logger.info(f"Encrypted data in Python, ciphertext size: {len(ciphertext_dict['ciphertext'])} bytes")
    
    # Convert encrypted data to Base64 strings for JS
    ciphertext_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
    cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8') 
    iv_b64 = base64.b64encode(iv).decode('utf-8')
    logger.info("Prepared encrypted data for JS (Base64 encoded)")
    
    # Define the URL for the crypto runner
    crypto_runner_url = f"{web_server}/tests/crypto_runner.html"
    logger.info(f"Using crypto runner at: {crypto_runner_url}")
    
    # Navigate to the page and wait for it to load
    page.goto(crypto_runner_url)
    page.wait_for_load_state("networkidle")
    
    # Wait for crypto libraries to be loaded
    page.wait_for_function("typeof window.JSEncrypt === 'function'", timeout=60000)
    page.wait_for_function("typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'", timeout=60000)
    logger.info("Page loaded and JS libraries ready")

    # Define the arguments to pass to page.evaluate
    js_args = {
        "ciphertext_b64": ciphertext_b64,
        "encryptedKey_b64": cipherkey_b64,
        "iv_b64": iv_b64,
        "privateKeyPem": private_key_pem
    }

    logger.info("Executing browser decryption logic")
    # Execute browser decryption within the page's context
    js_output = page.evaluate(""" 
    (args) => { 
        // This code runs in the browser 
        const { ciphertext_b64, encryptedKey_b64, iv_b64, privateKeyPem } = args;
        
        try {
            console.log('[TEST] Starting browser decryption');
            
            // Set up the private key for decryption
            const jsEncrypt = new window.JSEncrypt();
            jsEncrypt.setPrivateKey(privateKeyPem);
            console.log('[TEST] JSEncrypt initialized with private key');

            // Decrypt the AES key with RSA
            const decryptedKey_b64 = jsEncrypt.decrypt(encryptedKey_b64);
            console.log('[TEST] Output of jsEncrypt.decrypt (Base64 key expected):', decryptedKey_b64);

            if (!decryptedKey_b64) {
                throw new Error('RSA decryption of AES key failed in browser');
            }

            // Convert the Base64 decrypted key to a WordArray for CryptoJS
            const aesKey = CryptoJS.enc.Base64.parse(decryptedKey_b64);
            console.log('[TEST] Successfully parsed Base64 key to WordArray, sigBytes:', aesKey.sigBytes);

            // Convert the Base64 IV to a WordArray
            const iv = CryptoJS.enc.Base64.parse(iv_b64);
            console.log('[TEST] Parsed IV WordArray, sigBytes:', iv.sigBytes);
            
            // Convert the Base64 ciphertext to a WordArray
            const ciphertextWordArray = CryptoJS.enc.Base64.parse(ciphertext_b64);
            console.log('[TEST] Parsed ciphertext WordArray, sigBytes:', ciphertextWordArray.sigBytes);
            
            // Decrypt the ciphertext with AES
            console.log('[TEST] Starting AES decryption');
            const decrypted = CryptoJS.AES.decrypt(
                { ciphertext: ciphertextWordArray },
                aesKey,
                {
                    iv: iv,
                    mode: CryptoJS.mode.CBC,
                    padding: CryptoJS.pad.Pkcs7
                }
            );
            console.log('[TEST] AES decryption complete, sigBytes:', decrypted.sigBytes);
            
            // Convert the decrypted WordArray to a string
            const decryptedString = CryptoJS.enc.Utf8.stringify(decrypted);
            console.log('[TEST] Decrypted string length:', decryptedString.length);
            
            console.log('[TEST] Decryption successful!');
            return decryptedString; // Return the result to Python
        } catch (error) {
            console.error('[TEST] Browser Decryption error:', error);
            return { error: error.message || 'Unknown browser decryption error' }; 
        }
    }
    """, js_args)
    
    # Check if JS returned an error object
    if isinstance(js_output, dict) and 'error' in js_output:
        error_msg = f"Browser JavaScript decryption failed: {js_output['error']}"
        logger.error(error_msg)
        pytest.fail(error_msg)
    
    # Verify the decrypted data matches the original
    js_decrypted = json.loads(js_output)
    assert js_decrypted == test_data, "Browser JavaScript decryption produced different result"
    logger.info("Python encrypt -> Browser JS decrypt: Success!")
    print("Python encrypt -> Browser JS decrypt: Success!")

def test_js_encrypt_python_decrypt(page, web_server):
    """
    Test that data encrypted in browser JavaScript can be decrypted in Python
    """
    logger.info("Starting JS encrypt -> Python decrypt test")
    
    # Define test data in Python first
    test_data = {
        "message": "Hello from JavaScript!",
        "numbers": [1, 2, 3, 4, 5],
        "nested": {
            "value": "Test nested object"
        }
    }
    test_data_json = json.dumps(test_data)
    logger.info(f"Test data prepared, length: {len(test_data_json)} chars")

    # --- Step 1: Generate keys and encrypt using Browser JS --- #
    crypto_runner_url = f"{web_server}/tests/crypto_runner.html"
    logger.info(f"Using crypto runner at: {crypto_runner_url}")
    
    page.goto(crypto_runner_url)
    page.wait_for_load_state("networkidle")
    
    # Wait for crypto libraries to be loaded
    page.wait_for_function("typeof window.JSEncrypt === 'function'", timeout=60000)
    page.wait_for_function("typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'", timeout=60000)
    logger.info("Page loaded and JS libraries ready")

    js_encrypt_args = {
        "plaintext": test_data_json
    }

    logger.info("Executing browser encryption logic")
    encrypted_data = page.evaluate("""
    (args) => {
        const { plaintext } = args;
        try {
            console.log('[TEST] Starting browser encryption');
            
            // Generate RSA keys
            const crypt = new window.JSEncrypt({default_key_size: 2048});
            console.log('[TEST] JSEncrypt instance created');
            crypt.getKey(); // Generate keys
            const privateKey = crypt.getPrivateKey();
            console.log('[TEST] Private key generated, length:', privateKey.length);
            const publicKey = crypt.getPublicKey();
            console.log('[TEST] Public key generated, length:', publicKey.length);
            
            // Generate a random AES key and IV using CryptoJS
            const aesKey = CryptoJS.lib.WordArray.random(32); // 256 bits
            console.log('[TEST] Generated AES key, sigBytes:', aesKey.sigBytes);
            const iv = CryptoJS.lib.WordArray.random(16);    // 128 bits
            console.log('[TEST] Generated IV, sigBytes:', iv.sigBytes);

            // Convert key and IV to Base64 for Python
            const iv_b64 = CryptoJS.enc.Base64.stringify(iv);
            console.log('[TEST] IV Base64 length:', iv_b64.length);

            // --- RSA Encryption of AES Key ---
            // 1. Get the raw AES key as a Base64 string
            const aesKey_b64 = CryptoJS.enc.Base64.stringify(aesKey);
            console.log('[TEST] AES key Base64 length:', aesKey_b64.length);

            // 2. Set up JSEncrypt with the public key
            console.log('[TEST] Setting up JSEncrypt for RSA encryption');
            const jsEncrypt = new window.JSEncrypt();
            jsEncrypt.setPublicKey(publicKey);

            // 3. Encrypt the Base64 string of the AES key using RSA
            console.log('[TEST] Encrypting AES key with RSA');
            const encryptedKey_b64 = jsEncrypt.encrypt(aesKey_b64);
            if (!encryptedKey_b64) {
                throw new Error('JSEncrypt failed to encrypt the Base64 AES key.');
            }
            console.log('[TEST] Encrypted key Base64 length:', encryptedKey_b64.length);

            // Encrypt the message using AES
            console.log('[TEST] Starting AES encryption of plaintext, length:', plaintext.length);
            const encryptedData = CryptoJS.AES.encrypt(plaintext, aesKey, {
                iv: iv,
                mode: CryptoJS.mode.CBC,
                padding: CryptoJS.pad.Pkcs7
            });
            console.log('[TEST] AES encryption complete');
            
            // Return the encrypted data as Base64 strings
            const ciphertext_b64 = CryptoJS.enc.Base64.stringify(encryptedData.ciphertext);
            console.log('[TEST] Ciphertext Base64 length:', ciphertext_b64.length);
            
            console.log('[TEST] Encryption successful!');
            return {
                ciphertext: ciphertext_b64,
                cipherkey: encryptedKey_b64,
                iv: iv_b64,
                privateKey: privateKey
            };
        } catch (error) {
             console.error('[TEST] Browser Encryption error:', error);
            return { error: error.message || 'Browser encryption failed' };
        }
    }
    """, js_encrypt_args)

    if isinstance(encrypted_data, dict) and 'error' in encrypted_data:
        error_msg = f"Browser JavaScript encryption failed: {encrypted_data['error']}"
        logger.error(error_msg)
        pytest.fail(error_msg)
    logger.info("Browser encryption successful")

    # --- Step 3: Decrypt using Python --- #
    private_key_bytes = encrypted_data['privateKey'].encode('utf-8')
    
    # Convert the Base64 encrypted data to bytes for Python decryption
    ciphertext = base64.b64decode(encrypted_data['ciphertext'])
    cipherkey = base64.b64decode(encrypted_data['cipherkey'])
    iv = base64.b64decode(encrypted_data['iv'])
    logger.info(f"Prepared encrypted data for Python, ciphertext size: {len(ciphertext)} bytes")
    
    # Decrypt with Python
    logger.info("Starting Python decryption")
    decrypted_bytes = decrypt({'ciphertext': ciphertext, 'iv': iv}, cipherkey, private_key_bytes)
    
    # Verify the decryption worked
    assert decrypted_bytes is not None, "Python decryption failed"
    logger.info(f"Python decryption successful, decrypted size: {len(decrypted_bytes)} bytes")
    
    # Parse the decrypted data and compare with the original
    decrypted_data = json.loads(decrypted_bytes.decode('utf-8'))
    assert decrypted_data == test_data, "Python decryption produced different result"
    logger.info("JS encrypt -> Python decrypt: Success!")
    print("Browser JS encrypt -> Python decrypt: Success!") 
