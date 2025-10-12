"""Tests for CryptoClient fallback behavior to Cloudflare or other relays."""

import base64
from typing import List

import pytest
import requests

from utils.crypto_helpers import CryptoClient


@pytest.fixture
def crypto_client_with_fallback(monkeypatch):
    """Create a CryptoClient configured with a fallback relay."""

    monkeypatch.setenv("API_FALLBACK_URLS", "")

    client = CryptoClient(
        "https://primary.token.place",
        fallback_urls=["https://cf.token.place"],
    )

    return client


def test_fetch_server_public_key_uses_fallback(crypto_client_with_fallback, monkeypatch):
    """CryptoClient should try the fallback when the primary relay is unreachable."""

    client = crypto_client_with_fallback
    called_urls: List[str] = []

    def fake_get(url, timeout):  # pragma: no cover - behaviour asserted via list
        called_urls.append(url)
        if "primary" in url:
            raise requests.exceptions.ConnectionError("primary relay offline")

        class _Response:
            status_code = 200

            def json(self):
                return {"public_key": base64.b64encode(b"relay-key").decode()}

        return _Response()

    monkeypatch.setattr("utils.crypto_helpers.requests.get", fake_get)

    assert client.fetch_server_public_key(endpoint="/public-key") is True
    assert called_urls == [
        "https://primary.token.place/public-key",
        "https://cf.token.place/public-key",
    ]
    assert client.base_url == "https://cf.token.place"


def test_send_encrypted_message_uses_fallback(monkeypatch):
    """POST requests should cascade to the fallback relay when the primary fails."""

    client = CryptoClient(
        "https://relay.primary",
        fallback_urls=["https://relay.cloudflare"],
    )

    called_urls: List[str] = []

    def fake_post(url, json=None, timeout=None):  # pragma: no cover - validated via assertions
        called_urls.append(url)
        if "primary" in url:
            raise requests.exceptions.ConnectTimeout("no route to host")

        class _Response:
            status_code = 200

            def json(self):
                return {"ok": True}

        return _Response()

    monkeypatch.setattr("utils.crypto_helpers.requests.post", fake_post)

    payload = {"ciphertext": "", "cipherkey": "", "iv": ""}
    assert client.send_encrypted_message("/faucet", payload) == {"ok": True}
    assert called_urls == [
        "https://relay.primary/faucet",
        "https://relay.cloudflare/faucet",
    ]
    assert client.base_url == "https://relay.cloudflare"
