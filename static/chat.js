new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        serverPublicKey: null,
        privateKey: null,
        publicKey: null
    },
    async mounted() {
        await this.generateKeys();
        this.getServerPublicKey();
    },
    methods: {
        async generateKeys() {
            const { pemPrivateKey, pemPublicKey } = await generateKeys();
            this.privateKey = pemPrivateKey;
            this.publicKey = pemPublicKey;
        },
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
        async sendMessage() {
            const messageContent = this.newMessage.trim();
            if (messageContent && this.serverPublicKey) {
                this.chatHistory.push({ role: 'user', content: messageContent });
                this.newMessage = '';

                const plaintextBytes = new TextEncoder().encode(JSON.stringify([{ role: 'user', content: messageContent }]));
                const { iv, ciphertext, encryptedKey } = await encrypt(plaintextBytes, this.serverPublicKey);

                const faucetPayload = {
                    client_public_key: this.publicKey,
                    server_public_key: this.serverPublicKey,
                    chat_history: btoa(String.fromCharCode.apply(null, new Uint8Array(ciphertext))),
                    cipherkey: btoa(String.fromCharCode.apply(null, new Uint8Array(encryptedKey))),
                    iv: btoa(String.fromCharCode.apply(null, new Uint8Array(iv)))
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
                })
                .catch((error) => {
                    console.error('Error sending message to /faucet:', error);
                });

                // Poll for the response
                const decryptedResponse = await this.pollForResponse(encryptedKey);
                if (decryptedResponse) {
                    this.chatHistory = decryptedResponse;
                }
            }
        },
        async pollForResponse(encryptedKey) {
            const maxAttempts = 30;
            const delay = 2000;

            for (let i = 0; i < maxAttempts; i++) {
                const response = await fetch('/retrieve', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ client_public_key: this.publicKey }),
                });

                if (response.ok) {
                    const data = await response.json();
                    if (data.chat_history && data.iv && data.cipherkey) {
                        const decryptedResponse = await decrypt(
                            data.chat_history,
                            data.cipherkey,
                            data.iv,
                            this.privateKey
                        );
                        return JSON.parse(decryptedResponse);
                    }
                }

                await new Promise(resolve => setTimeout(resolve, delay));
            }

            console.error('Timeout while waiting for response');
            return null;
        }
    },
    updated() {
        this.$nextTick(() => {
            const container = this.$el.querySelector(".chat-container");
            container.scrollTop = container.scrollHeight;
        });
    }
});