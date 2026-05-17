"""End-to-end coverage for API v1 desktop bridge relay-blind E2EE."""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import closing
from typing import Any
from urllib.parse import urlparse

import pytest
import requests
from werkzeug.serving import make_server

import relay
from api.v1 import routes
from api.v1.compute_provider import DistributedApiV1ComputeProvider
from utils.crypto.crypto_manager import CryptoManager
from utils.networking.relay_client import RelayClient


PLAINTEXT_USER = "ping from browser"
PLAINTEXT_ASSISTANT = "pong from desktop"
LEGACY_PATHS = {
    "/sink",
    "/faucet",
    "/source",
    "/retrieve",
    "/next_server",
    "/stream/source",
}


class _ServerThread(threading.Thread):
    def __init__(self, app, host: str, port: int):
        super().__init__(daemon=True)
        self.server = make_server(host, port, app, threaded=True)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.ctx.pop()


class _FakeDesktopModelManager:
    use_mock_llm = True

    def supports_api_v1_model(self, model_id: str) -> bool:
        return model_id == "llama-3-8b-instruct"

    def llama_cpp_get_response(self, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        assert messages == [{"role": "user", "content": PLAINTEXT_USER}]
        return [*messages, {"role": "assistant", "content": PLAINTEXT_ASSISTANT}]


@pytest.fixture(autouse=True)
def _reset_relay_state():
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    yield
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()


@pytest.fixture
def relay_server():
    relay.app.config["TESTING"] = True
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    server = _ServerThread(relay.app, host, port)
    server.start()
    base_url = f"http://{host}:{port}"
    try:
        for _ in range(50):
            try:
                requests.get(f"{base_url}/api/v1/health", timeout=0.2)
                break
            except requests.RequestException:
                time.sleep(0.02)
        yield base_url
    finally:
        server.shutdown()
        server.join(timeout=5)


@pytest.fixture
def forbid_legacy_requests(monkeypatch):
    original_request = requests.sessions.Session.request
    seen: dict[str, list[Any]] = {"paths": [], "relay_payloads": []}

    def guarded_request(self, method, url, **kwargs):
        path = urlparse(url).path.rstrip("/") or "/"
        seen["paths"].append(path)
        if path in {"/api/v1/relay/requests", "/api/v1/relay/responses"}:
            seen["relay_payloads"].append(kwargs.get("json"))
        assert path not in LEGACY_PATHS, f"legacy relay endpoint called: {method} {path}"
        assert "/api/v2" not in path, f"API v2 endpoint called: {method} {path}"
        return original_request(self, method, url, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", guarded_request)
    return seen


def _browser_api_v1_payload(browser_crypto: CryptoManager) -> dict[str, Any]:
    encrypted_messages = routes.encryption_manager.encrypt_message(
        [{"role": "user", "content": PLAINTEXT_USER}],
        routes.encryption_manager.public_key_b64,
    )
    assert encrypted_messages is not None
    return {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "client_public_key": browser_crypto.public_key_b64,
        "messages": encrypted_messages,
        "metadata": {
            "inference_target": "desktop_bridge_api_v1_e2ee",
            "relay_path": "api_v1_e2ee",
        },
    }


def test_api_v1_encrypted_desktop_bridge_round_trips_relay_blind(
    relay_server,
    forbid_legacy_requests,
    monkeypatch,
):
    """Browser-shaped encrypted API v1 requests complete through desktop relay E2EE."""

    browser_crypto = CryptoManager()
    desktop_client = RelayClient(
        base_url=relay_server,
        port=None,
        crypto_manager=CryptoManager(),
        model_manager=_FakeDesktopModelManager(),
        include_configured_servers=False,
    )
    provider = DistributedApiV1ComputeProvider(base_url=relay_server, timeout_seconds=3)

    monkeypatch.setattr(routes, "get_api_v1_compute_provider_for_mode", lambda **_kwargs: provider)
    monkeypatch.setattr(routes, "get_api_v1_resolved_provider_path", lambda _provider: "distributed")

    stop_event = threading.Event()
    desktop_errors: list[BaseException] = []

    def desktop_loop() -> None:
        while not stop_event.is_set():
            try:
                work = desktop_client.poll_api_v1_encrypted_work()
                if work.get("protocol") == "tokenplace_api_v1_relay_e2ee":
                    desktop_client.process_client_request(work)
                    return
                time.sleep(0.02)
            except BaseException as exc:  # pragma: no cover - surfaced below
                desktop_errors.append(exc)
                return

    desktop_thread = threading.Thread(target=desktop_loop, daemon=True)
    desktop_thread.start()

    response = requests.post(
        f"{relay_server}/api/v1/chat/completions",
        json=_browser_api_v1_payload(browser_crypto),
        timeout=5,
    )
    stop_event.set()
    desktop_thread.join(timeout=2)

    assert not desktop_errors
    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Provider"] == "DistributedApiV1ComputeProvider"
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"] == "distributed_relay_e2ee"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"

    body = response.json()
    assert body["encrypted"] is True
    encrypted_response = body["data"]
    decrypted_completion = browser_crypto.decrypt_message(
        {
            "chat_history": encrypted_response["ciphertext"],
            "cipherkey": encrypted_response["cipherkey"],
            "iv": encrypted_response["iv"],
        }
    )

    assert decrypted_completion["object"] == "chat.completion"
    assert decrypted_completion["choices"][0]["message"]["content"] == PLAINTEXT_ASSISTANT
    assert decrypted_completion["metadata"] == {
        "inference_target": "desktop_bridge_api_v1_e2ee",
        "relay_path": "api_v1_e2ee",
    }

    seen_paths = forbid_legacy_requests["paths"]
    assert "/api/v1/relay/requests" in seen_paths
    assert "/api/v1/relay/responses" in seen_paths
    assert "/api/v1/relay/responses/retrieve" in seen_paths

    relay_visible_payloads = forbid_legacy_requests["relay_payloads"]
    assert len(relay_visible_payloads) >= 2
    for payload in relay_visible_payloads:
        assert {"chat_history", "cipherkey", "iv"}.issubset(payload.keys())
        payload_json = json.dumps(payload)
        assert PLAINTEXT_USER not in payload_json
        assert PLAINTEXT_ASSISTANT not in payload_json

    response_posts = [path for path in seen_paths if path == "/api/v1/relay/responses"]
    assert response_posts, "desktop must post encrypted response to API v1 relay responses"
