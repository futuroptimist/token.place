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
