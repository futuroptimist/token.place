from pathlib import Path


def test_relay_root_template_shows_relay_mode_diagnostics_when_chat_unavailable():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert 'v-if="!chatAvailable"' in index_html
    assert "Chat demo unavailable in this relay mode" in index_html
    assert "/relay/diagnostics" in index_html
    assert "/sink" in index_html


def test_chat_js_disables_send_flow_when_relay_chat_is_unavailable():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")
    assert "initializeChatAvailability" in chat_js
    assert "isLoopbackHost" in chat_js
    assert "!this.chatAvailable || !messageContent" in chat_js
    assert "Local relay-only mode is active on localhost" in chat_js
