new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: []
    },
    methods: {
        sendMessage() {
            const messageContent = this.newMessage.trim();
            if (messageContent) {
                this.newMessage = ''; // Clear the input field
                this.chatHistory.push({ role: 'user', content: messageContent }); // Add user message to chat history

                // Send just the new message to the server
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
                        console.log('Response:', JSON.stringify(data, null, 2));
                        this.chatHistory = data;
                    } else {
                        console.error('Unexpected response format:', data);
                    }
                })
                .catch((error) => {
                    console.error('Error:', error);
                });
            }
        }
    },
    updated() {
        // Scroll to the bottom of the chat container every time the chatHistory updates
        this.$nextTick(() => {
            const container = this.$el.querySelector(".chat-container");
            container.scrollTop = container.scrollHeight;
        });
    }
});
