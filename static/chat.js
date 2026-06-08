const ASSISTANT_GENERIC_FALLBACK_MESSAGE = 'Sorry, I encountered an issue generating a response. Please try again.';
const ASSISTANT_INVALID_RELAY_RESPONSE_MESSAGE = 'Sorry, the relay returned an invalid response. Please try again.';
const COMPUTE_NODE_COUNT_POLL_INTERVAL_MS = 30000;
const EMERGENCY_MODEL_FALLBACK_ID = 'llama-3-8b-instruct';
const RELAY_E2EE_PROTOCOL = 'tokenplace_api_v1_relay_e2ee';
const RELAY_RESPONSE_POLL_INTERVAL_MS = 500;
const RELAY_RESPONSE_TIMEOUT_MS = 30000;

new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        selectedServerPublicKeyB64: null,
        selectedServerPublicKey: null,
        selectedServerKeyLabel: '',
        clientPrivateKey: null,
        clientPublicKey: null,
        availableModels: [],
        selectedModelId: '',
        modelsLoading: false,
        modelsError: '',
        isGeneratingResponse: false,
        isTouchInput: false,
        relayApiV1NonStreaming: true,
        computeNodeCount: null,
        computeNodeCountStatus: 'loading',
        computeNodeCountLastUpdated: '',
        computeNodeCountPoller: null,
        computeNodeCountRequestId: 0
    },
    mounted() {
        this.detectTouchInput();
        this.fetchModels();
        this.generateClientKeys();
        this.refreshComputeNodeCount();
        this.computeNodeCountPoller = setInterval(() => {
            this.refreshComputeNodeCount();
        }, COMPUTE_NODE_COUNT_POLL_INTERVAL_MS);
        this.$nextTick(() => {
            this.adjustMessageInputHeight();
        });
    },
    computed: {
        computeNodeCountLabel() {
            if (this.computeNodeCountStatus === 'loading') {
                return 'Live compute nodes: loading…';
            }
            if (this.computeNodeCountStatus === 'error') {
                return 'Live compute nodes: unavailable';
            }
            return `Live compute nodes: ${this.computeNodeCount}`;
        },
        selectedModel() {
            if (!Array.isArray(this.availableModels)) {
                return null;
            }
            const catalogueModel = this.availableModels.find((model) => model && model.id === this.selectedModelId) || null;
            if (catalogueModel) {
                return catalogueModel;
            }
            if (this.modelsError && this.selectedModelId === EMERGENCY_MODEL_FALLBACK_ID) {
                return {
                    id: EMERGENCY_MODEL_FALLBACK_ID,
                    object: 'model',
                    owned_by: 'emergency-fallback',
                    root: EMERGENCY_MODEL_FALLBACK_ID
                };
            }
            return null;
        },
        selectedModelSummary() {
            const model = this.selectedModel;
            if (!model) {
                return '';
            }
            const fields = [];
            if (model.owned_by) {
                fields.push(`owned by ${model.owned_by}`);
            }
            if (model.root && model.root !== model.id) {
                fields.push(`root ${model.root}`);
            }
            return fields.join(' · ');
        },
        hasClientKeypair() {
            return Boolean(this.clientPrivateKey && this.clientPublicKey);
        },
        hasSelectedServerPublicKey() {
            return Boolean(this.selectedServerPublicKeyB64 && this.selectedServerPublicKey);
        },
        canSendMessage() {
            return Boolean(
                this.newMessage.trim() &&
                this.hasClientKeypair &&
                this.selectedModel &&
                !this.isGeneratingResponse
            );
        }
    },
    methods: {
        async refreshComputeNodeCount() {
            const requestId = this.computeNodeCountRequestId + 1;
            this.computeNodeCountRequestId = requestId;

            try {
                const response = await fetch('/relay/diagnostics', { cache: 'no-store' });
                if (requestId !== this.computeNodeCountRequestId) {
                    return;
                }
                if (!response.ok) {
                    throw new Error('Failed to fetch relay diagnostics');
                }
                const data = await response.json();
                if (requestId !== this.computeNodeCountRequestId) {
                    return;
                }
                if (
                    !data ||
                    typeof data !== 'object' ||
                    !Object.prototype.hasOwnProperty.call(data, 'total_api_v1_registered_compute_nodes')
                ) {
                    throw new Error('Relay diagnostics missing API v1 compute-node count');
                }
                const count = data.total_api_v1_registered_compute_nodes;
                if (!Number.isInteger(count) || count < 0) {
                    throw new Error('Relay diagnostics missing API v1 compute-node count');
                }
                this.computeNodeCount = count;
                this.computeNodeCountStatus = 'ready';
                this.computeNodeCountLastUpdated = new Date().toLocaleTimeString([], {
                    hour: '2-digit',
                    minute: '2-digit'
                });
            } catch (error) {
                if (requestId !== this.computeNodeCountRequestId) {
                    return;
                }
                console.warn('Unable to refresh compute-node count:', error);
                this.computeNodeCountStatus = 'error';
                this.computeNodeCountLastUpdated = '';
            }
        },

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
        fetchModels() {
            this.modelsLoading = true;
            this.modelsError = '';

            return fetch('/api/v1/models')
                .then(response => {
                    if (response.ok) {
                        return response.json();
                    }
                    throw new Error('Failed to fetch API v1 models');
                })
                .then(data => {
                    const models = data && Array.isArray(data.data) ? data.data : [];
                    this.availableModels = models.filter((model) => model && typeof model.id === 'string' && model.id);
                    if (this.availableModels.length > 0) {
                        this.selectedModelId = this.availableModels[0].id;
                    } else {
                        this.selectedModelId = '';
                    }
                })
                .catch(error => {
                    console.error('Error fetching API v1 models:', error);
                    this.availableModels = [];
                    this.modelsError = 'Could not load the API v1 model list. Using the emergency API v1 fallback model.';
                    this.selectedModelId = EMERGENCY_MODEL_FALLBACK_ID;
                })
                .finally(() => {
                    this.modelsLoading = false;
                });
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

        encodeServerPublicKeyForRouting(rawKey) {
            if (typeof rawKey !== 'string' || !rawKey.trim()) {
                return null;
            }
            const trimmed = rawKey.trim();
            if (trimmed.includes('-----BEGIN')) {
                return btoa(trimmed);
            }
            return trimmed.replace(/\s+/g, '');
        },

        shortenServerKeyLabel(rawKey) {
            if (typeof rawKey !== 'string' || !rawKey.trim()) {
                return '';
            }
            const compact = rawKey.replace(/-----BEGIN [^-]+-----|-----END [^-]+-----|\s+/g, '');
            if (compact.length <= 16) {
                return 'Server: short-key';
            }
            return `Server: ${compact.slice(0, 8)}…${compact.slice(-8)}`;
        },

        generateRequestId() {
            if (typeof crypto !== 'undefined' && crypto.randomUUID) {
                return crypto.randomUUID();
            }
            const random = CryptoJS.lib.WordArray.random(16).toString(CryptoJS.enc.Hex);
            return `req-${Date.now()}-${random}`;
        },

        delay(ms) {
            return new Promise((resolve) => setTimeout(resolve, ms));
        },

        async ensureSelectedServer() {
            if (this.hasSelectedServerPublicKey) {
                return true;
            }

            const response = await fetch('/api/v1/relay/servers/next', { cache: 'no-store' });
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

            const data = await response.json();
            const rawServerPublicKey = data && data.server_public_key;
            const selectedServerPublicKey = this.normalizeServerPublicKey(rawServerPublicKey);
            const selectedServerPublicKeyB64 = this.encodeServerPublicKeyForRouting(rawServerPublicKey);
            if (!selectedServerPublicKey || !selectedServerPublicKeyB64) {
                throw new Error('Invalid compute-node public key');
            }

            this.selectedServerPublicKeyB64 = selectedServerPublicKeyB64;
            this.selectedServerPublicKey = selectedServerPublicKey;
            this.selectedServerKeyLabel = this.shortenServerKeyLabel(selectedServerPublicKeyB64);
            return true;
        },

        async pollRelayResponse(requestId, clientPublicKeyB64) {
            const startedAt = Date.now();
            while (Date.now() - startedAt < RELAY_RESPONSE_TIMEOUT_MS) {
                const response = await fetch('/api/v1/relay/responses/retrieve', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        client_public_key: clientPublicKeyB64,
                        request_id: requestId
                    })
                });

                if (response.status === 202) {
                    await this.delay(RELAY_RESPONSE_POLL_INTERVAL_MS);
                    continue;
                }

                if (response.ok) {
                    return response.json();
                }

                let errorData = null;
                try {
                    errorData = await response.json();
                } catch (_jsonError) {
                    errorData = null;
                }
                return {
                    error: {
                        userMessage: this.getUserFacingApiError(errorData, response.status)
                    }
                };
            }

            return {
                error: {
                    userMessage: 'The LLM server took too long to respond. Please try again or start a new chat session.'
                }
            };
        },

        validateRelayResponseEnvelope(envelope, requestId, clientPublicKeyB64) {
            if (!envelope || typeof envelope !== 'object') {
                throw new Error('Invalid relay response envelope');
            }
            if (envelope.protocol && envelope.protocol !== RELAY_E2EE_PROTOCOL) {
                throw new Error('Invalid relay response protocol');
            }
            if (envelope.request_id && envelope.request_id !== requestId) {
                throw new Error('Mismatched relay response request_id');
            }
            if (envelope.client_public_key && envelope.client_public_key !== clientPublicKeyB64) {
                throw new Error('Mismatched relay response client key');
            }
            if (!envelope.ciphertext || !envelope.cipherkey || !envelope.iv) {
                throw new Error('Missing relay response ciphertext fields');
            }
        },

        async decryptRelayResponse(envelope, requestId, clientPublicKeyB64) {
            this.validateRelayResponseEnvelope(envelope, requestId, clientPublicKeyB64);
            const decryptedJson = await this.decrypt(envelope.ciphertext, envelope.cipherkey, envelope.iv);
            if (!decryptedJson) {
                throw new Error('Failed to decrypt relay response');
            }
            const decrypted = JSON.parse(decryptedJson);
            if (!decrypted || decrypted.protocol !== RELAY_E2EE_PROTOCOL) {
                throw new Error('Invalid decrypted relay response protocol');
            }
            if (decrypted.request_id !== requestId) {
                throw new Error('Mismatched decrypted relay response request_id');
            }
            if (decrypted.client_public_key !== clientPublicKeyB64) {
                throw new Error('Mismatched decrypted relay response client key');
            }
            if (!decrypted.api_v1_response || !Array.isArray(decrypted.api_v1_response.choices)) {
                throw new Error('Missing decrypted API v1 response');
            }
            return decrypted.api_v1_response;
        },

        async sendMessageApi(messageContent) {
            if (!this.selectedModel) {
                console.error('No API v1 catalogue model selected');
                return null;
            }

            try {
                const selected = await this.ensureSelectedServer();
                if (selected && selected.error) {
                    return selected;
                }

                const clientPublicKeyB64 = this.encodeClientPublicKeyForApi();
                const requestId = this.generateRequestId();
                const plaintextEnvelope = {
                    protocol: RELAY_E2EE_PROTOCOL,
                    version: 1,
                    request_id: requestId,
                    client_public_key: clientPublicKeyB64,
                    api_v1_request: {
                        model: this.selectedModelId,
                        messages: [
                            { role: 'user', content: messageContent }
                        ],
                        options: {}
                    }
                };
                const encryptedData = await this.encrypt(
                    JSON.stringify(plaintextEnvelope),
                    this.selectedServerPublicKey
                );
                if (!encryptedData) {
                    throw new Error('Failed to encrypt relay request envelope');
                }

                const payload = {
                    server_public_key: this.selectedServerPublicKeyB64,
                    client_public_key: clientPublicKeyB64,
                    request_id: requestId,
                    protocol: RELAY_E2EE_PROTOCOL,
                    version: 1,
                    ciphertext: encryptedData.ciphertext,
                    cipherkey: encryptedData.cipherkey,
                    iv: encryptedData.iv
                };
                const response = await fetch('/api/v1/relay/requests', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
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
                            userMessage: this.getUserFacingApiError(errorData, response.status)
                        }
                    };
                }

                const relayResponse = await this.pollRelayResponse(requestId, clientPublicKeyB64);
                if (relayResponse && relayResponse.error) {
                    return relayResponse;
                }
                return this.decryptRelayResponse(relayResponse, requestId, clientPublicKeyB64);
            } catch (error) {
                console.error('Relay API v1 E2EE request error:', error);
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

        getUserFacingApiError(errorPayload, statusCode) {
            const error = errorPayload && typeof errorPayload === 'object' ? errorPayload.error : null;
            const errorCode = error && typeof error.code === 'string' ? error.code : '';
            const fallbackMessage = ASSISTANT_GENERIC_FALLBACK_MESSAGE;

            const codeToMessage = {
                no_registered_compute_nodes: 'No LLM servers are available right now.',
                compute_node_timeout: 'The LLM server took too long to respond. Please try again.',
                compute_node_bridge_timeout: 'The LLM server took too long to respond. Please try again.',
                compute_node_unreachable: 'The LLM server is unavailable right now. Please try again.',
                compute_node_bridge_error: 'Unable to contact the LLM server right now. Please try again.',
                compute_node_invalid_payload: 'The LLM server returned an invalid response. Please try again.',
                cancelled: 'The selected LLM server is no longer available for this chat. Please start a new chat session before trying another server.',
                expired: 'The LLM server took too long to respond. Please start a new chat session or try again.'
            };

            if (codeToMessage[errorCode]) {
                return codeToMessage[errorCode];
            }
            if (statusCode === 404 || statusCode === 410) {
                return 'The selected LLM server is unavailable for this chat. Please start a new chat session before trying another server.';
            }
            if (statusCode >= 500) {
                return 'The selected LLM server is unavailable right now. Please start a new chat session or try again later.';
            }
            return fallbackMessage;
        },

        isInvalidAssistantResponseContent(content) {
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
            return normalized === ASSISTANT_GENERIC_FALLBACK_MESSAGE;
        },

        // Send a message to the server
        async sendMessage() {
            const messageContent = this.newMessage.trim();
            if (!messageContent || !this.canSendMessage) {
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
                let response = await this.sendMessageApi(messageContent);

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
                        if (this.isInvalidAssistantResponseContent(assistantMessage && assistantMessage.content)) {
                            throw new Error('invalid_assistant_response_content');
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
                        content: ASSISTANT_GENERIC_FALLBACK_MESSAGE
                    });
                }
            } catch (error) {
                console.error('Error sending message:', error);
                const isInvalidRelayResponse = error && error.message === 'invalid_assistant_response_content';
                this.chatHistory.push({
                    role: 'assistant',
                    content: isInvalidRelayResponse
                        ? ASSISTANT_INVALID_RELAY_RESPONSE_MESSAGE
                        : 'Sorry, an error occurred while sending your message. Please try again.'
                });
            } finally {
                this.isGeneratingResponse = false;
            }
        }
    },
    beforeDestroy() {
        if (this.computeNodeCountPoller) {
            clearInterval(this.computeNodeCountPoller);
            this.computeNodeCountPoller = null;
        }
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
