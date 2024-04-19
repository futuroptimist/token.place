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
                const binaryString = atob(base64);
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
        
            const plaintextWordArray = CryptoJS.enc.Utf8.parse(plaintext);
            const paddedPlaintext = CryptoJS.pad.Pkcs7.pad(plaintextWordArray);
            const key = CryptoJS.lib.WordArray.random(256 / 8);
            const iv = CryptoJS.lib.WordArray.random(128 / 8);
            const encrypted = CryptoJS.AES.encrypt(paddedPlaintext, key, {
                iv: iv,
                mode: CryptoJS.mode.CBC,
                padding: CryptoJS.pad.Pkcs7
            });
        
            console.log('AES Encryption Result:', encrypted);
        
            const keyBase64 = CryptoJS.enc.Base64.stringify(key);
            console.log('AES Key (Base64):', keyBase64);
        
            const publicKeyPem = `-----BEGIN PUBLIC KEY-----\n${publicKey}\n-----END PUBLIC KEY-----`;
            const publicKeyBuffer = await crypto.subtle.importKey(
                'spki',
                this.base64ToArrayBuffer(atob(publicKey)),
                {
                    name: 'RSA-OAEP',
                    hash: 'SHA-256',
                },
                true,
                ['encrypt']
            );
        
            const encryptedKeyBuffer = await crypto.subtle.encrypt(
                {
                    name: 'RSA-OAEP',
                },
                publicKeyBuffer,
                new TextEncoder().encode(keyBase64)
            );
        
            const encryptedKey = btoa(String.fromCharCode.apply(null, new Uint8Array(encryptedKeyBuffer)));
        
            console.log('RSA Encryption Result:', encryptedKey);
        
            return {
                ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
                encryptedKey: encryptedKey,
                iv: CryptoJS.enc.Base64.stringify(iv)
            };
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
            const ivWordArray = CryptoJS.enc.Base64.parse(iv);
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
                    const retrieveResponse = await fetch('/retrieve', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({ client_public_key: this.clientPublicKey })
                    });
        
                    console.log('Retrieve Response:', retrieveResponse);
        
                    if (retrieveResponse.ok) {
                        const responseData = await retrieveResponse.json();
                        console.log('Response Data:', responseData);
        
                        if (responseData.chat_history && responseData.cipherkey && responseData.iv) {
                            console.log('Received complete response data:', responseData);
                            const decryptedChatHistory = await this.decrypt(
                                responseData.chat_history,
                                responseData.cipherkey,
                                responseData.iv
                            );
                            console.log('Decrypted Chat History:', decryptedChatHistory);
        
                            if (decryptedChatHistory) {
                                console.log('Decrypted Chat History JSON:', JSON.parse(decryptedChatHistory));
                                clearInterval(pollInterval);
                                resolve(JSON.parse(decryptedChatHistory));
                            }
                        } else {
                            console.log('Incomplete response data. Retrying...');
                        }
                    } else {
                        console.error('Error retrieving response:', retrieveResponse.status);
                    }
        
                    // Check for timeout
                    if (Date.now() - startTime > 60000) {
                        clearInterval(pollInterval);
                        console.log('Timeout reached while polling /retrieve endpoint');
                        resolve(null);
                    }
                }, 3000); // Increased polling interval to 3 seconds
            });
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
        
                // Send the message to the /faucet endpoint
                const chatHistoryString = JSON.stringify(this.chatHistory);
                const encryptedData = await this.encrypt(chatHistoryString, this.serverPublicKey);

                if (encryptedData === null) {
                    console.error('Encryption failed. Aborting message send.');
                    return;
                }

                const faucetPayload = {
                    server_public_key: this.serverPublicKey,
                    client_public_key: this.clientPublicKey,
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