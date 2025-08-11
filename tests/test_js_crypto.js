/**
 * Unit tests for JavaScript crypto functions
 * Run with: node tests/test_js_crypto.js
 */

// Load the shim first to create browser environment objects
require('./js_test_shim.js');

// Import JSEncrypt correctly for Node.js
const JSEncrypt = require('jsencrypt');
const cryptoJs = require('crypto-js');

// Mock Vue component for testing
const crypto = {
    generateClientKeys() {
        const crypt = new JSEncrypt({ default_key_size: 2048 });
        crypt.getKey();
        this.clientPrivateKey = crypt.getPrivateKey();
        this.clientPublicKey = crypt.getPublicKey();
        return {
            privateKey: this.clientPrivateKey,
            publicKey: this.clientPublicKey
        };
    },

    extractBase64(pemString) {
        return pemString
            .replace(/-----BEGIN.*?-----/, '')
            .replace(/-----END.*?-----/, '')
            .replace(/\s/g, '');
    },

    async encrypt(plaintext, publicKeyPem) {
        try {
            // Generate random AES key (256 bits)
            const aesKey = cryptoJs.lib.WordArray.random(32);

            // Generate random IV (16 bytes)
            const iv = cryptoJs.lib.WordArray.random(16);

            // Encrypt the plaintext with AES in CBC mode with PKCS7 padding
            const encrypted = cryptoJs.AES.encrypt(
                plaintext,
                aesKey,
                {
                    iv: iv,
                    mode: cryptoJs.mode.CBC,
                    padding: cryptoJs.pad.Pkcs7
                }
            );

            // Prepare the RSA encryption
            const jsEncrypt = new JSEncrypt();
            jsEncrypt.setPublicKey(publicKeyPem);

            // Encrypt the AES key with RSA
            const aesKeyBase64 = cryptoJs.enc.Base64.stringify(aesKey);
            const encryptedKey = jsEncrypt.encrypt(aesKeyBase64);

            if (!encryptedKey) {
                throw new Error('RSA encryption of AES key failed');
            }

            return {
                ciphertext: encrypted.toString(), // Use toString() to get the Base64 representation
                cipherkey: encryptedKey,
                iv: cryptoJs.enc.Base64.stringify(iv)
            };
        } catch (error) {
            console.error('Encryption error:', error);
            return null;
        }
    },

    async decrypt(ciphertext, encryptedKey, ivBase64) {
        try {
            // Prepare for RSA decryption
            const jsEncrypt = new JSEncrypt();
            jsEncrypt.setPrivateKey(this.clientPrivateKey);

            // Decrypt the AES key with RSA
            const decryptedKeyBase64 = jsEncrypt.decrypt(encryptedKey);
            if (!decryptedKeyBase64) {
                throw new Error('RSA decryption of AES key failed');
            }

            // Convert the Base64 key to a WordArray
            const aesKey = cryptoJs.enc.Base64.parse(decryptedKeyBase64);

            // Convert the Base64 IV to a WordArray
            const iv = cryptoJs.enc.Base64.parse(ivBase64);

            // Decrypt the ciphertext with AES
            // Note: ciphertext from encrypt() is already a Base64 string
            const decrypted = cryptoJs.AES.decrypt(
                ciphertext,
                aesKey,
                {
                    iv: iv,
                    mode: cryptoJs.mode.CBC,
                    padding: cryptoJs.pad.Pkcs7
                }
            );

            // Convert the decrypted WordArray to a string
            return cryptoJs.enc.Utf8.stringify(decrypted);
        } catch (error) {
            console.error('Decryption error:', error);
            return null;
        }
    }
};

// Test functions
async function runTests() {
    console.log("Starting JavaScript crypto tests...");
    let passed = 0;
    let failed = 0;

    try {
        // Test key generation
        console.log("Test 1: Key Generation");
        const keys = crypto.generateClientKeys();
        if (keys.privateKey && keys.publicKey) {
            console.log("✅ Key generation successful");
            passed++;
        } else {
            console.log("❌ Key generation failed");
            failed++;
        }

        // Test encrypt and decrypt with simple message
        console.log("\nTest 2: Encrypt and Decrypt Simple Message");
        const message = "Hello, World!";
        const encryptedData = await crypto.encrypt(message, keys.publicKey);

        if (encryptedData && encryptedData.ciphertext && encryptedData.cipherkey && encryptedData.iv) {
            console.log("✅ Encryption successful");
            passed++;

            const decryptedMessage = await crypto.decrypt(
                encryptedData.ciphertext,
                encryptedData.cipherkey,
                encryptedData.iv
            );

            if (decryptedMessage === message) {
                console.log("✅ Decryption successful");
                passed++;
            } else {
                console.log("❌ Decryption failed or content mismatch");
                failed++;
            }
        } else {
            console.log("❌ Encryption failed");
            failed++;
        }

        // Test with JSON object
        console.log("\nTest 3: Encrypt and Decrypt JSON Object");
        const jsonObject = {
            name: "Test User",
            messages: [
                { role: "user", content: "Hello" },
                { role: "assistant", content: "Hi there!" }
            ]
        };

        const jsonString = JSON.stringify(jsonObject);
        const encryptedJson = await crypto.encrypt(jsonString, keys.publicKey);

        if (encryptedJson) {
            console.log("✅ JSON encryption successful");
            passed++;

            const decryptedJson = await crypto.decrypt(
                encryptedJson.ciphertext,
                encryptedJson.cipherkey,
                encryptedJson.iv
            );

            try {
                const parsedJson = JSON.parse(decryptedJson);
                if (parsedJson.name === jsonObject.name &&
                    parsedJson.messages.length === jsonObject.messages.length) {
                    console.log("✅ JSON decryption successful");
                    passed++;
                } else {
                    console.log("❌ JSON decryption content mismatch");
                    failed++;
                }
            } catch (e) {
                console.log("❌ JSON parsing failed after decryption");
                failed++;
            }
        } else {
            console.log("❌ JSON encryption failed");
            failed++;
        }

        // Test with long text
        console.log("\nTest 4: Encrypt and Decrypt Long Text");
        const longText = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. ".repeat(100);
        const encryptedLongText = await crypto.encrypt(longText, keys.publicKey);

        if (encryptedLongText) {
            console.log("✅ Long text encryption successful");
            passed++;

            const decryptedLongText = await crypto.decrypt(
                encryptedLongText.ciphertext,
                encryptedLongText.cipherkey,
                encryptedLongText.iv
            );

            if (decryptedLongText === longText) {
                console.log("✅ Long text decryption successful");
                passed++;
            } else {
                console.log("❌ Long text decryption failed or content mismatch");
                console.log(`Expected ${longText.length} chars, got ${decryptedLongText ? decryptedLongText.length : 0} chars`);
                failed++;
            }
        } else {
            console.log("❌ Long text encryption failed");
            failed++;
        }

    } catch (error) {
        console.error("Test error:", error);
        failed++;
    }

    // Print summary
    console.log("\nTest Results:");
    console.log(`Passed: ${passed}`);
    console.log(`Failed: ${failed}`);
    console.log(`Total: ${passed + failed}`);

    if (failed > 0) {
        process.exit(1);
    }
}

// Run the tests
runTests();
