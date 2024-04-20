new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        serverPublicKey: null,
        clientPrivateKey: null,
        clientPublicKey: null
    },
    mounted() {
        this.getServerPublicKey().then(() => {
            console.log("Final serverPublicKey before encoding:", this.serverPublicKey);
            this.generateClientKeys();
        });
    },
    methods: {
        getServerPublicKey() {
            return fetch('/next_server')
                .then(response => response.json())
                .then(data => {
                    if (data && data.server_public_key) {
                        this.serverPublicKey = data.server_public_key;
                        console.log("Server public key set:", this.serverPublicKey);
                    } else {
                        console.error('Failed to retrieve server public key:', data);
                    }
                })
                .catch((error) => {
                    console.error('Error fetching server public key:', error);
                });
        },
        generateClientKeys() {
            const crypt = new JSEncrypt({ default_key_size: 2048 });
            crypt.getKey();
            this.clientPrivateKey = crypt.getPrivateKey();
            this.clientPublicKey = crypt.getPublicKey();
        },
        base64ToArrayBuffer(base64) {
            try {
                const cleanedBase64 = base64.replace(/\s+/g, ''); // Remove whitespace characters
                const binaryString = atob(cleanedBase64);
                const len = binaryString.length;
                const bytes = new Uint8Array(len);
                for (let i = 0; i < len; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }
                return bytes.buffer;
            } catch (e) {
                console.error('Failed to decode Base64 string:', e);
                return null;
            }
        },
        async encrypt(plaintext, publicKey) {
            console.log('Plaintext:', plaintext);
            console.log('Public Key:', publicKey);
        
            try {
                const plaintextWordArray = CryptoJS.enc.Utf8.parse(plaintext);
                const paddedPlaintext = CryptoJS.pad.Pkcs7.pad(plaintextWordArray);
                const key = CryptoJS.lib.WordArray.random(256 / 8);
                const iv = CryptoJS.lib.WordArray.random(16);
                const encrypted = CryptoJS.AES.encrypt(paddedPlaintext, key, {
                    iv: iv,
                    mode: CryptoJS.mode.CBC,
                    padding: CryptoJS.pad.Pkcs7
                });
        
                console.log('AES Encryption Result:', encrypted);
        
                const keyBase64 = CryptoJS.enc.Base64.stringify(key);
                console.log('AES Key (Base64):', keyBase64);
        
                const encryptedKey = CryptoJS.RC4.encrypt(keyBase64, publicKey).toString();
        
                console.log('RSA Encryption Result:', encryptedKey);
        
                return {
                    ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
                    encryptedKey: encryptedKey,
                    iv: CryptoJS.enc.Base64.stringify(iv)
                };
            } catch (error) {
                console.error('Error during encryption:', error);
                console.error('Error stack trace:', error.stack);
                return null;
            }
        },
        async decrypt(cipherText, encryptedKey, iv) {
            const privateKeyPem = `-----BEGIN PRIVATE KEY-----\n${this.clientPrivateKey}\n-----END PRIVATE KEY-----`;
            const privateKeyBuffer = await crypto.subtle.importKey(
                'pkcs8',
                this.base64ToArrayBuffer(atob(this.clientPrivateKey)),
                {
                    name: 'RSA-OAEP',
                    hash: 'SHA-256',
                },
                true,
                ['decrypt']
            );
        
            const decryptedKeyBuffer = await crypto.subtle.decrypt(
                {
                    name: 'RSA-OAEP',
                },
                privateKeyBuffer,
                this.base64ToArrayBuffer(encryptedKey)
            );
        
            const decryptedKeyBase64 = btoa(String.fromCharCode.apply(null, new Uint8Array(decryptedKeyBuffer)));
        
            const key = CryptoJS.enc.Base64.parse(decryptedKeyBase64);
            const ivWordArray = CryptoJS.lib.WordArray.create(new Uint8Array(this.base64ToArrayBuffer(iv)));
            const cipherTextWordArray = CryptoJS.enc.Base64.parse(cipherText);
        
            const decrypted = CryptoJS.AES.decrypt(
                { ciphertext: cipherTextWordArray },
                key,
                {
                    iv: ivWordArray,
                    mode: CryptoJS.mode.CBC,
                    padding: CryptoJS.pad.Pkcs7
                }
            );
        
            let decryptedPlaintext = '';
            try {
                decryptedPlaintext = CryptoJS.enc.Utf8.stringify(decrypted);
            } catch (error) {
                console.error('Error decrypting the plaintext:', error);
            }
            return decryptedPlaintext;
        },
        async retrieveResponse() {
            const startTime = Date.now();
            return new Promise((resolve) => {
                const pollInterval = setInterval(async () => {
                    try {
                        const retrieveResponse = await fetch('/retrieve', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                            },
                            body: JSON.stringify({ client_public_key: this.clientPublicKey })
                        });
        
                        if (retrieveResponse.ok) {
                            const responseData = await retrieveResponse.json();
        
                            if (responseData.chat_history && responseData.cipherkey && responseData.iv) {
                                const decryptedChatHistory = await this.decrypt(
                                    responseData.chat_history,
                                    responseData.cipherkey,
                                    responseData.iv
                                );
        
                                if (decryptedChatHistory) {
                                    clearInterval(pollInterval);
                                    resolve(JSON.parse(decryptedChatHistory));
                                }
                            }
                        } else {
                            console.error('Error retrieving response:', retrieveResponse.status);
                        }
                    } catch (error) {
                        console.error('Error in retrieveResponse:', error);
                    }
        
                    // Check for timeout
                    if (Date.now() - startTime > 60000) {
                        clearInterval(pollInterval);
                        console.log('Timeout reached while polling /retrieve endpoint');
                        resolve(null);
                    }
                }, 3000);
            });
        },
        extractBase64PublicKey(pemPublicKey) {
            const base64PublicKey = pemPublicKey
                .replace('-----BEGIN PUBLIC KEY-----', '')
                .replace('-----END PUBLIC KEY-----', '')
                .replace(/\s/g, '');
            return base64PublicKey;
        },
        async sendMessage() {
            const messageContent = this.newMessage.trim();
            console.log('Message content:', messageContent);
            if (messageContent && this.serverPublicKey) {
                this.chatHistory.push({ role: 'user', content: messageContent });
                console.log('Chat history:', this.chatHistory);
                this.newMessage = '';
        
                // Send the message to the /inference endpoint
                fetch('/inference', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ chat_history: this.chatHistory })
                })
                .then(response => response.json())
                .then(data => {
                    if (data && Array.isArray(data)) {
                        this.chatHistory = data; // Update UI with the response from the /inference endpoint
                    } else {
                        console.error('Unexpected response format from /inference:', data);
                    }
                })
                .catch((error) => {
                    console.error('Error sending message to /inference:', error);
                });
        
                console.log("Sending encrypted message to /faucet");
                console.log("Server Public Key:", this.serverPublicKey);  // Add this line
        
                try {
                    // Send the message to the /faucet endpoint
                    const chatHistoryString = JSON.stringify(this.chatHistory);
                    const encryptedData = await this.encrypt(chatHistoryString, this.serverPublicKey);
        
                    if (encryptedData === null) {
                        console.error('Encryption failed. Aborting message send.');
                        return;
                    }
        
                    const faucetPayload = {
                        server_public_key: this.serverPublicKey,
                        client_public_key: this.extractBase64PublicKey(this.clientPublicKey),
                        chat_history: encryptedData.ciphertext,
                        cipherkey: encryptedData.encryptedKey,
                        iv: encryptedData.iv
                    };
        
                    console.log("Faucet payload:", faucetPayload);
                    fetch('/faucet', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify(faucetPayload)
                    })
                    .then(response => response.json())
                    .then(async data => {
                        console.log('Response from /faucet:', data);
                        // Process and log the /faucet response. Do not update the UI with this response.
        
                        // Call the retrieveResponse function to poll for the response
                        const decryptedChatHistory = await this.retrieveResponse();
                        if (decryptedChatHistory) {
                            console.log('Decrypted Chat History from /retrieve:', decryptedChatHistory);
                            this.chatHistory = decryptedChatHistory; // Update the chat history with the decrypted response
                        } else {
                            console.error('Failed to retrieve response from /retrieve endpoint');
                        }
                    })
                    .catch((error) => {
                        console.error('Error sending message to /faucet:', error);
                    });
                } catch (error) {
                    console.error('Error during encryption:', error);
                    return;
                }
            }
        },
    },
    updated() {
        this.$nextTick(() => {
            const container = this.$el.querySelector(".chat-container");
            container.scrollTop = container.scrollHeight;
        });
    }
});