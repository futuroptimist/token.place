"""Tests for Cloudflare fallback behavior in client API calls."""
import base64
import importlib
import json
from types import SimpleNamespace
from typing import Optional

import pytest
import requests


@pytest.fixture
def load_client(monkeypatch):
    """Reload the client module with specified API endpoints."""

    def _loader(primary: str, fallbacks: Optional[str]):
        monkeypatch.setenv("API_BASE_URL", primary)
        if fallbacks is None:
            monkeypatch.delenv("API_FALLBACK_URLS", raising=False)
        else:
            monkeypatch.setenv("API_FALLBACK_URLS", fallbacks)
        import client

        return importlib.reload(client)

    return _loader


def test_get_server_public_key_cloudflare_fallback(monkeypatch, load_client):
    client = load_client("https://primary.example/api/v1", "https://cf.example/api/v1")

    called_urls = []

    def fake_get(url, timeout):  # pragma: no cover - cover via assertions
        called_urls.append(url)
        if "primary" in url:
            raise requests.exceptions.ConnectionError("primary offline")

        class _Response:
            status_code = 200

            def json(self):
                return {"public_key": "cf-key"}

            def raise_for_status(self):
                return None

        return _Response()

    monkeypatch.setattr(client.requests, "get", fake_get)

    assert client.get_server_public_key() == "cf-key"
    assert called_urls == [
        "https://primary.example/api/v1/public-key",
        "https://cf.example/api/v1/public-key",
    ]


def test_chat_completions_cloudflare_fallback(monkeypatch, load_client):
    client = load_client("https://primary.example/api/v1", "https://cf.example/api/v1")

    called_urls = []

    def fake_post(url, json=None, timeout=None):  # pragma: no cover - cover via assertions
        called_urls.append(url)
        if "primary" in url:
            raise requests.exceptions.ConnectionError("primary offline")

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "encrypted": True,
                    "data": {
                        "ciphertext": base64.b64encode(b"ciphertext").decode(),
                        "cipherkey": base64.b64encode(b"cipherkey").decode(),
                        "iv": base64.b64encode(b"iv").decode(),
                    },
                }

        return _Response()

    monkeypatch.setattr(client.requests, "post", fake_post)
    monkeypatch.setattr(
        client,
        "encrypt",
        lambda *args, **kwargs: ({"ciphertext": b"ciphertext"}, b"cipherkey", b"iv"),
    )
    monkeypatch.setattr(
        client,
        "decrypt",
        lambda *args, **kwargs: json.dumps({"choices": []}).encode(),
    )

    server_pub_key_b64 = base64.b64encode(b"serverkey").decode()
    client_public_key = b"client-pub"

    result = client.call_chat_completions_encrypted(
        server_pub_key_b64, SimpleNamespace(), client_public_key
    )

    assert result == {"choices": []}
    assert called_urls == [
        "https://primary.example/api/v1/chat/completions",
        "https://cf.example/api/v1/chat/completions",
    ]
