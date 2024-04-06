new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        serverPublicKey: null // Store the server public key
    },
    mounted() {
        this.getServerPublicKey();
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
