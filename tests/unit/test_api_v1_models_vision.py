"""Unit tests for the inline vision helpers in the v1 models module."""

import base64
from typing import Any, Dict

import pytest

from api.v1 import models


def _png_data_uri() -> str:
    pixel = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"rest").decode()
    return f"data:image/png;base64,{pixel}"


def test_extract_base64_payload_variants():
    block = {"type": "input_image", "image": {"b64_json": "aaa"}}
    payload = models._extract_base64_payload(block)
    assert payload == {"encoded": "aaa", "skipped_remote": False}

    block = {"type": "image_url", "image_url": {"url": _png_data_uri()}}
    payload = models._extract_base64_payload(block)
    assert payload["encoded"].startswith("data:image/png;base64,")

    block = {"type": "image_url", "image_url": "https://example.com/image.png"}
    payload = models._extract_base64_payload(block)
    assert payload == {"encoded": None, "skipped_remote": True}


def test_build_vision_summary_happy_path(monkeypatch):
    analyses: Dict[str, Any] = {
        "format": "png",
        "width": 1,
        "height": 1,
        "size_bytes": 5,
        "orientation": "square",
    }
    calls = []

    def _fake_analyze(value: str) -> Dict[str, Any]:
        calls.append(value)
        return analyses

    monkeypatch.setattr(models, "analyze_base64_image", _fake_analyze)

    messages = [
        {
            "content": [
                {"type": "input_image", "image": {"b64_json": "ZmFrZQ=="}},
                {"type": "image_url", "image_url": "https://example.com/remote.png"},
            ]
        }
    ]

    summary = models._build_vision_summary(messages)
    assert "Vision analysis" in summary
    assert "remote URLs" in summary
    assert calls == ["ZmFrZQ=="]


def test_build_vision_summary_handles_invalid_payload(monkeypatch):
    def _raise(_: str) -> Dict[str, Any]:
        raise ValueError("bad data")

    monkeypatch.setattr(models, "analyze_base64_image", _raise)

    messages = [
        {
            "content": [
                {"type": "input_image", "image": {"b64_json": "invalid"}},
                {"type": "image_url", "image_url": "https://example.com/valid.png"},
            ]
        }
    ]

    summary = models._build_vision_summary(messages)
    assert summary == (
        "Vision analysis unavailable: remote image URLs require base64 data URIs for inspection."
    )


def test_build_vision_summary_no_entries_returns_none(monkeypatch):
    monkeypatch.setattr(models, "analyze_base64_image", lambda _: {"format": "png"})
    messages = [{"content": [{"type": "input_text", "text": "hello"}]}]
    assert models._build_vision_summary(messages) is None
