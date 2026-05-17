import base64
import json
import threading
import time
from urllib.parse import urlparse

import pytest
import requests
from werkzeug.serving import make_server

import relay
from api.v1 import compute_provider
from api.v1.encryption import encryption_manager
from encrypt import decrypt, encrypt, generate_keys
from utils.crypto.crypto_manager import CryptoManager
from utils.networking.relay_client import RelayClient

LEGACY_PATHS = {
    "/sink",
    "/faucet",
    "/source",
    "/next_server",
    "/stream/source",
    "/stream/retrieve",
}


class _ServerThread:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.thread.join(timeout=5)


class FakeDesktopModelManager:
    use_mock_llm = True

    def llama_cpp_get_response(self, messages):
        return list(messages) + [{"role": "assistant", "content": "pong from desktop"}]


def _reset_relay_state():
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()


@pytest.fixture(autouse=True)
def relay_state():
    _reset_relay_state()
    compute_provider._build_api_v1_compute_provider.cache_clear()
    yield
    _reset_relay_state()
    compute_provider._build_api_v1_compute_provider.cache_clear()


def _api_v1_browser_encrypt_messages(messages):
    ciphertext_dict, cipherkey, iv = encrypt(
        json.dumps(messages).encode("utf-8"),
        base64.b64decode(encryption_manager.public_key_b64),
        use_pkcs1v15=True,
    )
    return {
        "ciphertext": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
        "cipherkey": base64.b64encode(cipherkey).decode("utf-8"),
        "iv": base64.b64encode(iv).decode("utf-8"),
    }


def _decrypt_browser_response(encrypted_payload, browser_private_key):
    decrypted = decrypt(
        {
            "ciphertext": base64.b64decode(encrypted_payload["ciphertext"]),
            "iv": base64.b64decode(encrypted_payload["iv"]),
        },
        base64.b64decode(encrypted_payload["cipherkey"]),
        browser_private_key,
    )
    return json.loads(decrypted.decode("utf-8"))


def _payload_contains_text(payload, text):
    return text in json.dumps(payload, sort_keys=True, default=str)


def _install_legacy_route_guard(monkeypatch):
    original_request = requests.sessions.Session.request
    calls = []

    def guarded(self, method, url, *args, **kwargs):
        path = urlparse(url).path
        if path in LEGACY_PATHS or path.startswith("/api/v2"):
            calls.append((method, path))
            raise AssertionError(f"legacy/API v2 route used: {method} {path}")
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", guarded)
    return calls


def test_api_v1_encrypted_desktop_bridge_round_trips_relay_blind_e2ee(monkeypatch):
    legacy_calls = _install_legacy_route_guard(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")

    server_crypto = CryptoManager()
    desktop = RelayClient(
        base_url="http://127.0.0.1",
        port=None,
        crypto_manager=server_crypto,
        model_manager=FakeDesktopModelManager(),
        include_configured_servers=False,
    )
    desktop._request_timeout = 1
    processed = threading.Event()
    failures = []
    observed_requests = []
    observed_responses = []
    original_queue_client_response = relay._queue_client_response

    def queue_client_response_spy(client_public_key, envelope):
        observed_responses.append(dict(envelope))
        return original_queue_client_response(client_public_key, envelope)

    monkeypatch.setattr(relay, "_queue_client_response", queue_client_response_spy)

    def desktop_loop():
        deadline = time.time() + 8
        while time.time() < deadline and not processed.is_set():
            try:
                payload = desktop.poll_api_v1_encrypted_work()
                if payload.get("protocol") == "tokenplace_api_v1_relay_e2ee":
                    observed_requests.append(dict(payload))
                    if desktop.process_client_request(payload):
                        processed.set()
                        return
                    failures.append("process_client_request returned false")
                    return
            except Exception as exc:  # pragma: no cover
                failures.append(repr(exc))
                return
            time.sleep(0.05)
        failures.append("desktop loop timed out")

    with _ServerThread(relay.app) as running:
        desktop._relay_urls = (running.base_url,)
        assert desktop.register_api_v1_compute_node(running.base_url).get("error") is None

        desktop_thread = threading.Thread(target=desktop_loop, daemon=True)
        desktop_thread.start()

        browser_private_key, browser_public_key = generate_keys()
        browser_public_key_b64 = base64.b64encode(browser_public_key).decode("utf-8")
        response = requests.post(
            f"{running.base_url}/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                "client_public_key": browser_public_key_b64,
                "messages": _api_v1_browser_encrypt_messages(
                    [{"role": "user", "content": "ping from browser"}]
                ),
                "metadata": {
                    "inference_target": "desktop_bridge_api_v1_e2ee",
                    "relay_path": "api_v1_e2ee",
                },
            },
            timeout=10,
        )
        desktop_thread.join(timeout=5)

    assert not failures
    assert processed.is_set()
    assert legacy_calls == []
    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert (
        response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"]
        == "distributed_relay_e2ee"
    )
    body = response.json()
    assert body["encrypted"] is True
    decrypted = _decrypt_browser_response(body["data"], browser_private_key)
    assert decrypted["object"] == "chat.completion"
    assert decrypted["choices"][0]["message"]["content"] == "pong from desktop"

    assert observed_requests
    assert observed_responses
    for envelope in observed_requests + observed_responses:
        assert {"ciphertext", "cipherkey", "iv"}.issubset(envelope)
    visible_relay_state = {
        "known_servers": relay.known_servers,
        "queued_requests": observed_requests,
        "queued_responses": observed_responses,
    }
    assert not _payload_contains_text(visible_relay_state, "ping from browser")
    assert not _payload_contains_text(visible_relay_state, "pong from desktop")


def test_api_v1_stream_true_fails_before_relay_queue():
    response = relay.app.test_client().post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "stream": True,
            "messages": [{"role": "user", "content": "do not queue"}],
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        },
    )
    assert response.status_code == 400
    assert relay.client_inference_requests == {}


def test_api_v1_chat_image_content_fails_before_relay_queue():
    client = relay.app.test_client()
    plaintext_response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc"},
                        },
                    ],
                }
            ],
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        },
    )
    assert plaintext_response.status_code == 400
    assert relay.client_inference_requests == {}

    _, browser_public_key = generate_keys()
    encrypted_response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": base64.b64encode(browser_public_key).decode("utf-8"),
            "messages": _api_v1_browser_encrypt_messages(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,abc",
                            }
                        ],
                    }
                ]
            ),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        },
    )
    assert encrypted_response.status_code == 400
    assert relay.client_inference_requests == {}


def test_api_v1_response_retrieval_by_request_id_preserves_mismatches():
    client = relay.app.test_client()
    client_key = "client-key"
    first = {
        "client_public_key": client_key,
        "request_id": "req-a",
        "ciphertext": "c1",
        "cipherkey": "k1",
        "iv": "i1",
    }
    second = {
        "client_public_key": client_key,
        "request_id": "req-b",
        "ciphertext": "c2",
        "cipherkey": "k2",
        "iv": "i2",
    }
    relay._queue_client_response(client_key, dict(first))
    relay._queue_client_response(client_key, dict(second))

    missing = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": client_key, "request_id": "req-missing"},
    )
    assert missing.status_code == 404
    assert relay.client_responses[client_key][0]["request_id"] == "req-a"
    assert relay.client_responses[client_key][1]["request_id"] == "req-b"

    matched = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": client_key, "request_id": "req-b"},
    )
    assert matched.status_code == 200
    assert matched.get_json()["request_id"] == "req-b"
    assert relay.client_responses[client_key]["request_id"] == "req-a"
