import re
from pathlib import Path


def test_send_button_has_touch_optimized_binding():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert re.search(r"touch-optimized", index_html), "touch optimized class missing from template"
    assert re.search(r"\{\s*'touch-optimized'\s*:\s*isTouchInput\s*\}", index_html), (
        "send button must add touch-optimized class when touch input is detected"
    )


def test_chat_js_defines_touch_detection_hook():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "isTouchInput" in chat_js, "chat.js must expose touch detection state"
    assert "detectTouchInput" in chat_js, "chat.js must implement touch detection helper"


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
    assert "No LLM servers are available right now." in chat_js
    assert "The LLM server took too long to respond. Please try again." in chat_js
    assert "The LLM server is unavailable right now. Please try again." in chat_js
    assert "The LLM server returned an invalid response. Please try again." in chat_js
    assert "distributed provider timed out contacting relay bridge" not in chat_js
    assert "distributed provider request failed" not in chat_js


def test_landing_chat_js_preserves_context_and_handles_api_v1_message_envelopes():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "createApiV1Messages" in chat_js
    assert "this.chatHistory" in chat_js
    assert "messages: this.createApiV1Messages(messageContent)" in chat_js
    assert "response.message && typeof response.message === 'object'" in chat_js


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
    assert "sendMessageApiOnce" in chat_js
    assert "ensureSelectedServer({ forceReselect: true })" in chat_js
    assert "failedServerPublicKeyB64 && this.selectedServerPublicKeyB64 === failedServerPublicKeyB64" in chat_js
    assert "The previous LLM server disconnected. Continuing with another available server." in chat_js
    assert "No LLM servers are available right now. Your chat history is still here." in chat_js
    assert "this.clearSelectedServer()" in chat_js
