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
        async generateClientKeys() {
            // Generate client's RSA key pair
            const keyPair = await window.crypto.subtle.generateKey(
              {
                name: "RSA-OAEP",
                modulusLength: 2048,
                publicExponent: new Uint8Array([0x01, 0x00, 0x01]),
                hash: "SHA-256"
              },
              true,
              ["encrypt", "decrypt"]
            );
          
            // Export the private key in PEM format
            const pemPrivateKey = await window.crypto.subtle.exportKey(
              "pkcs8",
              keyPair.privateKey
            );
          
            // Export the public key in PEM format
            const pemPublicKey = await window.crypto.subtle.exportKey(
              "spki",
              keyPair.publicKey
            );
          
            // Convert the keys from ArrayBuffer to base64 strings
            const privateKeyBase64 = btoa(String.fromCharCode.apply(null, new Uint8Array(pemPrivateKey)));
            const publicKeyBase64 = btoa(String.fromCharCode.apply(null, new Uint8Array(pemPublicKey)));
          
            // Add PEM headers and footers to the base64 strings
            const privateKeyPem = `-----BEGIN PRIVATE KEY-----\n${privateKeyBase64}\n-----END PRIVATE KEY-----`;
            const publicKeyPem = `-----BEGIN PUBLIC KEY-----\n${publicKeyBase64}\n-----END PUBLIC KEY-----`;
          
            // Store the generated keys in the Vue instance data
            this.clientPrivateKey = privateKeyPem;
            this.clientPublicKey = publicKeyPem;
        },
        async encrypt(plaintext, publicKey) {
            const encoder = new TextEncoder();
            const plainTextBytes = encoder.encode(JSON.stringify(plaintext));
            console.log('Plain text bytes:', plainTextBytes);
        
            const key = await crypto.subtle.generateKey({ name: 'AES-CBC', length: 256 }, true, ['encrypt', 'decrypt']);
            console.log('AES key generated:', key);
        
            const iv = crypto.getRandomValues(new Uint8Array(16));
            console.log('IV generated:', iv);
        
            try {
                const publicKeyBytes = this.base64ToArrayBuffer(publicKey);
                console.log('Public key bytes:', publicKeyBytes);
        
                const publicKeyImported = await crypto.subtle.importKey(
                    "spki",
                    publicKeyBytes,
                    { name: "RSA-OAEP", hash: "SHA-256" },
                    false,
                    ["encrypt"]
                );
                console.log('Public key imported:', publicKeyImported);
        
                const exportedKey = await crypto.subtle.exportKey('raw', key);
                console.log('Exported AES key:', exportedKey);
        
                const encryptedKey = await crypto.subtle.encrypt(
                    { name: 'RSA-OAEP' },
                    publicKeyImported,
                    exportedKey
                );
                console.log('Encrypted key:', encryptedKey);
        
                const cipherText = await crypto.subtle.encrypt(
                    { name: 'AES-CBC', iv },
                    key,
                    plainTextBytes
                );
                console.log('Cipher text:', cipherText);
        
                return {
                    cipherText: this.arrayBufferToBase64(cipherText),
                    cipherKey: this.arrayBufferToBase64(encryptedKey),
                    iv: this.arrayBufferToBase64(iv)
                };
            } catch (error) {
                console.error("Error during encryption process:", error);
                console.error("Stack trace:", error.stack);
            }
        },
        async decrypt(cipherText, cipherKey, iv, privateKey) {
            const cipherTextBuffer = this.base64ToArrayBuffer(cipherText);
            const cipherKeyBuffer = this.base64ToArrayBuffer(cipherKey);
            const ivBuffer = this.base64ToArrayBuffer(atob(iv));
        
            const privateKeyBuffer = this.base64ToArrayBuffer(privateKey);
            const privateKeyImported = await crypto.subtle.importKey(
                'pkcs8',
                privateKeyBuffer,
                { name: 'RSA-OAEP', hash: 'SHA-256' },
                false,
                ['decrypt']
            );
        
            const decryptedKey = await crypto.subtle.decrypt(
                { name: 'RSA-OAEP' },
                privateKeyImported,
                cipherKeyBuffer
            );
        
            const key = await crypto.subtle.importKey(
                'raw',
                decryptedKey,
                { name: 'AES-CBC', length: 256 },
                false,
                ['decrypt']
            );
        
            const decryptedBytes = await crypto.subtle.decrypt(
                { name: 'AES-CBC', iv: ivBuffer },
                key,
                cipherTextBuffer
            );
        
            const decoder = new TextDecoder();
            return decoder.decode(decryptedBytes);
        },
        base64ToArrayBuffer(base64) {
            const binaryString = atob(base64);
            const len = binaryString.length;
            const bytes = new Uint8Array(len);
            for (let i = 0; i < len; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            return bytes.buffer;
        },
        arrayBufferToBase64(buffer) {
            const bytes = new Uint8Array(buffer);
            let binary = '';
            for (let i = 0; i < bytes.byteLength; i++) {
                binary += String.fromCharCode(bytes[i]);
            }
            return btoa(binary);
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
                const encryptedData = await this.encrypt(this.chatHistory, this.serverPublicKey);

                const faucetPayload = {
                    server_public_key: this.serverPublicKey,
                    client_public_key: this.clientPublicKey,
                    chat_history: encryptedData.cipherText,
                    cipherkey: encryptedData.cipherKey,
                    iv: encryptedData.iv
                };
        
                fetch('/faucet', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(faucetPayload)
                })
                .then(response => response.json())
                .then(data => {
                    console.log('Response from /faucet:', data);
                    // Process and log the /faucet response. Do not update the UI with this response.
        
                    // Start polling the /retrieve endpoint
                    const startTime = Date.now();
                    const pollInterval = setInterval(async () => {
                        const retrieveResponse = await fetch('/retrieve', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                            },
                            body: JSON.stringify({ client_public_key: this.clientPublicKey })
                        });
        
                        if (retrieveResponse.ok) {
                            const responseData = await retrieveResponse.json();
                            const decryptedChatHistory = await this.decrypt(
                                responseData.chat_history,
                                responseData.cipherkey,
                                responseData.iv,
                                this.clientPrivateKey
                            );
                            console.log('Decrypted chat history from /retrieve:', JSON.parse(decryptedChatHistory));
                            clearInterval(pollInterval);
                        } else {
                            console.error('Error retrieving response:', retrieveResponse.status);
                        }
        
                        // Check for timeout
                        if (Date.now() - startTime > 60000) {
                            clearInterval(pollInterval);
                            console.log('Timeout reached while polling /retrieve endpoint');
                        }
                    }, 2000);
                })
                .catch((error) => {
                    console.error('Error sending message to /faucet:', error);
                });
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