import re
from pathlib import Path


def test_send_button_has_touch_optimized_binding():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert re.search(r"touch-optimized", index_html), "touch optimized class missing from template"
    assert re.search(r"\{\s*'touch-optimized'\s*:\s*isTouchInput\s*\}", index_html), (
        "send button must add touch-optimized class when touch input is detected"
    )


def test_landing_source_has_no_raw_vue_text_interpolation():
    """Landing HTML must not expose Vue mustaches before hydration."""
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert re.search(r"\{\{[\s\S]*?\}\}", index_html) is None


def test_chat_js_defines_touch_detection_hook():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "isTouchInput" in chat_js, "chat.js must expose touch detection state"
    assert "detectTouchInput" in chat_js, "chat.js must implement touch detection helper"


def test_landing_context_tier_selector_is_visible_persistent_and_disabled_while_pending():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert 'label for="context-tier-select">Context tier</label>' in index_html
    assert 'data-testid="landing-context-tier-select"' in index_html
    assert 'v-model="selectedContextTier"' in index_html
    assert ':disabled="isGeneratingResponse"' in index_html
    assert '<option value="auto">Auto</option>' in index_html
    assert index_html.index('<option value="auto">Auto</option>') < index_html.index('<option value="8k-fast">8K Fast</option>')
    assert '<option value="8k-fast">8K Fast</option>' in index_html
    assert '<option value="64k-full">64K Full</option>' in index_html

    assert "CONTEXT_TIER_STORAGE_KEY = 'token.place.landing.contextTier.v1'" in chat_js
    assert "AUTO_CONTEXT_TIER = 'auto'" in chat_js
    assert "DEFAULT_CONTEXT_TIER = AUTO_CONTEXT_TIER" in chat_js
    assert "selectedContextTier: DEFAULT_CONTEXT_TIER" in chat_js
    assert "loadStoredContextTier" in chat_js
    assert "persistContextTier" in chat_js
    assert "normalizeContextTier" in chat_js
    assert "isKnownContextTier" in chat_js
    assert "const normalizedValue = typeof value === 'string' ? value.trim() : value" in chat_js
    assert "return this.isKnownContextTierSelectorValue(normalizedValue) ? normalizedValue : DEFAULT_CONTEXT_TIER" in chat_js
    assert "if (stored !== null && stored !== normalized)" in chat_js


def test_landing_context_tier_selection_is_sent_to_next_server_and_encrypted_routing():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert "new URLSearchParams" in chat_js
    assert "model: this.selectedModelId" in chat_js
    assert "context_tier: requestedContextTier" in chat_js
    assert "`/api/v1/relay/servers/next?${params.toString()}`" in chat_js
    assert "selected_context_tier" in chat_js
    assert "selected_context_window_tokens" in chat_js
    assert "selectedContextTierCanSatisfy(selectedContextTier, requestedContextTier)" in chat_js
    assert "if (!selectedContextTier || !this.selectedContextTierCanSatisfy" in chat_js
    assert "selectedProfileMetadata" in chat_js

    assert "routing: {" in chat_js
    assert "context_tier: requestedContextTier" in chat_js
    assert "normalizeWireContextTier" in chat_js
    assert "ciphertext: encryptedData.ciphertext" in chat_js
    assert "messageContent" not in chat_js.split("const relayPayload = {", 1)[1].split("};\n", 1)[0]


