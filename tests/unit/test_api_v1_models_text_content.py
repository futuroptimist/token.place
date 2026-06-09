"""Unit tests for text-only content block helpers in the v1 models module."""

import pytest

from api.v1 import models


def test_stringify_content_blocks_text_only_variants():
    content = [
        {"type": "input_text", "text": "  First segment  "},
        {"type": "text", "text": "Second segment"},
        "ignored",
    ]

    result = models._stringify_content_blocks(content)
    assert result == "First segment\n\nSecond segment"


def test_generate_response_rejects_image_content_blocks(monkeypatch):
    """API v1 chat must fail closed instead of faking multimodal support."""

    monkeypatch.setattr(models, "USE_MOCK_LLM", True)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this."},
                {"type": "input_image", "image": {"b64_json": "ZmFrZQ=="}},
            ],
        }
    ]

    with pytest.raises(models.ModelError) as exc:
        models.generate_response("llama-3.1-8b-instruct", messages)

    assert exc.value.status_code == 400
    assert "do not support image content" in exc.value.message


def test_stringify_content_blocks_falls_back_gracefully():
    assert models._stringify_content_blocks("plain text") == "plain text"
    assert models._stringify_content_blocks(None) is None
    assert models._stringify_content_blocks({"unexpected": "structure"}) == {"unexpected": "structure"}
    assert models._stringify_content_blocks([{"type": "unknown"}]) == ""


def test_normalise_chat_messages_in_place():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": " hi "}]},
        {"role": "assistant", "content": "ready"},
        "raw-string-entry",
    ]

    result = models._normalise_chat_messages(messages)

    assert result is messages
    assert messages[0]["content"] == "hi"
    assert messages[1]["content"] == "ready"
    assert messages[2] == "raw-string-entry"


def test_generate_response_normalises_text_blocks(monkeypatch):
    """Structured text content should collapse to strings before inference."""

    captured = {}

    class _DummyModel:
        def create_chat_completion(self, messages, **_):
            captured["messages"] = messages
            assert messages[0]["content"] == "First segment\n\nSecond segment"
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Acknowledged.",
                        }
                    }
                ]
            }

    monkeypatch.setattr(models, "USE_MOCK_LLM", False)
    monkeypatch.setattr(models, "get_model_instance", lambda _model_id: _DummyModel())

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "First segment"},
                {"type": "text", "text": "Second segment"},
            ],
        }
    ]

    result = models.generate_response("llama-3.1-8b-instruct", messages)

    assert captured["messages"][0]["content"] == "First segment\n\nSecond segment"
    assert result[-1]["role"] == "assistant"
    assert result[-1]["content"] == "Acknowledged."


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "content must be text-only"),
        (123, "content must be text-only"),
        ([{"type": "text", "text": ""}], "Invalid text-only content block"),
        ([{"type": "text"}], "Invalid text-only content block"),
        (["raw block"], "Invalid text-only content block"),
    ],
)
def test_generate_response_rejects_invalid_text_content_shapes(monkeypatch, content, expected):
    """API v1 should return precise request errors before runtime invocation."""

    monkeypatch.setattr(models, "USE_MOCK_LLM", True)

    with pytest.raises(models.ModelError) as exc:
        models.generate_response(
            "llama-3.1-8b-instruct",
            [{"role": "user", "content": content}],
        )

    assert exc.value.status_code == 400
    assert expected in exc.value.message
