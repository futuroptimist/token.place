require('./js_test_shim.js');

const http = require('http');
const { once } = require('events');
const JSEncrypt = require('jsencrypt');
const CryptoJS = require('crypto-js');

function encryptForClient(plaintext, clientPublicKey) {
    const aesKey = CryptoJS.lib.WordArray.random(32);
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
    jsEncrypt.setPublicKey(clientPublicKey);
    const aesKeyBase64 = CryptoJS.enc.Base64.stringify(aesKey);
    const encryptedKey = jsEncrypt.encrypt(aesKeyBase64);

    if (!encryptedKey) {
        throw new Error('Failed to encrypt AES key for client');
    }

    return {
        ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
        cipherkey: encryptedKey,
        iv: CryptoJS.enc.Base64.stringify(iv)
    };
}

function decryptRequestBody(body, serverPrivateKey) {
    const jsEncrypt = new JSEncrypt();
    jsEncrypt.setPrivateKey(serverPrivateKey);

    const decryptedKeyBase64 = jsEncrypt.decrypt(body.cipherkey);
    if (!decryptedKeyBase64) {
        throw new Error('Unable to decrypt AES key with mock server private key');
    }

    const aesKey = CryptoJS.enc.Base64.parse(decryptedKeyBase64);
    const iv = CryptoJS.enc.Base64.parse(body.iv);
    const ciphertext = CryptoJS.enc.Base64.parse(body.ciphertext);

    const decrypted = CryptoJS.AES.decrypt(
        { ciphertext },
        aesKey,
        {
            iv,
            mode: CryptoJS.mode.CBC,
            padding: CryptoJS.pad.Pkcs7
        }
    );

    const plaintext = CryptoJS.enc.Utf8.stringify(decrypted);
    if (!plaintext) {
        throw new Error('Mock server failed to recover plaintext payload');
    }

    return plaintext;
}

async function startMockServer() {
    const serverCrypt = new JSEncrypt({ default_key_size: 2048 });
    serverCrypt.getKey();
    const serverPrivateKey = serverCrypt.getPrivateKey();
    const serverPublicKey = serverCrypt.getPublicKey();

    const server = http.createServer(async (req, res) => {
        try {
            const { pathname } = new URL(req.url, 'http://127.0.0.1');

            if (
                req.method === 'GET'
                && (pathname === '/api/v1/public-key' || pathname === '/v1/public-key')
            ) {
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ public_key: serverPublicKey }));
                return;
            }

            if (
                req.method === 'POST'
                && (pathname === '/api/v1/chat/completions' || pathname === '/v1/chat/completions')
            ) {
                let bodyRaw = '';
                req.on('data', chunk => {
                    bodyRaw += chunk;
                });

                req.on('end', () => {
                    try {
                        const parsed = JSON.parse(bodyRaw || '{}');
                        const requiredFields = ['ciphertext', 'cipherkey', 'iv', 'client_public_key'];
                        for (const field of requiredFields) {
                            if (!parsed[field]) {
                                res.writeHead(400, { 'Content-Type': 'application/json' });
                                res.end(JSON.stringify({ error: `Missing field: ${field}` }));
                                return;
                            }
                        }

                        const requestPlaintext = decryptRequestBody(parsed, serverPrivateKey);
                        let userMessage;
                        try {
                            const jsonPayload = JSON.parse(requestPlaintext);
                            userMessage = jsonPayload.content || '';
                        } catch (error) {
                            userMessage = requestPlaintext;
                        }

                        const responsePayload = {
                            role: 'assistant',
                            content: `Mock response: ${userMessage}`
                        };
                        const encryptedResponse = encryptForClient(
                            JSON.stringify(responsePayload),
                            parsed.client_public_key
                        );

                        res.writeHead(200, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify(encryptedResponse));
                    } catch (error) {
                        res.writeHead(500, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ error: error.message }));
                    }
                });
                return;
            }

            res.writeHead(404, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Not Found' }));
        } catch (error) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: error.message }));
        }
    });

    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const address = server.address();
    const baseUrl = `http://127.0.0.1:${address.port}`;

    return {
        baseUrl,
        publicKey: serverPublicKey,
        async stop() {
            await new Promise((resolve, reject) => {
                server.close(err => {
                    if (err) {
                        reject(err);
                    } else {
                        resolve();
                    }
                });
            });
        }
    };
}

module.exports = {
    startMockServer
};
