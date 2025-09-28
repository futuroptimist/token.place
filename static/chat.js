new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        serverPublicKey: null,
        clientPrivateKey: null,
        clientPublicKey: null,
        isGeneratingResponse: false,
        isTouchInput: false
    },
    mounted() {
        this.detectTouchInput();
        this.getServerPublicKey().then(() => {
            this.generateClientKeys();
        });
    },
    methods: {
        detectTouchInput() {
            try {
                const hasWindow = typeof window !== 'undefined';
                const nav = typeof navigator !== 'undefined' ? navigator : undefined;
                const doc = typeof document !== 'undefined' ? document : undefined;
                const hasTouch =
                    (hasWindow && 'ontouchstart' in window) ||
                    (nav && (nav.maxTouchPoints > 0 || nav.msMaxTouchPoints > 0));

                this.isTouchInput = Boolean(hasTouch);

                if (doc && doc.body) {
                    if (this.isTouchInput) {
                        doc.body.classList.add('touch-device');
                    } else {
                        doc.body.classList.remove('touch-device');
                    }
                }
            } catch (error) {
                console.warn('Unable to determine touch capabilities:', error);
                this.isTouchInput = false;
            }
        },
        getServerPublicKey() {
            // Fetch the server's public key from the API
            return fetch('/api/v1/public-key')
                .then(response => {
                    if (response.ok) {
                        return response.json();
                    }
                    throw new Error('Failed to fetch server public key');
                })
                .then(data => {
                    if (data && data.public_key) {
                        this.serverPublicKey = data.public_key;
                    } else {
                        console.error('Unexpected server public key format:', data);
                    }
                })
                .catch(error => {
                    console.error('Error fetching server public key:', error);
                });
        },
        generateClientKeys() {
            const crypt = new JSEncrypt({ default_key_size: 2048 });
            crypt.getKey();
            this.clientPrivateKey = crypt.getPrivateKey();
            this.clientPublicKey = crypt.getPublicKey();
        },

        // Convert Base64 string to ArrayBuffer
        base64ToArrayBuffer(base64) {
            try {
                const cleanedBase64 = base64.replace(/\s+/g, '');
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

        // Convert ArrayBuffer to Base64 string
        arrayBufferToBase64(buffer) {
            const bytes = new Uint8Array(buffer);
            let binary = '';
            for (let i = 0; i < bytes.byteLength; i++) {
                binary += String.fromCharCode(bytes[i]);
            }
            return btoa(binary);
        },

        // Extract the Base64 content from PEM format
        extractBase64(pemString) {
            return pemString
                .replace(/-----BEGIN.*?-----/, '')
                .replace(/-----END.*?-----/, '')
                .replace(/\s/g, '');
        },

        /**
         * Encrypt plaintext using hybrid encryption (RSA for key, AES for data)
         * Compatible with Python backend's encrypt function
         */
        async encrypt(plaintext, publicKeyPem) {
            try {

                // Generate random AES key (256 bits)
                const aesKey = CryptoJS.lib.WordArray.random(32); // 32 bytes = 256 bits

                // Generate random IV (16 bytes)
                const iv = CryptoJS.lib.WordArray.random(16);

                // Pad and encrypt the plaintext with AES in CBC mode
                // const paddedData = CryptoJS.pad.Pkcs7.pad(CryptoJS.enc.Utf8.parse(plaintext), 16); // Remove manual padding
                const encrypted = CryptoJS.AES.encrypt(CryptoJS.enc.Utf8.parse(plaintext), aesKey, { // Encrypt original plaintext
                    iv: iv,
                    mode: CryptoJS.mode.CBC,
                    padding: CryptoJS.pad.Pkcs7 // Let CryptoJS handle PKCS7 padding
                });

                // Prepare the RSA encryption
                const jsEncrypt = new JSEncrypt();
                // console.log('Public key PEM provided to encrypt function:', publicKeyPem); // Remove log
                jsEncrypt.setPublicKey(publicKeyPem);

                // Encrypt the AES key with RSA-OAEP (SHA-256)
                const aesKeyBase64 = CryptoJS.enc.Base64.stringify(aesKey);
                // console.log('AES key Base64 being encrypted:', aesKeyBase64); // Remove log
                const encryptedKey = jsEncrypt.encrypt(aesKeyBase64);

                if (!encryptedKey) {
                    throw new Error('RSA encryption of AES key failed');
                }

                return {
                    ciphertext: CryptoJS.enc.Base64.stringify(encrypted.ciphertext),
                    cipherkey: encryptedKey,
                    iv: CryptoJS.enc.Base64.stringify(iv)
                };
            } catch (error) {
                console.error('Encryption error:', error);
                return null;
            }
        },

        /**
         * Decrypt ciphertext using hybrid decryption (RSA for key, AES for data)
         * Compatible with Python backend's decrypt function
         */
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
                const aesKey = CryptoJS.enc.Base64.parse(decryptedKeyBase64);

                // Convert the Base64 IV to a WordArray
                const iv = CryptoJS.enc.Base64.parse(ivBase64);

                // Convert the Base64 ciphertext to a WordArray
                const ciphertextWordArray = CryptoJS.enc.Base64.parse(ciphertext);

                // Decrypt the ciphertext with AES
                const decrypted = CryptoJS.AES.decrypt(
                    { ciphertext: ciphertextWordArray },
                    aesKey,
                    {
                        iv: iv,
                        mode: CryptoJS.mode.CBC,
                        padding: CryptoJS.pad.Pkcs7
                    }
                );

                // Convert the decrypted WordArray to a string
                return CryptoJS.enc.Utf8.stringify(decrypted);
            } catch (error) {
                console.error('Decryption error:', error);
                return null;
            }
        },

        // Send a message to the server using the new API
        async sendMessageApi() {
            if (!this.serverPublicKey) {
                console.error('Server public key not available');
                return null;
            }

            try {
                // Encrypt the chat history
                const encryptedData = await this.encrypt(
                    JSON.stringify(this.chatHistory),
                    this.serverPublicKey
                );

                if (!encryptedData) {
                    throw new Error('Failed to encrypt chat history');
                }

                // Create the API request payload
                const payload = {
                    model: "llama-3-8b-instruct",
                    encrypted: true,
                    client_public_key: this.extractBase64(this.clientPublicKey),
                    messages: encryptedData
                };

                // Send the request to the API
                const response = await fetch('/api/v1/chat/completions', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(payload)
                });

                if (!response.ok) {
                    const errorData = await response.json();
                    throw new Error(`API error: ${errorData.error?.message || 'Unknown error'}`);
                }

                const responseData = await response.json();

                // Handle encrypted response
                if (responseData.encrypted && responseData.data) {
                    const decryptedJson = await this.decrypt(
                        responseData.data.ciphertext,
                        responseData.data.cipherkey,
                        responseData.data.iv
                    );

                    if (!decryptedJson) {
                        throw new Error('Failed to decrypt response');
                    }

                    return JSON.parse(decryptedJson);
                }

                // Handle unencrypted response (should not happen with encrypted=true)
                return responseData;
            } catch (error) {
                console.error('API request error:', error);
                return null;
            }
        },



        // Send a message to the server
        async sendMessage() {
            const messageContent = this.newMessage.trim();
            if (!messageContent || !this.serverPublicKey || this.isGeneratingResponse) {
                return;
            }

            this.isGeneratingResponse = true;
            this.chatHistory.push({ role: "user", content: messageContent });
            this.newMessage = '';

            try {
                // Send the message via the API
                let response = await this.sendMessageApi();

                // Process the response
                if (response) {
                    // For API response, extract last message
                    if (response.choices && response.choices.length > 0) {
                        const assistantMessage = response.choices[0].message;
                        this.chatHistory.push(assistantMessage);
                    }
                    // For legacy response format (full chat history)
                    else if (Array.isArray(response)) {
                        this.chatHistory = response;
                    }
                    else {
                        throw new Error('Unexpected response format');
                    }
                } else {
                    // Add a failure message if we couldn't get a response
                    this.chatHistory.push({
                        role: 'assistant',
                        content: 'Sorry, I encountered an issue generating a response. Please try again.'
                    });
                }
            } catch (error) {
                console.error('Error sending message:', error);
                this.chatHistory.push({
                    role: 'assistant',
                    content: 'Sorry, an error occurred while sending your message. Please try again.'
                });
            } finally {
                this.isGeneratingResponse = false;
            }
        }
    },
    updated() {
        this.$nextTick(() => {
            const container = this.$el.querySelector(".chat-container");
            container.scrollTop = container.scrollHeight;
        });
    }
});
