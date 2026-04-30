new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        serverPublicKey: null,
        clientPrivateKey: null,
        clientPublicKey: null,
        isGeneratingResponse: false,
        isTouchInput: false,
        relayApiV1NonStreaming: true
    },
    mounted() {
        this.detectTouchInput();
        this.getServerPublicKey().then(() => {
            this.generateClientKeys();
        });
        this.$nextTick(() => {
            this.adjustMessageInputHeight();
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
        normalizeServerPublicKey(rawKey) {
            if (typeof rawKey !== 'string') {
                return null;
            }

            const trimmed = rawKey.trim();
            if (!trimmed) {
                return null;
            }

            if (trimmed.includes('-----BEGIN')) {
                return trimmed;
            }

            try {
                const cleanedBase64 = trimmed.replace(/\s+/g, '');
                const decoded = atob(cleanedBase64).trim();
                if (decoded.includes('-----BEGIN')) {
                    return decoded;
                }
            } catch (error) {
                console.warn('Server public key is not Base64-encoded PEM:', error);
            }

            return null;
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
                    const normalizedKey = this.normalizeServerPublicKey(data && data.public_key);
                    if (normalizedKey) {
                        this.serverPublicKey = normalizedKey;
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

        wordArrayToUint8Array(wordArray) {
            if (!wordArray || typeof wordArray.sigBytes !== 'number') {
                return new Uint8Array();
            }
            const { words, sigBytes } = wordArray;
            const buffer = new Uint8Array(sigBytes);
            for (let i = 0; i < sigBytes; i++) {
                buffer[i] = (words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
            }
            return buffer;
        },

        encodeClientPublicKeyForApi() {
            if (typeof this.clientPublicKey !== 'string' || !this.clientPublicKey.trim()) {
                throw new Error('Client public key is unavailable');
            }
            return btoa(this.clientPublicKey);
        },

        escapeHtml(value) {
            if (value === null || value === undefined) {
                return '';
            }

            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        },

        renderMarkdown(content) {
            if (content === null || content === undefined) {
                return '';
            }

            const raw = typeof content === 'string' ? content : String(content);
            const codeBlocks = [];
            let escaped = this.escapeHtml(raw);

            escaped = escaped.replace(/```([\s\S]*?)```/g, (_, code) => {
                const token = `__CODE_BLOCK_${codeBlocks.length}__`;
                codeBlocks.push(`<pre><code>${code.replace(/\r?\n$/, '')}</code></pre>`);
                return token;
            });

            escaped = escaped.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
            escaped = escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
            escaped = escaped.replace(/__(.+?)__/g, '<strong>$1</strong>');
            escaped = escaped.replace(/\*(?!\s)([^*]+?)\*/g, '<em>$1</em>');
            escaped = escaped.replace(/_(?!\s)([^_]+?)_/g, '<em>$1</em>');

            const lines = escaped.split(/\r?\n/);
            const htmlParts = [];
            let listBuffer = [];

            const flushList = () => {
                if (listBuffer.length === 0) {
                    return;
                }
                const items = listBuffer.map((item) => `<li>${item}</li>`).join('');
                htmlParts.push(`<ul>${items}</ul>`);
                listBuffer = [];
            };

            for (const line of lines) {
                const trimmed = line.trim();
                const placeholderMatch = /^__CODE_BLOCK_(\d+)__$/.exec(trimmed);
                if (placeholderMatch) {
                    flushList();
                    const idx = Number(placeholderMatch[1]);
                    htmlParts.push(codeBlocks[idx] || '');
                    continue;
                }

                const listMatch = /^[-*]\s+(.*)$/.exec(trimmed);
                if (listMatch) {
                    listBuffer.push(listMatch[1]);
                    continue;
                }

                flushList();

                if (trimmed.length === 0) {
                    htmlParts.push('<br>');
                    continue;
                }

                htmlParts.push(trimmed);
            }

            flushList();

            let html = htmlParts.join('<br>');
            html = html.replace(/(<br>)+/g, '<br>');
            html = html.replace(/<br><ul>/g, '<ul>');
            html = html.replace(/<\/ul><br>/g, '</ul>');
            html = html.replace(/<br><pre>/g, '<pre>');
            html = html.replace(/<\/pre><br>/g, '</pre>');

            return html;
        },

        handleMessageKeydown(event) {
            if (event.key !== 'Enter') {
                return;
            }

            if (event.isComposing || event.keyCode === 229) {
                return;
            }

            if (event.shiftKey) {
                return;
            }

            if (event.altKey || event.ctrlKey || event.metaKey) {
                return;
            }

            if (this.isTouchInput) {
                return;
            }

            event.preventDefault();
            this.sendMessage();
        },

        handleMessageInput(event) {
            const target = event && event.target ? event.target : undefined;
            this.adjustMessageInputHeight(target);
        },

        adjustMessageInputHeight(target) {
            const textarea = target || this.$refs.messageInput;

            if (!textarea) {
                return;
            }

            textarea.style.height = 'auto';
            const minHeight = 48;
            const maxHeight = 240;
            const boundedHeight = Math.max(minHeight, Math.min(textarea.scrollHeight, maxHeight));
            textarea.style.height = `${boundedHeight}px`;
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
                    client_public_key: this.encodeClientPublicKeyForApi(),
                    messages: encryptedData,
                    metadata: {
                        tokenplace_execution_target: 'desktop_bridge_api_v1_e2ee'
                    }
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
                    let errorData = null;
                    try {
                        errorData = await response.json();
                    } catch (_jsonError) {
                        errorData = null;
                    }
                    return {
                        error: {
                            userMessage: this.getUserFacingApiError(errorData)
                        }
                    };
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


        calculateTypingChunkSize(content) {
            if (!content || typeof content !== 'string') {
                return 1;
            }
            const length = content.length;
            if (length <= 24) {
                return 1;
            }
            if (length <= 96) {
                return 2;
            }
            if (length <= 180) {
                return 3;
            }
            return Math.min(6, Math.ceil(length / 48));
        },

        appendAssistantMessage(message) {
            if (!message || typeof message !== 'object') {
                return;
            }

            // Relay-path landing chat in v0.1.0 is API v1-only and non-streaming.
            // Keep this branch explicit so future edits cannot silently reintroduce
            // incremental character streaming in the UI.
            if (this.relayApiV1NonStreaming !== true) {
                console.warn('relayApiV1NonStreaming is disabled; forcing atomic render fallback.');
            }

            const entry = Object.assign({}, message, { isTyping: false });
            this.chatHistory.push(entry);
        },

        getDisplayContent(message) {
            if (!message || typeof message !== 'object') {
                return '';
            }

            if (typeof message.displayContent === 'string') {
                return message.displayContent;
            }

            return message.content;
        },

        getUserFacingApiError(errorPayload) {
            const error = errorPayload && typeof errorPayload === 'object' ? errorPayload.error : null;
            const errorCode = error && typeof error.code === 'string' ? error.code : '';
            const fallbackMessage = 'Sorry, I encountered an issue generating a response. Please try again.';

            const codeToMessage = {
                no_registered_compute_nodes: 'No LLM servers are available right now.',
                compute_node_timeout: 'The LLM server took too long to respond. Please try again.',
                compute_node_bridge_timeout: 'The LLM server took too long to respond. Please try again.',
                compute_node_unreachable: 'The LLM server is unavailable right now. Please try again.',
                compute_node_bridge_error: 'Unable to contact the LLM server right now. Please try again.',
                compute_node_invalid_payload: 'The LLM server returned an invalid response. Please try again.',
                distributed_mode_required: 'Desktop bridge mode requires a reachable relay compute node.'
            };

            return codeToMessage[errorCode] || fallbackMessage;
        },

        isInvalidAssistantContent(content) {
            if (typeof content !== 'string') {
                return true;
            }
            const normalized = content.trim();
            if (!normalized) {
                return true;
            }
            if (normalized.toLowerCase() === 'stub') {
                return true;
            }
            return normalized === 'Sorry, I encountered an issue generating a response. Please try again.';
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
            this.$nextTick(() => {
                this.adjustMessageInputHeight();
            });

            try {
                // Relay-path landing chat in v0.1.0 is API v1-only and non-streaming.
                let response = await this.sendMessageApi();

                // Process the response
                if (response) {
                    if (response.error && typeof response.error.userMessage === 'string') {
                        this.chatHistory.push({
                            role: 'assistant',
                            content: response.error.userMessage
                        });
                    }
                    // For API response, extract last message
                    else if (response.choices && response.choices.length > 0) {
                        const assistantMessage = response.choices[0].message;
                        if (!assistantMessage || this.isInvalidAssistantContent(assistantMessage.content)) {
                            throw new Error('Invalid assistant response content for desktop bridge mode');
                        }
                        this.appendAssistantMessage(assistantMessage);
                    }
                    // For legacy response format (full chat history)
                    else if (Array.isArray(response)) {
                        const history = response.slice();
                        const candidate = history.length > 0 ? history[history.length - 1] : null;
                        if (candidate && candidate.role === 'assistant' && typeof candidate.content === 'string') {
                            history.pop();
                            this.chatHistory = history;
                            this.appendAssistantMessage(candidate);
                        } else {
                            this.chatHistory = response;
                        }
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
    beforeDestroy() {
        if (!Array.isArray(this.chatHistory)) {
            return;
        }
        this.chatHistory.forEach((entry) => {
            if (entry && entry._animator && typeof entry._animator.cancel === 'function') {
                entry._animator.cancel();
            }
        });
    },
    updated() {
        this.$nextTick(() => {
            const container = this.$el.querySelector(".chat-container");
            container.scrollTop = container.scrollHeight;
        });
    }
});
