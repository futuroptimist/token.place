import base64
import json
import socket
import threading
import time
from contextlib import closing
from urllib.parse import urlparse

import pytest
import requests
from werkzeug.serving import make_server

from encrypt import encrypt
import relay
from relay import app, client_inference_requests, client_responses, known_servers
from utils.crypto.crypto_manager import CryptoManager
from utils.networking.relay_client import RelayClient


LEGACY_RELAY_PATHS = {
    "/sink",
    "/faucet",
    "/source",
    "/next_server",
    "/stream/source",
    "/stream/retrieve",
    "/retrieve",
}


class _FakeDesktopModelManager:
    api_model_id = "llama-3-8b-instruct"
    use_mock_llm = False

    def supports_api_v1_model(self, model_id):
        return model_id == "llama-3-8b-instruct"

    def llama_cpp_get_response(self, messages):
        assert messages == [{"role": "user", "content": "ping from browser"}]
        return [*messages, {"role": "assistant", "content": "pong from desktop"}]


@pytest.fixture(autouse=True)
def _reset_relay_state(monkeypatch):
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()
    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "")
    yield
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()


@pytest.fixture
def relay_server(monkeypatch):
    app.config["TESTING"] = True
    original_request = requests.sessions.Session.request
    original_queue_client_response = relay._queue_client_response
    legacy_calls = []
    queued_responses = []

    def guard_legacy_routes(session, method, url, **kwargs):
        path = urlparse(url).path
        if path in LEGACY_RELAY_PATHS or path.startswith("/api/v2"):
            legacy_calls.append((method, path))
            raise AssertionError(f"legacy/API v2 route used in API v1 E2EE path: {method} {path}")
        return original_request(session, method, url, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", guard_legacy_routes)

    def record_queued_response(client_public_key, envelope):
        queued_responses.append(dict(envelope))
        return original_queue_client_response(client_public_key, envelope)

    monkeypatch.setattr(relay, "_queue_client_response", record_queued_response)

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, legacy_calls, queued_responses
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _browser_encrypt_messages(messages, server_public_key_b64):
    public_key = base64.b64decode(server_public_key_b64)
    ciphertext_dict, cipherkey, iv = encrypt(json.dumps(messages).encode("utf-8"), public_key)
    return {
        "ciphertext": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
        "cipherkey": base64.b64encode(cipherkey).decode("utf-8"),
        "iv": base64.b64encode(iv).decode("utf-8"),
    }


def _assert_relay_visible_payload_is_ciphertext_only(payload, *plaintext_needles):
    encoded = json.dumps(payload, sort_keys=True)
    assert "cipherkey" in payload
    assert "iv" in payload
    assert "chat_history" in payload or "ciphertext" in payload
    for needle in plaintext_needles:
        assert needle not in encoded


def test_api_v1_encrypted_desktop_bridge_round_trip(relay_server):
    base_url, legacy_calls, queued_responses = relay_server
    client_crypto = CryptoManager()

    desktop_client = RelayClient(
        base_url=base_url,
        port=None,
        crypto_manager=CryptoManager(),
        model_manager=_FakeDesktopModelManager(),
        include_configured_servers=False,
    )

    register_response = desktop_client.register_api_v1_compute_node(base_url)
    assert "error" not in register_response

    provider_done = threading.Event()
    provider_result = {}

    def run_browser_request():
        try:
            public_key_response = requests.get(f"{base_url}/api/v1/public-key", timeout=5)
            public_key_response.raise_for_status()
            server_public_key = public_key_response.json()["public_key"]
            payload = {
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                "client_public_key": client_crypto.public_key_b64,
                "messages": _browser_encrypt_messages(
                    [{"role": "user", "content": "ping from browser"}],
                    server_public_key,
                ),
                "metadata": {
                    "inference_target": "desktop_bridge_api_v1_e2ee",
                    "relay_path": "api_v1_e2ee",
                },
            }
            provider_result["response"] = requests.post(
                f"{base_url}/api/v1/chat/completions",
                json=payload,
                timeout=10,
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertions below
            provider_result["exception"] = exc
        finally:
            provider_done.set()

    browser_thread = threading.Thread(target=run_browser_request, daemon=True)
    browser_thread.start()

    work_item = None
    deadline = time.time() + 5
    while time.time() < deadline and work_item is None:
        polled = desktop_client.poll_api_v1_encrypted_work()
        if polled.get("protocol") == "tokenplace_api_v1_relay_e2ee":
            work_item = polled
            break
        time.sleep(0.05)

    assert work_item is not None
    _assert_relay_visible_payload_is_ciphertext_only(
        work_item,
        "ping from browser",
        "pong from desktop",
    )

    assert desktop_client.process_client_request(work_item) is True

    assert provider_done.wait(timeout=10)
    browser_thread.join(timeout=1)
    assert "exception" not in provider_result
    response = provider_result["response"]

    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Provider"] == "DistributedApiV1ComputeProvider"
    assert (
        response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"]
        == "distributed_relay_e2ee"
    )
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"

    response_payload = response.json()
    assert response_payload["encrypted"] is True
    encrypted_data = dict(response_payload["data"])
    encrypted_data["chat_history"] = encrypted_data.pop("ciphertext")
    decrypted_payload = client_crypto.decrypt_message(encrypted_data)

    assert decrypted_payload["object"] == "chat.completion"
    assert decrypted_payload["choices"][0]["message"]["content"] == "pong from desktop"
    assert decrypted_payload["metadata"] == {
        "inference_target": "desktop_bridge_api_v1_e2ee",
        "relay_path": "api_v1_e2ee",
    }
    assert legacy_calls == []
    assert len(queued_responses) == 1
    _assert_relay_visible_payload_is_ciphertext_only(
        queued_responses[0],
        "ping from browser",
        "pong from desktop",
    )

    # The queued relay request/response surfaces are relay-blind: only ciphertext envelopes
    # and safe routing metadata are visible to relay-owned state.
    assert client_inference_requests == {}
    assert client_responses == {}


def test_api_v1_stream_true_fails_before_relay_work(relay_server):
    base_url, _legacy_calls, _queued_responses = relay_server
    response = requests.post(
        f"{base_url}/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=5,
    )

    assert 400 <= response.status_code < 500
    assert response.json()["error"]["param"] == "stream"
    assert client_inference_requests == {}


@pytest.mark.parametrize("encrypted", [False, True])
def test_api_v1_image_content_fails_before_relay_work(relay_server, encrypted):
    base_url, _legacy_calls, _queued_responses = relay_server
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaa"}},
            ],
        }
    ]

    payload = {"model": "llama-3-8b-instruct"}
    if encrypted:
        client_crypto = CryptoManager()
        public_key_response = requests.get(f"{base_url}/api/v1/public-key", timeout=5)
        server_public_key = public_key_response.json()["public_key"]
        payload.update(
            {
                "encrypted": True,
                "client_public_key": client_crypto.public_key_b64,
                "messages": _browser_encrypt_messages(messages, server_public_key),
                "metadata": {
                    "inference_target": "desktop_bridge_api_v1_e2ee",
                    "relay_path": "api_v1_e2ee",
                },
            }
        )
    else:
        payload["messages"] = messages

    response = requests.post(f"{base_url}/api/v1/chat/completions", json=payload, timeout=5)

    assert 400 <= response.status_code < 500
    assert "text-only" in response.json()["error"]["message"]
    assert client_inference_requests == {}


def test_api_v1_response_retrieve_matches_request_id_without_consuming_mismatches():
    client_public_key = "client-key"
    client_responses[client_public_key] = [
        {
            "client_public_key": client_public_key,
            "request_id": "req-a",
            "chat_history": "a",
            "cipherkey": "k",
            "iv": "i",
        },
        {
            "client_public_key": client_public_key,
            "request_id": "req-b",
            "chat_history": "b",
            "cipherkey": "k",
            "iv": "i",
        },
    ]

    with app.test_client() as client:
        response = client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": client_public_key, "request_id": "req-b"},
        )
        assert response.status_code == 200
        assert response.get_json()["request_id"] == "req-b"

        response = client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": client_public_key, "request_id": "req-missing"},
        )
        assert response.status_code == 404

        response = client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": client_public_key, "request_id": "req-a"},
        )
        assert response.status_code == 200
        assert response.get_json()["request_id"] == "req-a"

    assert client_public_key not in client_responses
