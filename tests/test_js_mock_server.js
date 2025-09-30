/**
 * Integration test verifying the JavaScript mock server round-trip.
 */

require('./js_test_shim.js');

const assert = require('assert');
const JSEncrypt = require('jsencrypt');
const CryptoJS = require('crypto-js');
const { startMockServer } = require('./mock_js_server');

function encryptForServer(plaintext, serverPublicKey) {
    const aesKey = CryptoJS.lib.WordArray.random(32); // 256-bit AES key
    const iv = CryptoJS.lib.WordArray.random(16);

    const encrypted = CryptoJS.AES.encrypt(
        CryptoJS.enc.Utf8.parse(plaintext),
        aesKey,
        {
            iv,
            mode: CryptoJS.mode.CBC,
            padding: CryptoJS.pad.Pkcs7
        }
    );

    const jsEncrypt = new JSEncrypt();
    jsEncrypt.setPublicKey(serverPublicKey);
    const aesKeyBase64 = CryptoJS.enc.Base64.stringify(aesKey);
    const encryptedKey = jsEncrypt.encrypt(aesKeyBase64);

    if (!encryptedKey) {
        throw new Error('Failed to RSA encrypt AES key for server');
    }

    return {
        ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
        cipherkey: encryptedKey,
        iv: CryptoJS.enc.Base64.stringify(iv)
    };
}

function decryptFromServer(payload, clientPrivateKey) {
    const jsEncrypt = new JSEncrypt();
    jsEncrypt.setPrivateKey(clientPrivateKey);

    const decryptedKeyBase64 = jsEncrypt.decrypt(payload.cipherkey);
    if (!decryptedKeyBase64) {
        throw new Error('Failed to RSA decrypt AES key from server');
    }

    const aesKey = CryptoJS.enc.Base64.parse(decryptedKeyBase64);
    const iv = CryptoJS.enc.Base64.parse(payload.iv);
    const ciphertext = CryptoJS.enc.Base64.parse(payload.ciphertext);

    const decrypted = CryptoJS.AES.decrypt(
        { ciphertext },
        aesKey,
        {
            iv,
            mode: CryptoJS.mode.CBC,
            padding: CryptoJS.pad.Pkcs7
        }
    );

    return CryptoJS.enc.Utf8.stringify(decrypted);
}

async function runMockServerTest() {
    const clientCrypt = new JSEncrypt({ default_key_size: 2048 });
    clientCrypt.getKey();
    const clientPrivateKey = clientCrypt.getPrivateKey();
    const clientPublicKey = clientCrypt.getPublicKey();

    const server = await startMockServer();

    try {
        assert.ok(server.baseUrl, 'Mock server should expose baseUrl');

        const keyResponse = await fetch(`${server.baseUrl}/api/v1/public-key`);
        assert.strictEqual(keyResponse.status, 200, 'Public key endpoint should return 200');
        const keyPayload = await keyResponse.json();
        assert.strictEqual(
            keyPayload.public_key,
            server.publicKey,
            'Server should return its public key'
        );

        const plaintext = JSON.stringify({
            role: 'user',
            content: 'Hello mock server!'
        });

        const encryptedPayload = encryptForServer(plaintext, server.publicKey);

        const response = await fetch(`${server.baseUrl}/api/v1/chat/completions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ciphertext: encryptedPayload.ciphertext,
                cipherkey: encryptedPayload.cipherkey,
                iv: encryptedPayload.iv,
                client_public_key: clientPublicKey,
                model: 'mock-llm'
            })
        });

        assert.strictEqual(response.status, 200, 'Chat endpoint should return 200');
        const encryptedReply = await response.json();
        assert.ok(encryptedReply.ciphertext, 'Response should include ciphertext');
        assert.ok(encryptedReply.cipherkey, 'Response should include cipherkey');
        assert.ok(encryptedReply.iv, 'Response should include iv');

        const decryptedReply = decryptFromServer(encryptedReply, clientPrivateKey);
        const parsedReply = JSON.parse(decryptedReply);

        assert.strictEqual(parsedReply.role, 'assistant', 'Reply should be from assistant');
        assert.strictEqual(
            parsedReply.content,
            'Mock response: Hello mock server!',
            'Reply content should match mock server output'
        );

        console.log('✅ Mock server round-trip test passed');
    } finally {
        await server.stop();
    }
}

if (require.main === module) {
    runMockServerTest().catch(error => {
        console.error('❌ Mock server test failed:', error);
        process.exitCode = 1;
    });
}

module.exports = { runMockServerTest };