def test_landing_auto_context_tier_estimator_and_retry_guards():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert "AUTO_OUTPUT_RESERVATION_TOKENS" in chat_js
    assert "AUTO_CONTEXT_SAFETY_MARGIN_TOKENS" in chat_js
    assert "AUTO_MESSAGE_OVERHEAD_TOKENS" in chat_js
    assert "estimateApiV1MessagesForAutoContextTier" in chat_js
    assert "const characterEstimate = Math.ceil(totalCharacters / 4);" in chat_js
    assert "const wordEstimate = Math.ceil(totalWords * 1.35);" in chat_js
    assert "const resolvedTier = requiredEstimate <= CONTEXT_TIER_ORDER['8k-fast'] ? '8k-fast' : '64k-full';" in chat_js
    assert "return {" in chat_js and "resolvedTier" in chat_js
    assert "requestedContextTier = '64k-full';" in chat_js
    assert "normalizedError.code === 'compute_node_context_window_exceeded'" in chat_js
    assert "!autoRetryAttempted" in chat_js
    assert "selectedContextTier !== '64k-full'" in chat_js
    assert "forceReselect: autoMode" in chat_js
    assert "Auto selected ${label} for this prompt" in chat_js
    assert "context_tier: requestedContextTier" in chat_js
    assert "context_tier: this.normalizeContextTier(this.selectedContextTier)" not in chat_js


def test_landing_context_tier_errors_have_safe_user_messages():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert "no_matching_compute_node" in chat_js
    assert "No LLM servers are available for the selected context tier right now." in chat_js
    assert "invalid_context_tier" in chat_js
    assert "compute_node_context_tier_unsupported" in chat_js
    assert "does not support the requested context tier" in chat_js
    assert "server_public_key" not in chat_js.split("const codeToMessage = {", 1)[1].split("};\n", 1)[0]


def test_landing_chat_js_avoids_relay_v2_streaming_path():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "/api/v1/relay/servers/next" in chat_js, (
        "landing chat must select one API v1 compute node for a browser session"
    )
    assert "/api/v1/relay/requests" in chat_js, (
        "landing chat must dispatch ciphertext-only API v1 relay request envelopes"
    )
    assert "/api/v1/relay/responses/retrieve" in chat_js, (
        "landing chat must retrieve encrypted API v1 relay responses directly"
    )
    assert "/api/v1/chat/completions" not in chat_js, (
        "landing chat must not let server-side chat completions choose a compute node per turn"
    )
    assert "/api/v1/completions" not in chat_js, (
        "landing chat must not call legacy API v1 text completions endpoint"
    )
    assert "/relay/api/v1/chat/completions" not in chat_js, (
        "landing chat must not use relay-prefixed API v1 bypass path"
    )
    assert "/api/v2/chat/completions" not in chat_js, (
        "landing chat must not call relay v2 chat completions endpoint"
    )
    assert "sendStreamingMessage(" not in chat_js, (
        "landing chat must not include streaming relay send helper call chain"
    )
    assert not re.search(r"""(['\"]?)stream\1\s*:\s*true\b""", chat_js), (
        "landing chat request payload must not opt into streaming for relay API v1"
    )


def test_landing_chat_js_disables_incremental_typing_for_relay_v1():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "relayApiV1NonStreaming: true" in chat_js
    assert "incremental character streaming" in chat_js


def test_landing_chat_js_maps_structured_api_v1_errors_to_user_messages():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "getUserFacingApiError" in chat_js
    assert "no_registered_compute_nodes" in chat_js
    assert "compute_node_timeout" in chat_js
    assert "compute_node_bridge_timeout" in chat_js
    assert "compute_node_unreachable" in chat_js
    assert "compute_node_invalid_payload" in chat_js
    assert "compute_node_request_too_large" in chat_js
    assert "compute_node_context_window_exceeded" in chat_js
    assert "No LLM servers are available right now." in chat_js
    assert "The LLM server took too long to respond. Please try again." in chat_js
    assert "The LLM server is unavailable right now. Please try again." in chat_js
    assert "The LLM server returned an invalid response. Please try again." in chat_js
    assert "This request exceeds the current API size limit. Please shorten it and try again." in chat_js
    assert "This prompt exceeds the selected LLM server's context window." in chat_js
    assert "distributed provider timed out contacting relay bridge" not in chat_js
    assert "distributed provider request failed" not in chat_js


