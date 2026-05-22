"""Focused API v1 relay coverage for CryptoClient response envelopes."""

import pytest

from utils.crypto_helpers import CryptoClient


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


ENCRYPTED_RELAY_RESPONSE = {
    "chat_history": "ciphertext",
    "cipherkey": "cipherkey",
    "iv": "iv",
}


def test_retrieve_chat_response_decodes_api_v1_envelope_with_request_id(monkeypatch):
    """API v1 retrieval should poll by request_id and append the assistant reply."""
    client = CryptoClient("https://test-server.com")
    client.client_public_key_b64 = "client-key"
    sent_payloads = []

    def fake_post(url, json, timeout):
        sent_payloads.append((url, dict(json), timeout))
        return _FakeResponse(200, ENCRYPTED_RELAY_RESPONSE)

    monkeypatch.setattr("utils.crypto_helpers.requests.post", fake_post)
    monkeypatch.setattr(
        client,
        "decrypt_message",
        lambda _encrypted: {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "request_id": "req-1",
            "api_v1_response": {
                "message": {"role": "assistant", "content": "done"},
            },
        },
    )

    response = client.retrieve_chat_response(
        max_retries=1,
        retry_delay=0,
        expected_request_id="req-1",
        chat_history=[{"role": "user", "content": "hi"}],
    )

    assert sent_payloads == [
        (
            "https://test-server.com/api/v1/relay/responses/retrieve",
            {"client_public_key": "client-key", "request_id": "req-1"},
            10,
        )
    ]
    assert response == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.parametrize(
    "decrypted_envelope",
    [
        {
            "protocol": "unexpected",
            "api_v1_response": {"message": {"role": "assistant", "content": "done"}},
        },
        {"protocol": "tokenplace_api_v1_relay_e2ee"},
        {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "api_v1_response": {"error": {"message": "boom"}},
        },
        {"protocol": "tokenplace_api_v1_relay_e2ee", "api_v1_response": {}},
        {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "api_v1_response": {"message": {"role": "assistant"}},
        },
    ],
)
def test_retrieve_chat_response_rejects_invalid_api_v1_envelopes(
    monkeypatch,
    decrypted_envelope,
):
    """Malformed API v1 response envelopes should fail closed."""
    client = CryptoClient("https://test-server.com")
    client.client_public_key_b64 = "client-key"
    monkeypatch.setattr(
        "utils.crypto_helpers.requests.post",
        lambda _url, json, timeout: _FakeResponse(200, ENCRYPTED_RELAY_RESPONSE),
    )
    monkeypatch.setattr(client, "decrypt_message", lambda _encrypted: decrypted_envelope)

    assert client.retrieve_chat_response(max_retries=1, retry_delay=0) is None


def test_retrieve_chat_response_retries_mismatched_api_v1_request_id(monkeypatch):
    """A response for another request_id should not satisfy this retrieval."""
    client = CryptoClient("https://test-server.com")
    client.client_public_key_b64 = "client-key"
    sleep_calls = []

    monkeypatch.setattr(
        "utils.crypto_helpers.requests.post",
        lambda _url, json, timeout: _FakeResponse(200, ENCRYPTED_RELAY_RESPONSE),
    )
    monkeypatch.setattr(
        client,
        "decrypt_message",
        lambda _encrypted: {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "request_id": "other-request",
            "api_v1_response": {
                "message": {"role": "assistant", "content": "wrong"},
            },
        },
    )
    monkeypatch.setattr("utils.crypto_helpers.time.sleep", lambda seconds: sleep_calls.append(seconds))

    assert client.retrieve_chat_response(
        max_retries=1,
        retry_delay=0,
        expected_request_id="expected-request",
    ) is None
    assert sleep_calls == [0]


def test_retrieve_chat_response_returns_api_v1_message_without_original_history(monkeypatch):
    """Direct polling callers still get the assistant message when no history is supplied."""
    client = CryptoClient("https://test-server.com")
    client.client_public_key_b64 = "client-key"

    monkeypatch.setattr(
        "utils.crypto_helpers.requests.post",
        lambda _url, json, timeout: _FakeResponse(200, ENCRYPTED_RELAY_RESPONSE),
    )
    monkeypatch.setattr(
        client,
        "decrypt_message",
        lambda _encrypted: {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "request_id": "req-1",
            "api_v1_response": {
                "message": {"role": "assistant", "content": "done"},
            },
        },
    )

    assert client.retrieve_chat_response(
        max_retries=1,
        retry_delay=0,
        expected_request_id="req-1",
    ) == [{"role": "assistant", "content": "done"}]
