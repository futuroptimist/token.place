from pathlib import Path


def test_landing_chat_avoids_api_v2_and_streaming_for_relay_path() -> None:
    """Relay landing chat must remain API v1-only, non-streaming in v0.1.0."""

    chat_js = Path('static/chat.js').read_text(encoding='utf-8')

    assert "fetch('/api/v1/chat/completions'" in chat_js
    assert "const streamed = await this.sendStreamingMessage(historySnapshot);" not in chat_js
    assert "sendMessageApi()" in chat_js