def test_landing_chat_js_preserves_context_and_handles_api_v1_message_envelopes():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "createApiV1Messages" in chat_js
    assert "this.chatHistory" in chat_js
    assert "const apiV1Messages = this.createApiV1Messages(messageContent);" in chat_js
    assert "messages: apiV1Messages" in chat_js
    assert "response.message && typeof response.message === 'object'" in chat_js
    assert "response.choices[0].message" in chat_js


def test_landing_chat_js_rejects_raw_array_chat_responses():
    # Landing chat invariants currently use static string guards here rather
    # than an executable static/chat.js harness. Keep these assertions focused
    # on the API v1 response contract so unexpected raw arrays stay on the
    # invalid response path instead of being appended as assistant replies.
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    forbidden_array_branch = "Array.isArray" + "(response)"
    forbidden_slice_branch = "response" + ".slice"
    forbidden_history_comment = "full chat" + " history"
    forbidden_compat_comment = "legacy" + " response" + " format"

    assert forbidden_array_branch not in chat_js
    assert forbidden_slice_branch not in chat_js
    assert forbidden_history_comment not in chat_js
    assert forbidden_compat_comment not in chat_js.lower()
    assert "else if (response.message && typeof response.message === 'object')" in chat_js
    assert "const assistantMessage = response.message;" in chat_js
    assert "else if (response.choices && response.choices.length > 0)" in chat_js
    assert "const assistantMessage = response.choices[0].message;" in chat_js
    assert "this.appendAssistantMessage(assistantMessage);" in chat_js
    assert "throw new Error('Unexpected response format');" in chat_js


def test_landing_chat_js_reselects_or_cancels_on_terminal_relay_states():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "RELAY_RESPONSE_POLL_TIMEOUT_MS = 300000" in chat_js
    assert "cancelRelayRequest" in chat_js
    assert "/api/v1/relay/requests/cancel" in chat_js
    assert "cancel_token: cancelToken" in chat_js
    assert "isTerminalSelectedServerError" in chat_js
    assert "dispatchResponse.status" in chat_js
    assert "status === 404 || status === 410" in chat_js
    assert "getFailoverAttemptLimit" in chat_js
    assert "const replacementCount = Math.max(this.computeNodeCount - 1, 0);" in chat_js
    assert "return Math.min(replacementCount, 3);" in chat_js
    assert "applySupersededSuccess" in chat_js
    assert "await this.refreshComputeNodeCount({ applySupersededSuccess: true });" in chat_js
    assert "Math.max(maxFailovers, 1)" in chat_js
    assert "const terminallyFailedServerPublicKeysB64 = new Set();" in chat_js
    assert "terminallyFailedServerPublicKeysB64.add(this.selectedServerPublicKeyB64);" in chat_js
    assert "let skippedFailedServerSelections = 0;" in chat_js
    assert "const maxSkippedFailedServerSelections = Math.max(maxFailovers + terminallyFailedServerPublicKeysB64.size, 1);" in chat_js
    assert "skippedFailedServerSelections >= maxSkippedFailedServerSelections" in chat_js
    assert "sendMessageApiOnce" in chat_js
    assert "ensureSelectedServer({" in chat_js
    assert "forceReselect: true," in chat_js
    assert "requestedContextTier" in chat_js
    assert "terminallyFailedServerPublicKeysB64.has(this.selectedServerPublicKeyB64)" in chat_js
    assert "skippedFailedServerSelections += 1;" in chat_js
    assert "skippedFailedServerSelections = 0;" in chat_js
    assert "originalFailedServerPublicKeyB64" not in chat_js
    assert "failedServerPublicKeyB64" not in chat_js
    assert "reselectAttempts" not in chat_js
    assert "continue;" in chat_js
    assert "The previous LLM server disconnected. Continuing with another available server." in chat_js
    assert "No LLM servers are available right now. Your chat history is still here." in chat_js
    assert "this.clearSelectedServer()" in chat_js
