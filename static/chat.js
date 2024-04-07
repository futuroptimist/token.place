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
        this.getServerPublicKey();
        this.generateClientKeys();
    },
    methods: {
        getServerPublicKey() {
            fetch('/next_server')
            .then(response => response.json())
            .then(data => {
                if (data && data.server_public_key) {
                    this.serverPublicKey = data.server_public_key;
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
            const plainTextBytes = encoder.encode(plaintext);

            const key = await crypto.subtle.generateKey({ name: 'AES-CBC', length: 256 }, true, ['encrypt', 'decrypt']);
            const iv = crypto.getRandomValues(new Uint8Array(16));

            const publicKeyImported = await crypto.subtle.importKey(
                'spki',
                this.str2ab(atob(publicKey)),
                { name: 'RSA-OAEP', hash: 'SHA-256' },
                false,
                ['encrypt']
            );

            const encryptedKey = await crypto.subtle.encrypt(
                { name: 'RSA-OAEP' },
                publicKeyImported,
                await crypto.subtle.exportKey('raw', key)
            );

            const cipherText = await crypto.subtle.encrypt(
                { name: 'AES-CBC', iv },
                key,
                plainTextBytes
            );

            return {
                cipherText: this.ab2str(cipherText),
                cipherKey: this.ab2str(encryptedKey),
                iv: this.ab2str(iv)
            };
        },
        ab2str(buffer) {
            return btoa(String.fromCharCode.apply(null, new Uint8Array(buffer)));
        },

        str2ab(str) {
            const buf = new ArrayBuffer(str.length);
            const bufView = new Uint8Array(buf);
            for (let i = 0, strLen = str.length; i < strLen; i++) {
                bufView[i] = str.charCodeAt(i);
            }
            return buf;
        },
        async decrypt(cipherText, cipherKey, iv, privateKey) {
            const privateKeyImported = await crypto.subtle.importKey(
                'pkcs8',
                this.str2ab(atob(privateKey)),
                { name: 'RSA-OAEP', hash: 'SHA-256' },
                false,
                ['decrypt']
            );

            const decryptedKey = await crypto.subtle.decrypt(
                { name: 'RSA-OAEP' },
                privateKeyImported,
                this.str2ab(cipherKey)
            );

            const key = await crypto.subtle.importKey(
                'raw',
                decryptedKey,
                { name: 'AES-CBC', length: 256 },
                false,
                ['decrypt']
            );

            const decryptedBytes = await crypto.subtle.decrypt(
                { name: 'AES-CBC', iv: this.str2ab(iv) },
                key,
                this.str2ab(cipherText)
            );

            const decoder = new TextDecoder();
            return decoder.decode(decryptedBytes);
        },
        sendMessage() {
            const messageContent = this.newMessage.trim();
            if (messageContent && this.serverPublicKey) {
                this.chatHistory.push({ role: 'user', content: messageContent }); // Display user's message immediately
                this.newMessage = ''; // Clear the input field after sending
                
                // Payload for the /faucet endpoint
                const faucetPayload = {
                    server_public_key: this.serverPublicKey,
                    chat_history: JSON.stringify([{ role: 'user', content: messageContent }])
                };
                
                // Send the message to the /faucet endpoint
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
                })
                .catch((error) => {
                    console.error('Error sending message to /faucet:', error);
                });

                // Original message sending logic to the /inference (or another) endpoint
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
                        this.chatHistory = data; // Update UI with the response from the original endpoint
                    } else {
                        console.error('Unexpected response format from /inference:', data);
                    }
                })
                .catch((error) => {
                    console.error('Error sending message to /inference:', error);
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
