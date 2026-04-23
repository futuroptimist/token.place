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
    assert "/api/v2/chat/completions" not in chat_js, (
        "landing chat must not call relay v2 chat completions endpoint"
    )
    assert "sendStreamingMessage(" not in chat_js, (
        "landing chat must not include streaming relay send helper call chain"
    )


def test_landing_chat_js_disables_incremental_typing_for_relay_v1():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "relayApiV1NonStreaming: true" in chat_js
    assert "incremental character streaming" in chat_js


def test_landing_chat_js_maps_structured_api_v1_errors_to_user_messages():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "getUserFacingApiError" in chat_js
    assert "no_registered_compute_nodes" in chat_js
    assert "No LLM servers are available right now." in chat_js
