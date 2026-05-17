"""Vision support tests for API v2 chat completions."""

import json
import types

import pytest

from api.v2 import routes as v2_routes
from relay import app as relay_app


@pytest.fixture
def client():
    relay_app.config["TESTING"] = True
    with relay_app.test_client() as test_client:
        yield test_client


def _allow_policy(monkeypatch):
    monkeypatch.setattr(
        v2_routes,
        "evaluate_messages_for_policy",
        lambda messages: types.SimpleNamespace(allowed=True, matched_term=None, reason=None),
    )


def test_chat_completion_with_base64_image_returns_analysis(client, monkeypatch):
    """Requests containing base64-encoded images should receive a vision analysis."""

    base64_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y7ZlJ4AAAAASUVORK5CYII="
    )

    _allow_policy(monkeypatch)
    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])
    monkeypatch.setattr(v2_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    payload = {
        "model": "alpha",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe the attachment."},
                    {"type": "input_image", "image": {"b64_json": base64_png}},
                ],
            }
        ],
    }

    response = client.post(
        "/api/v2/chat/completions",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["object"] == "chat.completion"
    message = data["choices"][0]["message"]["content"]
    assert "png" in message.lower()
    assert "1x1" in message
    assert "bytes" in message


def test_validate_chat_messages_rejects_empty_image_block():
    """v2 image blocks must include an analyzable payload before runtime use."""

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this."},
                {"type": "image"},
            ],
        }
    ]

    with pytest.raises(v2_routes.ValidationError) as exc:
        v2_routes.validate_chat_messages(messages)

    assert exc.value.field == "messages"
    assert "image must be an object" in exc.value.message


def test_chat_completion_rejects_empty_image_block_before_runtime(client, monkeypatch):
    """Malformed v2 images should fail validation instead of reaching API v1 runtime."""

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "alpha"}])
    monkeypatch.setattr(v2_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    def _unexpected_generate(*_args, **_kwargs):
        raise AssertionError("generate_response should not be called")

    monkeypatch.setattr(v2_routes, "generate_response", _unexpected_generate)

    payload = {
        "model": "alpha",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this."},
                    {"type": "image"},
                ],
            }
        ],
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 400
    data = response.get_json()
    assert data["error"]["param"] == "messages"
    assert "image must be an object" in data["error"]["message"]


def test_validate_chat_messages_accepts_image_url_string_and_object():
    """v2 validation should accept remote image URL block shapes it can summarize later."""

    v2_routes.validate_chat_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe these"},
                    {"type": "image_url", "image_url": "https://example.com/a.png"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/b.png"}},
                ],
            }
        ]
    )


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ("not-a-list", "Messages must be an array"),
        (["not-a-message"], "messages[0] must be an object"),
        ([{"role": "bad", "content": "hi"}], "Invalid role"),
        ([{"role": "user", "content": 42}], "content must be a string or array"),
        ([{"role": "user", "content": []}], "content must contain at least one item"),
        ([{"role": "user", "content": ["bad"]}], "content[0] must be an object"),
        ([{"role": "user", "content": [{"type": "text", "text": ""}]}], "text must be a non-empty string"),
        ([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": ""}}]}], "image_url.url"),
        ([{"role": "user", "content": [{"type": "input_image", "image": {"b64_json": ""}}]}], "must include base64 data"),
        ([{"role": "user", "content": [{"type": "unknown"}]}], "Unsupported content type"),
    ],
)
def test_validate_chat_messages_rejects_malformed_multimodal_blocks(messages, expected):
    """Malformed v2 content blocks should fail at validation boundaries."""

    with pytest.raises(v2_routes.ValidationError) as exc:
        v2_routes.validate_chat_messages(messages)

    assert exc.value.field == "messages"
    assert expected in exc.value.message


def test_extract_base64_payload_supports_all_inline_image_keys():
    """Cover the v2 image payload extraction variants used before vision summaries."""

    assert v2_routes._extract_base64_payload(
        {"type": "input_image", "image": {"b64_json": " aaa "}}
    ) == {"encoded": " aaa ", "skipped_remote": False}
    assert v2_routes._extract_base64_payload(
        {"type": "image", "image": {"base64": "bbb"}}
    )["encoded"] == "bbb"
    assert v2_routes._extract_base64_payload(
        {"type": "image", "image_url": {"data": "ccc"}}
    )["encoded"] == "ccc"


def test_build_v2_vision_summary_handles_remote_and_invalid_payloads(monkeypatch):
    """Remote URLs are reported, while invalid inline base64 payloads are skipped safely."""

    warnings = []
    monkeypatch.setattr(v2_routes, "log_warning", warnings.append)

    remote_only = v2_routes._build_v2_vision_summary(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                ],
            }
        ]
    )
    assert "remote image URLs require base64" in remote_only

    invalid_only = v2_routes._build_v2_vision_summary(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image": {"b64_json": "not-valid-base64"}},
                ],
            }
        ]
    )
    assert invalid_only is None
    assert any("Skipping invalid image payload" in message for message in warnings)


def test_build_v2_vision_summary_appends_remote_note_when_inline_analysis_exists():
    """A valid inline image plus remote URLs should produce analysis and a remote note."""

    base64_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y7ZlJ4AAAAASUVORK5CYII="
    )
    summary = v2_routes._build_v2_vision_summary(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image": {"b64_json": base64_png}},
                    {"type": "image_url", "image_url": "https://example.com/remote.png"},
                ],
            }
        ]
    )

    assert "Vision analysis" in summary
    assert "Additional attachments reference remote URLs" in summary
