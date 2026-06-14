const ASSISTANT_GENERIC_FALLBACK_MESSAGE = 'Sorry, I encountered an issue generating a response. Please try again.';
const ASSISTANT_INVALID_RELAY_RESPONSE_MESSAGE = 'Sorry, the relay returned an invalid response. Please try again.';
const COMPUTE_NODE_COUNT_POLL_INTERVAL_MS = 30000;
const RELAY_RESPONSE_POLL_TIMEOUT_MS = 300000;
const EMERGENCY_MODEL_FALLBACK_ID = 'llama-3.1-8b-instruct';

new Vue({
    el: '#app',
    data: {
        newMessage: '',
        chatHistory: [],
        serverPublicKey: null,
        selectedServerPublicKeyB64: null,
        selectedServerPublicKey: null,
        selectedServerKeyLabel: '',
        selectedServerTerminalFailure: '',
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
                return '';
            }
            if (this.computeNodeCountStatus === 'error') {
                return 'Live compute nodes: unavailable';
            }
            return `Live compute nodes: ${this.computeNodeCount}`;
        },
        computeNodeCountLastUpdatedLabel() {
            if (!this.computeNodeCountLastUpdated) {
                return '';
            }
            return `Updated ${this.computeNodeCountLastUpdated}`;
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
                    root: EMERGENCY_MODEL_FALLBACK_ID
                };
            }
            return null;
        },
        hasClientKeypair() {
            return Boolean(this.clientPrivateKey && this.clientPublicKey);
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
        async refreshComputeNodeCount(options = {}) {
            // Failover capacity refreshes must be allowed to apply their own
            // successful diagnostics result even if the background poller starts
            // another request before they finish; otherwise a stale count of one
            // can incorrectly suppress probing for a newly registered replacement.
            const applySupersededSuccess = options && options.applySupersededSuccess === true;
            const requestId = this.computeNodeCountRequestId + 1;
            this.computeNodeCountRequestId = requestId;

            try {
                const response = await fetch('/relay/diagnostics', { cache: 'no-store' });
                if (requestId !== this.computeNodeCountRequestId && !applySupersededSuccess) {
                    return false;
                }
                if (!response.ok) {
                    throw new Error('Failed to fetch relay diagnostics');
                }
                const data = await response.json();
                if (requestId !== this.computeNodeCountRequestId && !applySupersededSuccess) {
                    return false;
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
                return true;
            } catch (error) {
                if (requestId !== this.computeNodeCountRequestId) {
                    return false;
                }
                console.warn('Unable to refresh compute-node count:', error);
                this.computeNodeCountStatus = 'error';
                this.computeNodeCountLastUpdated = '';
                return false;
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

        encodePemToBase64(pem) {
            if (typeof pem !== 'string' || !pem.trim()) {
                return null;
            }
            return btoa(pem.trim());
        },

        createServerKeyLabel(rawKey) {
            if (typeof rawKey !== 'string' || !rawKey.trim()) {
                return '';
            }
            const normalized = rawKey.replace(/\s+/g, '');
            const fingerprint = CryptoJS.SHA256(normalized).toString(CryptoJS.enc.Hex);
            return `Server: ${fingerprint.slice(0, 8)}…${fingerprint.slice(-8)}`;
        },

        async ensureSelectedServer(options = {}) {
            const forceReselect = Boolean(options.forceReselect);
            if (forceReselect) {
                this.clearSelectedServer();
            }

            if (this.selectedServerPublicKey && this.selectedServerPublicKeyB64) {
                return true;
            }

            try {
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
                const rawKey = data && data.server_public_key;
                const normalizedKey = this.normalizeServerPublicKey(rawKey);
                if (!normalizedKey) {
                    throw new Error('Relay returned an invalid compute-node public key');
                }

                const rawKeyText = String(rawKey).trim();
                this.selectedServerPublicKey = normalizedKey;
                this.serverPublicKey = normalizedKey;
                this.selectedServerPublicKeyB64 = rawKeyText.includes('-----BEGIN')
                    ? this.encodePemToBase64(normalizedKey)
                    : rawKeyText.replace(/\s+/g, '');
                this.selectedServerKeyLabel = this.createServerKeyLabel(this.selectedServerPublicKeyB64);
                return true;
            } catch (error) {
                console.error('Error selecting API v1 compute node:', error);
                return {
                    error: {
                        userMessage: 'Unable to select an LLM server right now. Your chat history is still here.'
                    }
                };
            }
        },

        createRequestId() {
            const bytes = new Uint8Array(16);
            if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
                crypto.getRandomValues(bytes);
            } else {
                for (let i = 0; i < bytes.length; i++) {
                    bytes[i] = Math.floor(Math.random() * 256);
                }
            }
            return Array.from(bytes).map((byte) => byte.toString(16).padStart(2, '0')).join('');
        },

        clearSelectedServer() {
            this.selectedServerPublicKey = null;
            this.selectedServerPublicKeyB64 = null;
            this.serverPublicKey = null;
            this.selectedServerKeyLabel = '';
        },

        markSelectedServerTerminalFailure(message) {
            this.selectedServerTerminalFailure = message || 'The previous LLM server disconnected. Continuing with another available server.';
        },

        clearSelectedServerNotice() {
            this.selectedServerTerminalFailure = '';
        },

        startNewChatAfterSelectedServerFailure() {
            this.chatHistory = [];
            this.newMessage = '';
            this.clearSelectedServer();
            this.selectedServerTerminalFailure = '';
            this.$nextTick(() => {
                this.adjustMessageInputHeight();
            });
        },

        createApiV1Messages(messageContent) {
            const messages = Array.isArray(this.chatHistory)
                ? this.chatHistory
                    .filter((entry) => entry && (entry.role === 'user' || entry.role === 'assistant'))
                    .map((entry) => ({
                        role: entry.role,
                        content: typeof entry.content === 'string' ? entry.content : this.getDisplayContent(entry)
                    }))
                    .filter((entry) => typeof entry.content === 'string' && entry.content.trim())
                : [];

            const latest = messages.length > 0 ? messages[messages.length - 1] : null;
            if (!latest || latest.role !== 'user' || latest.content !== messageContent) {
                messages.push({
                    role: 'user',
                    content: messageContent
                });
            }
            return messages;
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

        getUserFacingRelayRetrieveError(status) {
            if (status === 202) {
                return null;
            }
            if (status === 404) {
                return 'The previous LLM server disconnected. Continuing with another available server.';
            }
            if (status === 410) {
                return 'The previous LLM server disconnected. Continuing with another available server.';
            }
            if (status >= 500) {
                return 'The relay is unavailable right now. Please try again later.';
            }
            return ASSISTANT_GENERIC_FALLBACK_MESSAGE;
        },

        async cancelRelayRequest(clientPublicKeyB64, requestId, cancelToken) {
            if (!clientPublicKeyB64 || !requestId || !cancelToken) {
                return;
            }
            try {
                await fetch('/api/v1/relay/requests/cancel', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        client_public_key: clientPublicKeyB64,
                        request_id: requestId,
                        cancel_token: cancelToken,
                        status: 'cancelled',
                        reason: 'client_timeout'
                    })
                });
            } catch (error) {
                console.warn('Unable to cancel timed-out API v1 relay request:', error);
            }
        },

        isTerminalSelectedServerError(status, errorPayload) {
            if (status === 404 || status === 410) {
                return true;
            }
            const error = errorPayload && typeof errorPayload === 'object' ? errorPayload.error : null;
            const errorCode = error && typeof error.code === 'string' ? error.code : '';
            return [
                'selected_server_terminal',
                'selected_server_unavailable',
                'selected_server_expired',
                'selected_server_removed',
                'server_unavailable',
                'server_expired',
                'server_removed'
            ].includes(errorCode);
        },

        getFailoverAttemptLimit() {
            if (Number.isInteger(this.computeNodeCount)) {
                const replacementCount = Math.max(this.computeNodeCount - 1, 0);
                return Math.min(replacementCount, 3);
            }
            return 1;
        },

        async retrieveRelayResponse(clientPublicKeyB64, requestId, cancelToken) {
            const timeoutMs = RELAY_RESPONSE_POLL_TIMEOUT_MS;
            const pollIntervalMs = 500;
            const deadline = Date.now() + timeoutMs;

            while (Date.now() < deadline) {
                const response = await fetch('/api/v1/relay/responses/retrieve', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        client_public_key: clientPublicKeyB64,
                        request_id: requestId
                    })
                });

                if (response.status === 202) {
                    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
                    continue;
                }

                if (!response.ok) {
                    let errorData = null;
                    try {
                        errorData = await response.json();
                    } catch (_jsonError) {
                        errorData = null;
                    }
                    const userMessage = this.getUserFacingRelayRetrieveError(response.status);
                    return {
                        error: {
                            userMessage,
                            terminalSelectedServer: this.isTerminalSelectedServerError(response.status, errorData)
                        }
                    };
                }

                return response.json();
            }

            await this.cancelRelayRequest(clientPublicKeyB64, requestId, cancelToken);
            return {
                error: {
                    userMessage: 'The LLM server took too long to respond. Please try again.'
                }
            };
        },

        // Send a message through the relay-blind API v1 E2EE request routes.
        async sendMessageApiOnce(messageContent) {
            if (!this.selectedModel) {
                console.error('No API v1 catalogue model selected');
                return null;
            }

            const selectedServer = await this.ensureSelectedServer();
            if (selectedServer !== true) {
                return selectedServer;
            }

            const requestId = this.createRequestId();
            const cancelToken = this.createRequestId();
            const clientPublicKeyB64 = this.encodeClientPublicKeyForApi();
            const plaintextEnvelope = {
                protocol: 'tokenplace_api_v1_relay_e2ee',
                version: 1,
                request_id: requestId,
                client_public_key: clientPublicKeyB64,
                api_v1_request: {
                    model: this.selectedModelId,
                    messages: this.createApiV1Messages(messageContent),
                    options: {}
                }
            };

            try {
                const encryptedData = await this.encrypt(
                    JSON.stringify(plaintextEnvelope),
                    this.selectedServerPublicKey
                );

                if (!encryptedData) {
                    throw new Error('Failed to encrypt relay request envelope');
                }

                const relayPayload = {
                    server_public_key: this.selectedServerPublicKeyB64,
                    client_public_key: clientPublicKeyB64,
                    request_id: requestId,
                    protocol: 'tokenplace_api_v1_relay_e2ee',
                    version: 1,
                    ciphertext: encryptedData.ciphertext,
                    cipherkey: encryptedData.cipherkey,
                    iv: encryptedData.iv,
                    cancel_token: cancelToken
                };

                const dispatchResponse = await fetch('/api/v1/relay/requests', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(relayPayload)
                });

                if (!dispatchResponse.ok) {
                    let errorData = null;
                    try {
                        errorData = await dispatchResponse.json();
                    } catch (_jsonError) {
                        errorData = null;
                    }
                    const unavailable = this.isTerminalSelectedServerError(dispatchResponse.status, errorData);
                    const userMessage = unavailable
                        ? 'The previous LLM server disconnected. Continuing with another available server.'
                        : this.getUserFacingApiError(errorData);
                    return {
                        error: {
                            userMessage,
                            terminalSelectedServer: unavailable
                        }
                    };
                }

                const encryptedResponse = await this.retrieveRelayResponse(clientPublicKeyB64, requestId, cancelToken);
                if (encryptedResponse && encryptedResponse.error) {
                    return encryptedResponse;
                }

                const responseCiphertext = encryptedResponse.chat_history || encryptedResponse.ciphertext;
                const decryptedJson = await this.decrypt(
                    responseCiphertext,
                    encryptedResponse.cipherkey,
                    encryptedResponse.iv
                );

                if (!decryptedJson) {
                    throw new Error('Failed to decrypt relay response');
                }

                const responseEnvelope = JSON.parse(decryptedJson);
                if (
                    !responseEnvelope ||
                    responseEnvelope.protocol !== 'tokenplace_api_v1_relay_e2ee' ||
                    responseEnvelope.version !== 1 ||
                    responseEnvelope.request_id !== requestId ||
                    responseEnvelope.client_public_key !== clientPublicKeyB64 ||
                    !responseEnvelope.api_v1_response
                ) {
                    throw new Error('Invalid relay response envelope');
                }

                return responseEnvelope.api_v1_response;
            } catch (error) {
                console.error('API v1 relay request error:', error);
                return null;
            }
        },

        async sendMessageApi(messageContent) {
            let maxFailovers = this.getFailoverAttemptLimit();
            let refreshedFailoverCapacity = false;
            let skippedFailedServerSelections = 0;
            let failovers = 0;
            let needsDispatch = true;
            const terminallyFailedServerPublicKeysB64 = new Set();

            while (failovers <= maxFailovers) {
                if (needsDispatch) {
                    const response = await this.sendMessageApiOnce(messageContent);
                    if (response && response.error && response.error.terminalSelectedServer === true && this.selectedServerPublicKeyB64) {
                        terminallyFailedServerPublicKeysB64.add(this.selectedServerPublicKeyB64);
                    }
                    if (!response || !response.error || response.error.terminalSelectedServer !== true) {
                        if (response && !(response.error && response.error.terminalSelectedServer === true)) {
                            this.clearSelectedServerNotice();
                        }
                        return response;
                    }
                }
                needsDispatch = false;

                if (failovers >= maxFailovers && !refreshedFailoverCapacity) {
                    refreshedFailoverCapacity = true;
                    const refreshed = await this.refreshComputeNodeCount({ applySupersededSuccess: true });
                    // If diagnostics could not be applied because another refresh
                    // raced or failed, allow one bounded next-server probe rather
                    // than failing closed from a possibly stale local count.
                    maxFailovers = refreshed ? this.getFailoverAttemptLimit() : Math.max(maxFailovers, 1);
                }

                const maxSkippedFailedServerSelections = Math.max(maxFailovers + terminallyFailedServerPublicKeysB64.size, 1);
                if (failovers >= maxFailovers || skippedFailedServerSelections >= maxSkippedFailedServerSelections) {
                    this.clearSelectedServer();
                    this.markSelectedServerTerminalFailure('The previous LLM server disconnected. No replacement LLM server accepted this request. Your chat history is still here.');
                    return {
                        error: {
                            userMessage: 'The previous LLM server disconnected. No replacement LLM server accepted this request. Your chat history is still here.'
                        }
                    };
                }

                this.clearSelectedServer();
                this.markSelectedServerTerminalFailure('The previous LLM server disconnected. Continuing with another available server.');
                const selectedServer = await this.ensureSelectedServer({ forceReselect: true });
                if (selectedServer !== true) {
                    const userMessage = selectedServer && selectedServer.error && selectedServer.error.userMessage
                        ? selectedServer.error.userMessage
                        : 'No LLM servers are available right now. Your chat history is still here.';
                    this.markSelectedServerTerminalFailure(userMessage);
                    return { error: { userMessage } };
                }
                if (terminallyFailedServerPublicKeysB64.has(this.selectedServerPublicKeyB64)) {
                    skippedFailedServerSelections += 1;
                    this.clearSelectedServer();
                    this.markSelectedServerTerminalFailure('The previous LLM server disconnected. Continuing with another available server.');
                    continue;
                }
                skippedFailedServerSelections = 0;
                failovers += 1;
                needsDispatch = true;
            }

            return {
                error: {
                    userMessage: 'The previous LLM server disconnected. No replacement LLM server accepted this request. Your chat history is still here.'
                }
            };
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
            const fallbackMessage = ASSISTANT_GENERIC_FALLBACK_MESSAGE;
            if (!error || typeof error !== 'object') {
                return fallbackMessage;
            }

            if (typeof error.userMessage === 'string' && error.userMessage.trim()) {
                return error.userMessage;
            }

            const errorCode = typeof error.code === 'string' ? error.code.trim() : '';
            const codeToMessage = {
                no_registered_compute_nodes: 'No LLM servers are available right now. Your chat history is still here.',
                compute_node_model_unsupported: 'The selected model is not available on this LLM server. Please try again.',
                compute_node_options_unsupported: 'The selected LLM server does not support one of the requested options. Please try again.',
                compute_node_invalid_request: 'The LLM server rejected the request format. Please try again.',
                compute_node_invalid_model_output: 'The LLM server returned an invalid response. Please try again.',
                compute_node_internal_error: 'The LLM server failed while generating a response. Please try again.',
                compute_node_timeout: 'The LLM server took too long to respond. Please try again.',
                compute_node_request_cancelled: 'The LLM server request expired before it could be answered. Please try again.',
                compute_node_bridge_timeout: 'The LLM server took too long to respond. Please try again.',
                compute_node_unreachable: 'The LLM server is unavailable right now. Please try again.',
                compute_node_bridge_error: 'Unable to contact the LLM server right now. Please try again.',
                compute_node_invalid_payload: 'The LLM server returned an invalid response. Please try again.'
            };

            if (Object.prototype.hasOwnProperty.call(codeToMessage, errorCode)) {
                return codeToMessage[errorCode];
            }

            // Raw error.message values can contain internal relay or compute-node details.
            // Only explicit userMessage values and known safe error codes are rendered.
            return fallbackMessage;
        },

        normalizeApiV1ResponseError(response) {
            const error = response && typeof response === 'object' ? response.error : null;
            if (!error || typeof error !== 'object') {
                return null;
            }

            const code = typeof error.code === 'string' && error.code.trim() ? error.code.trim() : '';
            return {
                userMessage: this.getUserFacingApiError(response),
                terminalSelectedServer: error.terminalSelectedServer === true,
                code
            };
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
                    const normalizedError = this.normalizeApiV1ResponseError(response);
                    if (normalizedError) {
                        if (normalizedError.code) {
                            console.warn('API v1 structured error rendered:', { code: normalizedError.code });
                        }
                        this.chatHistory.push({
                            role: 'assistant',
                            content: normalizedError.userMessage
                        });
                    }
                    // For API response, extract last message
                    else if (response.message && typeof response.message === 'object') {
                        const assistantMessage = response.message;
                        if (this.isInvalidAssistantResponseContent(assistantMessage && assistantMessage.content)) {
                            throw new Error('invalid_assistant_response_content');
                        }
                        this.appendAssistantMessage(assistantMessage);
                    }
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
            if (!this.$el || typeof this.$el.querySelector !== 'function') {
                return;
            }
            const container = this.$el.querySelector(".chat-container");
            if (!container) {
                return;
            }
            container.scrollTop = container.scrollHeight;
        });
    }
});
