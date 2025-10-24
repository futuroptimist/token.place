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
        activeStreamController: null
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

        notifyStreamingRecorder(payload, options = {}) {
            if (typeof window === 'undefined') {
                return;
            }
            const hook = window.__tokenPlaceStreamingRecorder;
            if (!hook || typeof hook !== 'object') {
                return;
            }
            try {
                if (options.error && typeof hook.error === 'function') {
                    hook.error(String(options.error));
                }
                if (options.done && typeof hook.complete === 'function') {
                    hook.complete(payload ?? '');
                    if (Array.isArray(hook.events)) {
                        hook.events.push(payload ?? '');
                    }
                    hook.done = true;
                    return;
                }
                if (typeof hook.record === 'function') {
                    hook.record(payload ?? '');
                } else if (Array.isArray(hook.events)) {
                    hook.events.push(payload ?? '');
                }
            } catch (error) {
                console.warn('Streaming recorder hook failed:', error);
            }
        },

        // Extract the Base64 content from PEM format
        extractBase64(pemString) {
            return pemString
                .replace(/-----BEGIN.*?-----/, '')
                .replace(/-----END.*?-----/, '')
                .replace(/\s/g, '');
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

        createStreamDecryptor() {
            const jsEncrypt = new JSEncrypt();
            jsEncrypt.setPrivateKey(this.clientPrivateKey);

            const state = {
                mode: null,
                aesKey: null,
                associatedDataB64: null,
                usedIvs: new Set(),
                gcmKeyPromise: null
            };

            const decodeBase64Buffer = (value, fieldName) => {
                if (typeof value !== 'string' || !value) {
                    console.error(`Streaming chunk missing ${fieldName}`);
                    return null;
                }
                const buffer = this.base64ToArrayBuffer(value);
                if (!buffer) {
                    console.error(`Failed to decode ${fieldName} for streaming chunk`);
                    return null;
                }
                return buffer;
            };

            const ensureGcmKey = async () => {
                if (state.mode !== 'GCM') {
                    return null;
                }
                if (!state.gcmKeyPromise) {
                    if (typeof window === 'undefined' || !window.crypto || !window.crypto.subtle) {
                        throw new Error('Web Crypto API not available for AES-GCM');
                    }
                    const rawKey = this.wordArrayToUint8Array(state.aesKey);
                    state.gcmKeyPromise = window.crypto.subtle.importKey(
                        'raw',
                        rawKey,
                        { name: 'AES-GCM' },
                        false,
                        ['decrypt']
                    );
                }
                return state.gcmKeyPromise;
            };

            const parseDecryptedText = (text) => {
                if (typeof text !== 'string') {
                    return text;
                }
                try {
                    return JSON.parse(text);
                } catch (_) {
                    return text;
                }
            };

            const normaliseMode = (value) => {
                if (typeof value === 'string' && value.trim()) {
                    return value.trim().toUpperCase();
                }
                return 'CBC';
            };

            const decryptGcmChunk = async (payload) => {
                const ciphertextBuffer = decodeBase64Buffer(payload.ciphertext, 'ciphertext');
                const ivBuffer = decodeBase64Buffer(payload.iv, 'iv');
                const tagBuffer = decodeBase64Buffer(payload.tag, 'tag');
                if (!ciphertextBuffer || !ivBuffer || !tagBuffer) {
                    return null;
                }
                const cryptoKey = await ensureGcmKey();
                if (!cryptoKey) {
                    return null;
                }
                const combined = new Uint8Array(ciphertextBuffer.byteLength + tagBuffer.byteLength);
                combined.set(new Uint8Array(ciphertextBuffer), 0);
                combined.set(new Uint8Array(tagBuffer), ciphertextBuffer.byteLength);

                let additionalData;
                const associatedDataB64 = typeof payload.associated_data === 'string'
                    ? payload.associated_data
                    : state.associatedDataB64;
                if (typeof associatedDataB64 === 'string') {
                    const buffer = decodeBase64Buffer(associatedDataB64, 'associated_data');
                    if (!buffer) {
                        return null;
                    }
                    additionalData = new Uint8Array(buffer);
                }

                try {
                    const decryptedBuffer = await window.crypto.subtle.decrypt(
                        {
                            name: 'AES-GCM',
                            iv: new Uint8Array(ivBuffer),
                            additionalData: additionalData,
                            tagLength: 128
                        },
                        cryptoKey,
                        combined
                    );
                    const decoded = new TextDecoder().decode(decryptedBuffer);
                    return parseDecryptedText(decoded);
                } catch (error) {
                    console.error('AES-GCM decryption failed', error);
                    return null;
                }
            };

            const decryptCbcChunk = (payload) => {
                const ciphertextWordArray = CryptoJS.enc.Base64.parse(payload.ciphertext);
                const ivWordArray = CryptoJS.enc.Base64.parse(payload.iv);
                const decrypted = CryptoJS.AES.decrypt(
                    { ciphertext: ciphertextWordArray },
                    state.aesKey,
                    {
                        iv: ivWordArray,
                        mode: CryptoJS.mode.CBC,
                        padding: CryptoJS.pad.Pkcs7
                    }
                );
                const text = CryptoJS.enc.Utf8.stringify(decrypted);
                return parseDecryptedText(text);
            };

            return {
                async decrypt(payload) {
                    if (!payload || typeof payload !== 'object') {
                        return null;
                    }

                    const mode = normaliseMode(payload.mode || (payload.tag ? 'GCM' : state.mode));
                    const isFirstChunk = !state.aesKey;

                    if (isFirstChunk) {
                        if (typeof payload.cipherkey !== 'string' || !payload.cipherkey) {
                            console.error('Streaming chunk missing cipherkey for first payload');
                            return null;
                        }
                        const decryptedKeyBase64 = jsEncrypt.decrypt(payload.cipherkey);
                        if (!decryptedKeyBase64) {
                            console.error('Failed to decrypt streaming cipher key');
                            return null;
                        }
                        state.aesKey = CryptoJS.enc.Base64.parse(decryptedKeyBase64);
                        state.mode = mode;
                        state.usedIvs.clear();
                        state.associatedDataB64 = typeof payload.associated_data === 'string'
                            ? payload.associated_data
                            : null;
                        state.gcmKeyPromise = null;
                    } else {
                        if (typeof payload.cipherkey === 'string' && payload.cipherkey) {
                            console.warn('Ignoring unexpected cipherkey on subsequent streaming chunk');
                        }
                        if (mode !== state.mode) {
                            console.error('Streaming cipher mode mismatch');
                            return null;
                        }
                        if (state.associatedDataB64 !== null) {
                            const incoming = typeof payload.associated_data === 'string'
                                ? payload.associated_data
                                : null;
                            if (incoming !== null && incoming !== state.associatedDataB64) {
                                console.error('Streaming associated_data mismatch');
                                return null;
                            }
                        }
                    }

                    if (typeof payload.iv !== 'string' || typeof payload.ciphertext !== 'string') {
                        console.error('Streaming chunk missing ciphertext or iv');
                        return null;
                    }

                    if (state.usedIvs.has(payload.iv)) {
                        console.error('Repeated IV detected in streaming payload');
                        return null;
                    }
                    state.usedIvs.add(payload.iv);

                    if (state.mode === 'GCM') {
                        if (typeof payload.tag !== 'string') {
                            console.error('AES-GCM streaming chunk missing authentication tag');
                            return null;
                        }
                        return decryptGcmChunk(payload);
                    }

                    return decryptCbcChunk(payload);
                },

                reset() {
                    state.mode = null;
                    state.aesKey = null;
                    state.associatedDataB64 = null;
                    state.usedIvs.clear();
                    state.gcmKeyPromise = null;
                }
            };
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

            const content = message.content;
            const typingFactory = typeof ChatTypingEffect !== 'undefined'
                && ChatTypingEffect
                && typeof ChatTypingEffect.createTypingAnimator === 'function';

            const shouldAnimate = typingFactory && typeof content === 'string' && content.trim().length > 0;

            if (!shouldAnimate) {
                const entry = Object.assign({}, message, { isTyping: false });
                this.chatHistory.push(entry);
                return;
            }

            const finalText = content;
            const entry = Object.assign({}, message, {
                content: finalText,
                displayContent: '',
                isTyping: true
            });
            const chunkSize = this.calculateTypingChunkSize(finalText);

            const animator = ChatTypingEffect.createTypingAnimator({
                fullText: finalText,
                chunkSize,
                onUpdate: (partial) => {
                    entry.displayContent = partial;
                },
                onComplete: () => {
                    entry.isTyping = false;
                    if (entry._animator && typeof entry._animator.cancel === 'function') {
                        entry._animator.cancel();
                    }
                    delete entry._animator;
                    if (typeof this.$delete === 'function') {
                        this.$delete(entry, 'displayContent');
                    } else {
                        delete entry.displayContent;
                    }
                },
                schedule: (fn, delay) => {
                    if (typeof window !== 'undefined' && typeof window.setTimeout === 'function') {
                        return window.setTimeout(fn, delay);
                    }
                    return setTimeout(fn, delay);
                },
                cancelScheduled: (id) => {
                    if (typeof window !== 'undefined' && typeof window.clearTimeout === 'function') {
                        window.clearTimeout(id);
                    } else {
                        clearTimeout(id);
                    }
                }
            });

            entry._animator = animator;
            this.chatHistory.push(entry);

            this.$nextTick(() => {
                animator.start();
            });
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

        extractTextFromChunk(data) {
            const parts = [];
            const visit = (value) => {
                if (value === null || value === undefined) {
                    return;
                }
                if (typeof value === 'string') {
                    if (value.length > 0) {
                        parts.push(value);
                    }
                    return;
                }
                if (Array.isArray(value)) {
                    value.forEach(visit);
                    return;
                }
                if (typeof value === 'object') {
                    if (typeof value.content === 'string') {
                        visit(value.content);
                    }
                    if (typeof value.text === 'string') {
                        visit(value.text);
                    }
                    if (typeof value.delta === 'object') {
                        visit(value.delta);
                    }
                    if (typeof value.message === 'object') {
                        visit(value.message);
                    }
                    if (value.data !== undefined) {
                        visit(value.data);
                    }
                    if (Array.isArray(value.choices)) {
                        value.choices.forEach(visit);
                    }
                }
            };
            visit(data);
            return parts.join('');
        },

        extractFinishReason(data) {
            if (!data || typeof data !== 'object') {
                return null;
            }
            if (typeof data.finish_reason === 'string') {
                return data.finish_reason;
            }
            const choices = Array.isArray(data.choices) ? data.choices : [];
            for (const choice of choices) {
                if (!choice || typeof choice !== 'object') {
                    continue;
                }
                if (typeof choice.finish_reason === 'string') {
                    return choice.finish_reason;
                }
                if (choice.delta && typeof choice.delta.finish_reason === 'string') {
                    return choice.delta.finish_reason;
                }
            }
            return null;
        },

        applyStreamingDelta(entry, eventName, payload) {
            if (!entry) {
                return { appended: false, finished: false };
            }

            if (eventName === 'error') {
                const reason = typeof payload === 'string'
                    ? payload
                    : (payload && payload.message) || 'Unknown streaming error';
                throw new Error(reason);
            }

            let appended = false;
            const current = typeof entry.displayContent === 'string'
                ? entry.displayContent
                : (entry.content || '');

            if (typeof payload === 'string') {
                if (payload.length > 0) {
                    const nextText = current + payload;
                    entry.displayContent = nextText;
                    entry.content = nextText;
                    this.notifyStreamingRecorder(nextText);
                    appended = true;
                }
            } else if (payload && typeof payload === 'object') {
                const text = this.extractTextFromChunk(payload);
                if (text) {
                    const nextText = current + text;
                    entry.displayContent = nextText;
                    entry.content = nextText;
                    this.notifyStreamingRecorder(nextText);
                    appended = true;
                }
            }

            if (appended) {
                this.$nextTick(() => {
                    if (!this.$el || typeof this.$el.querySelector !== 'function') {
                        return;
                    }
                    const container = this.$el.querySelector('.chat-container');
                    if (container) {
                        container.scrollTop = container.scrollHeight;
                    }
                });
            }

            const finishReason = this.extractFinishReason(payload);
            if (finishReason && !entry.finish_reason) {
                entry.finish_reason = finishReason;
            }

            return { appended, finished: Boolean(finishReason) };
        },

        async processStreamingResponse(historySnapshot, entry, controller) {
            if (!Array.isArray(historySnapshot) || historySnapshot.length === 0) {
                return false;
            }
            if (!this.serverPublicKey || !this.clientPublicKey) {
                return false;
            }

            try {
                const encryptedHistory = await this.encrypt(
                    JSON.stringify(historySnapshot),
                    this.serverPublicKey
                );

                if (!encryptedHistory) {
                    throw new Error('Failed to encrypt chat history for streaming request');
                }

                const payload = {
                    model: 'llama-3-8b-instruct',
                    encrypted: true,
                    stream: true,
                    client_public_key: this.extractBase64(this.clientPublicKey),
                    messages: encryptedHistory
                };

                const fetchOptions = {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                };

                if (controller && controller.signal) {
                    fetchOptions.signal = controller.signal;
                }

                const response = await fetch('/api/v2/chat/completions', fetchOptions);

                if (!response.ok) {
                    let errorMessage = `Streaming request failed with status ${response.status}`;
                    try {
                        const errorPayload = await response.json();
                        if (errorPayload && errorPayload.error && errorPayload.error.message) {
                            errorMessage = errorPayload.error.message;
                        }
                    } catch (_) {
                        // ignore JSON parse failures
                    }
                    throw new Error(errorMessage);
                }

                const contentType = (response.headers.get('Content-Type') || '').toLowerCase();
                if (!contentType.includes('text/event-stream')) {
                    try {
                        const fallbackData = await response.json();
                        if (
                            fallbackData &&
                            fallbackData.choices &&
                            fallbackData.choices.length > 0 &&
                            fallbackData.choices[0].message &&
                            typeof fallbackData.choices[0].message.content === 'string'
                        ) {
                            entry.isStreaming = false;
                            entry.displayContent = fallbackData.choices[0].message.content;
                            entry.content = entry.displayContent;
                            this.notifyStreamingRecorder(entry.content, { done: true });
                            if (typeof this.$delete === 'function') {
                                this.$delete(entry, 'displayContent');
                            } else {
                                delete entry.displayContent;
                            }
                            return true;
                        }
                    } catch (error) {
                        console.warn('Failed to parse non-streaming response:', error);
                    }
                    return false;
                }

                if (!response.body || typeof response.body.getReader !== 'function') {
                    throw new Error('Streaming response body unavailable');
                }

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                const decryptor = this.createStreamDecryptor();
                let buffer = '';
                let sawContent = false;
                let streamComplete = false;

                const consumeDataString = async (dataStr, eventHint) => {
                    if (!dataStr) {
                        return;
                    }
                    if (dataStr === '[DONE]') {
                        streamComplete = true;
                        return;
                    }

                    let payload;
                    try {
                        payload = JSON.parse(dataStr);
                    } catch (_) {
                        const nextText = (entry.displayContent || entry.content || '') + dataStr;
                        entry.displayContent = nextText;
                        entry.content = nextText;
                        this.notifyStreamingRecorder(nextText);
                        sawContent = true;
                        return;
                    }

                    const eventName = (payload && typeof payload.event === 'string' && payload.event)
                        ? payload.event
                        : (eventHint || 'chunk');

                    let decryptedPayload = null;
                    if (payload && payload.encrypted === true) {
                        decryptedPayload = await decryptor.decrypt(payload.data || {});
                    } else if (
                        payload &&
                        payload.data &&
                        payload.data.encrypted === true
                    ) {
                        const encryptedBody = payload.data.data || payload.data;
                        decryptedPayload = await decryptor.decrypt(encryptedBody || {});
                    }

                    if (decryptedPayload !== null) {
                        const { appended } = this.applyStreamingDelta(entry, eventName, decryptedPayload);
                        sawContent = sawContent || appended;
                        return;
                    }

                    const payloadData = payload && payload.data !== undefined
                        ? payload.data
                        : payload;
                    const { appended } = this.applyStreamingDelta(entry, eventName, payloadData);
                    sawContent = sawContent || appended;
                };

                while (!streamComplete) {
                    const { value, done } = await reader.read();
                    if (done) {
                        break;
                    }
                    buffer += decoder.decode(value, { stream: true });

                    let boundary = buffer.indexOf('\n\n');
                    while (boundary !== -1) {
                        const rawEvent = buffer.slice(0, boundary);
                        buffer = buffer.slice(boundary + 2);

                        const lines = rawEvent.split('\n');
                        const dataLines = [];
                        let eventHint = null;
                        for (const line of lines) {
                            if (line.startsWith('data:')) {
                                dataLines.push(line.slice(5).trim());
                            } else if (line.startsWith('event:')) {
                                eventHint = line.slice(6).trim();
                            }
                        }

                        const dataStr = dataLines.join('\n');
                        if (dataStr) {
                            await consumeDataString(dataStr, eventHint);
                        }

                        if (streamComplete) {
                            break;
                        }

                        boundary = buffer.indexOf('\n\n');
                    }
                }

                entry.isStreaming = false;
                if (typeof entry.displayContent === 'string') {
                    entry.content = entry.displayContent;
                }
                this.notifyStreamingRecorder(entry.content || '', { done: true });
                if (typeof this.$delete === 'function' && entry.displayContent !== undefined) {
                    this.$delete(entry, 'displayContent');
                } else {
                    delete entry.displayContent;
                }
                return sawContent;
            } catch (error) {
                console.error('Streaming chat completion failed:', error);
                this.notifyStreamingRecorder('', { error: error });
                return false;
            }
        },

        async sendStreamingMessage(historySnapshot) {
            const assistantEntry = {
                role: 'assistant',
                content: '',
                displayContent: '',
                isStreaming: true
            };

            this.chatHistory.push(assistantEntry);

            const controller = typeof AbortController !== 'undefined'
                ? new AbortController()
                : null;
            this.activeStreamController = controller;

            try {
                const streamed = await this.processStreamingResponse(
                    historySnapshot,
                    assistantEntry,
                    controller
                );

                if (!streamed) {
                    const index = this.chatHistory.indexOf(assistantEntry);
                    if (index !== -1) {
                        this.chatHistory.splice(index, 1);
                    }
                    return false;
                }

                return true;
            } finally {
                if (this.activeStreamController === controller) {
                    this.activeStreamController = null;
                }
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
            this.$nextTick(() => {
                this.adjustMessageInputHeight();
            });

            try {
                const historySnapshot = this.chatHistory.slice();
                const streamed = await this.sendStreamingMessage(historySnapshot);
                if (streamed) {
                    return;
                }

                // Send the message via the API
                let response = await this.sendMessageApi();

                // Process the response
                if (response) {
                    // For API response, extract last message
                    if (response.choices && response.choices.length > 0) {
                        const assistantMessage = response.choices[0].message;
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
