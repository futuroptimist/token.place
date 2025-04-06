import pytest
import base64
import json
import sys
import os
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from encrypt import generate_keys, encrypt, decrypt
from playwright.sync_api import Page
import pathlib
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('crypto_tests')

def test_python_encrypt_js_decrypt(page: Page):
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
    # In Python, we encrypt the *raw* AES key bytes with RSA
    ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)
    logger.info(f"Encrypted data in Python, ciphertext size: {len(ciphertext_dict['ciphertext'])} bytes")
    
    # Convert encrypted data to Base64 strings for JS
    ciphertext_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
    # cipherkey is the RSA encrypted *raw* AES key bytes, need to Base64 encode for JS
    cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8') 
    iv_b64 = base64.b64encode(iv).decode('utf-8')
    logger.info("Prepared encrypted data for JS (Base64 encoded)")
    
    # Get the absolute path to the runner HTML file
    runner_html_path = pathlib.Path("tests/crypto_runner.html").resolve()
    runner_url = runner_html_path.as_uri()
    logger.info(f"Using crypto runner at: {runner_url}")
    
    # Navigate Playwright page to the runner
    page.goto(runner_url)
    page.wait_for_load_state("networkidle")
    # Explicitly wait for JSEncrypt and CryptoJS to be defined
    page.wait_for_function("typeof window.JSEncrypt === 'function'", timeout=60000)
    page.wait_for_function("typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'", timeout=60000)
    logger.info("Page loaded and JS libraries ready")

    # Define the arguments to pass to page.evaluate
    js_args = {
        "ciphertext_b64": ciphertext_b64,
        "encryptedKey_b64": cipherkey_b64, # Pass the Base64 of the RSA encrypted raw AES key
        "iv_b64": iv_b64,
        "privateKeyPem": private_key_pem
    }

    logger.info("Executing browser decryption logic")
    # Execute the original browser decryption logic within the page's context
    js_output = page.evaluate(""" 
        (args) => { 
            // This code runs in the browser 
            const { ciphertext_b64, encryptedKey_b64, iv_b64, privateKeyPem } = args;
            
            try {
                console.log('[TEST] Starting browser decryption');
                // Set up the private key for decryption
                console.log('[TEST] Attempting to use JSEncrypt. Type:', typeof window.JSEncrypt);
                const jsEncrypt = new window.JSEncrypt();
                if (typeof jsEncrypt.setPrivateKey !== 'function') throw new Error('jsEncrypt.setPrivateKey is not a function');
                jsEncrypt.setPrivateKey(privateKeyPem);
                if (typeof jsEncrypt !== 'object' || jsEncrypt === null) throw new Error('JSEncrypt instance became invalid after setPrivateKey');
                console.log('[TEST] JSEncrypt initialized with private key');

                // Decrypt the AES key with RSA
                // JSEncrypt.decrypt expects Base64 input (cipherkey_b64) and now returns the Base64 string of the original AES key
                if (typeof jsEncrypt.decrypt !== 'function') throw new Error('jsEncrypt.decrypt is not a function');
                const decryptedKey_b64 = jsEncrypt.decrypt(encryptedKey_b64); // Should return Base64 string
                console.log('[TEST] Output of jsEncrypt.decrypt (Base64 key expected):', decryptedKey_b64);

                if (decryptedKey_b64 === false || decryptedKey_b64 === null || decryptedKey_b64 === undefined || typeof decryptedKey_b64 !== 'string' || decryptedKey_b64.length === 0) {
                    throw new Error('RSA decryption of AES key failed in browser or returned invalid Base64 string: ' + decryptedKey_b64);
                }

                // Convert the Base64 decrypted key string directly to a WordArray for CryptoJS
                let aesKey;
                try {
                    if (!CryptoJS.enc.Base64) throw new Error('CryptoJS.enc.Base64 is missing');
                    aesKey = CryptoJS.enc.Base64.parse(decryptedKey_b64);
                    console.log('[TEST] Successfully parsed Base64 key to WordArray, sigBytes:', aesKey.sigBytes);
                } catch (parseError) {
                     console.error("[TEST] Failed to parse Base64 decrypted key:", parseError);
                     throw new Error('Failed to parse the Base64 decrypted AES key for CryptoJS. Decrypted Base64 value: ' + decryptedKey_b64);
                }

                if (!aesKey || aesKey.sigBytes !== 32) { // Check if parsing succeeded and key size is correct (256 bits)
                    throw new Error('Parsed AES key from Base64 is invalid or has incorrect size (' + (aesKey ? aesKey.sigBytes : 'undefined') + ' bytes). Original Base64: ' + decryptedKey_b64);
                }
                console.log('[TEST] Parsed AES key for CryptoJS (WordArray from Base64):', aesKey);

                // Convert the Base64 IV to a WordArray
                const iv = CryptoJS.enc.Base64.parse(iv_b64);
                console.log('[TEST] Parsed IV WordArray, sigBytes:', iv.sigBytes);
                
                // Convert the Base64 ciphertext to a WordArray
                const ciphertextWordArray = CryptoJS.enc.Base64.parse(ciphertext_b64);
                console.log('[TEST] Parsed ciphertext WordArray, sigBytes:', ciphertextWordArray.sigBytes);
                
                // Assert CryptoJS components are loaded
                if (!CryptoJS || !CryptoJS.AES || !CryptoJS.enc || !CryptoJS.mode || !CryptoJS.pad) throw new Error('CryptoJS components missing');
                if (!CryptoJS.mode.CBC || !CryptoJS.pad.Pkcs7 || !CryptoJS.enc.Utf8) throw new Error('CryptoJS modes/padding/enc missing');
                console.log('[TEST] All CryptoJS components verified');
                
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
                if (!decryptedString) {
                     // If stringify returns empty, decryption might have failed (e.g., bad padding)
                     // Let's check the original decrypted WordArray for content
                     console.error('[TEST] CryptoJS.enc.Utf8.stringify returned empty. Decrypted WordArray:', decrypted);
                     throw new Error('AES decryption resulted in empty string after UTF8 stringify. Check padding or key.');
                 }
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
        raise RuntimeError(error_msg)
    
    # Verify the decrypted data matches the original
    js_decrypted = json.loads(js_output)
    assert js_decrypted == test_data, "Browser JavaScript decryption produced different result"
    logger.info("Python encrypt -> Browser JS decrypt: Success!")
    print("Python encrypt -> Browser JS decrypt: Success!")

def test_js_encrypt_python_decrypt(page: Page):
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

    # --- Step 1: Generate keys using Browser JS --- #
    runner_html_path = pathlib.Path("tests/crypto_runner.html").resolve()
    runner_url = runner_html_path.as_uri()
    logger.info(f"Using crypto runner at: {runner_url}")
    
    page.goto(runner_url)
    page.wait_for_load_state("networkidle")
    # Explicitly wait for JSEncrypt and CryptoJS to be defined
    page.wait_for_function("typeof window.JSEncrypt === 'function'", timeout=60000)
    page.wait_for_function("typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'", timeout=60000)
    logger.info("Page loaded and JS libraries ready")

    logger.info("Generating RSA keys in browser")
    js_keys = page.evaluate("""
        () => {
            try {
                console.log('[TEST] Starting browser key generation');
                console.log('[TEST] Attempting to use JSEncrypt. Type:', typeof window.JSEncrypt);
                const crypt = new window.JSEncrypt({default_key_size: 2048});
                console.log('[TEST] JSEncrypt instance created');
                if (typeof crypt !== 'object' || crypt === null) throw new Error('JSEncrypt instance (crypt) is not an object');
                if (typeof crypt.getKey !== 'function') throw new Error('crypt.getKey is not a function');
                console.log('[TEST] Generating keys...');
                crypt.getKey(); // Generate keys
                if (typeof crypt.getPrivateKey !== 'function') throw new Error('crypt.getPrivateKey is not a function');
                const privateKey = crypt.getPrivateKey();
                console.log('[TEST] Private key generated, length:', privateKey.length);
                if (typeof crypt.getPublicKey !== 'function') throw new Error('crypt.getPublicKey is not a function');
                const publicKey = crypt.getPublicKey();
                console.log('[TEST] Public key generated, length:', publicKey.length);
                return { privateKey: privateKey, publicKey: publicKey };
            } catch (error) {
                console.error('[TEST] Browser key generation error:', error);
                return { error: error.message || 'Browser key generation failed' };
            }
        }
        """)
    
    if isinstance(js_keys, dict) and 'error' in js_keys:
        error_msg = f"Browser JavaScript key generation failed: {js_keys['error']}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    private_key_pem = js_keys['privateKey']
    public_key_pem = js_keys['publicKey']
    logger.info("Browser key generation successful")
    
    # --- Step 2: Encrypt using Browser JS --- #
    # Navigate again or reuse page if state allows (navigating is safer)
    page.goto(runner_url)
    page.wait_for_load_state("networkidle")
    # Explicitly wait for JSEncrypt and CryptoJS again after navigation
    page.wait_for_function("typeof window.JSEncrypt === 'function'", timeout=60000)
    page.wait_for_function("typeof window.CryptoJS === 'object' && typeof window.CryptoJS.AES === 'object'", timeout=60000)
    logger.info("Page reloaded for encryption step")
    
    js_encrypt_args = {
        "plaintext": test_data_json, # Pass the original JSON string
        "publicKeyPem": public_key_pem
    }

    logger.info("Executing browser encryption logic")
    encrypted_data = page.evaluate("""
        (args) => {
            const { plaintext, publicKeyPem } = args;
            try {
                console.log('[TEST] Starting browser encryption');
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

                // 2. Set up JSEncrypt with the Python public key
                console.log('[TEST] Setting up JSEncrypt with public key');
                const jsEncrypt = new window.JSEncrypt();
                jsEncrypt.setPublicKey(publicKeyPem);

                // 3. Encrypt the Base64 string of the AES key using RSA
                console.log('[TEST] Encrypting AES key with RSA');
                const encryptedKey_b64 = jsEncrypt.encrypt(aesKey_b64);
                if (encryptedKey_b64 === false) {
                    throw new Error('JSEncrypt failed to encrypt the Base64 AES key.');
                }
                console.log('[TEST] Encrypted key Base64 length:', encryptedKey_b64.length);
                // --------------------------------

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
                    iv: iv_b64
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
        raise RuntimeError(error_msg)
    logger.info("Browser encryption successful")

    # --- Step 3: Decrypt using Python --- #
    private_key_bytes = private_key_pem.encode('utf-8')
    
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