"""End-to-end API v1 desktop bridge E2EE relay regression tests.

Manual staging verification snippet:
1. Run desktop compute node against ``https://staging.token.place``.
2. Confirm ``GET /healthz`` reports ``knownServers >= 1``.
3. Confirm ``GET /relay/diagnostics`` includes the registered node public key.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest
import requests
from werkzeug.serving import make_server

import relay
from api.v1 import compute_provider, routes
from api.v1.encryption import EncryptionManager, encryption_manager
from utils.crypto.crypto_manager import CryptoManager
from utils.networking import relay_client as relay_client_module
from utils.networking.relay_client import RelayClient


LEGACY_ROUTE_FRAGMENTS = (
    "/sink",
    "/faucet",
    "/source",
    "/next_server",
    "/stream/source",
    "/stream/",
    "/api/v2",
)


class FakeDesktopRuntime:
    """Non-streaming llama.cpp-compatible runtime used by the desktop bridge."""

    def __init__(self):
        self.calls = []

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        assert tokenize is False
        return "".join(
            f"<{message['role']}>{message['content']}" for message in messages
        ) + ("<assistant>" if add_generation_prompt else "")

    def tokenize(self, payload, _add_bos=False):
        return list(range(len(payload)))

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs.get("stream") is False
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "pong from desktop",
                    }
                }
            ]
        }


class FakeDesktopModelManager:
    """Small fake desktop model that never loads llama.cpp/GPU."""

    use_mock_llm = False
    api_model_id = "qwen3-8b-instruct"
    model_id = "qwen3-8b-instruct"
    file_name = "Qwen3-8B-Q4_K_M.gguf"
    model_path = "/models/Qwen3-8B-Q4_K_M.gguf"

    def __init__(self):
        self.runtime = FakeDesktopRuntime()

    def get_llm_instance(self):
        return self.runtime

    def llama_cpp_get_response(self, messages):
        raise AssertionError(
            "API v1 must not use legacy streaming llama_cpp_get_response "
            "when direct completion exists"
        )


@contextmanager
def live_relay_server():
    server = make_server("127.0.0.1", 0, relay.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture(autouse=True)
def reset_relay_state(monkeypatch):
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_pending_request_ids.clear()
    relay.client_terminal_request_ids.clear()
    relay.client_responses.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()
    compute_provider._build_api_v1_compute_provider.cache_clear()
    monkeypatch.delenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", raising=False)
    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", raising=False)
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "off")
    yield
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_pending_request_ids.clear()
    relay.client_terminal_request_ids.clear()
    relay.client_responses.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()
    compute_provider._build_api_v1_compute_provider.cache_clear()


def _encrypt_browser_messages(messages):
    encrypted = encryption_manager.encrypt_message(
        messages,
        encryption_manager.public_key_b64,
    )
    assert encrypted is not None
    return {
        "ciphertext": encrypted["ciphertext"],
        "cipherkey": encrypted["cipherkey"],
        "iv": encrypted["iv"],
    }


def _decrypt_browser_response(browser_crypto: EncryptionManager, response_body):
    encrypted = response_body["data"]
    decrypted = browser_crypto.decrypt_message(
        {
            "ciphertext": base64.b64decode(encrypted["ciphertext"]),
            "iv": base64.b64decode(encrypted["iv"]),
        },
        base64.b64decode(encrypted["cipherkey"]),
    )
    assert decrypted is not None
    return json.loads(decrypted.decode("utf-8"))


def _assert_ciphertext_only(payload, *, forbidden_text):
    serialized = json.dumps(payload, sort_keys=True)
    assert "cipherkey" in payload
    assert "iv" in payload
    assert "chat_history" in payload or "ciphertext" in payload
    assert forbidden_text not in serialized


def _start_fake_desktop_loop(base_url, done_event):
    model_manager = FakeDesktopModelManager()
    desktop_client = RelayClient(
        base_url=base_url,
        port=None,
        crypto_manager=CryptoManager(),
        model_manager=model_manager,
        include_configured_servers=False,
    )
    desktop_client._request_timeout = 2

    register = desktop_client.register_api_v1_compute_node(base_url)
    assert register["next_ping_in_x_seconds"] > 0

    def worker():
        deadline = time.time() + 5
        while time.time() < deadline and not done_event.is_set():
            payload = desktop_client.poll_api_v1_encrypted_work()
            if payload.get("protocol") == "tokenplace_api_v1_relay_e2ee":
                desktop_client.process_client_request(payload)
                done_event.set()
                return
            time.sleep(0.05)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, model_manager


def test_api_v1_encrypted_desktop_bridge_round_trip(monkeypatch):
    """Browser-shaped encrypted API v1 chat completes via relay-blind desktop bridge."""

    observed_posts = []
    observed_retrieve_statuses = []
    real_request = requests.sessions.Session.request
    real_relay_post = relay_client_module.requests.post

    def guard_legacy_requests(self, method, url, **kwargs):
        path = urlparse(url).path
        if any(fragment in path for fragment in LEGACY_ROUTE_FRAGMENTS):
            raise AssertionError(f"legacy/API v2 route used in API v1 bridge: {path}")
        response = real_request(self, method, url, **kwargs)
        if method.upper() == "POST" and path in {
            "/api/v1/relay/requests",
            "/api/v1/relay/responses",
        }:
            observed_posts.append((path, kwargs.get("json")))
        if method.upper() == "POST" and path == "/api/v1/relay/responses/retrieve":
            observed_retrieve_statuses.append(response.status_code)
        return response

    def guard_relay_client_post(url, *args, **kwargs):
        path = urlparse(url).path
        if any(fragment in path for fragment in LEGACY_ROUTE_FRAGMENTS):
            raise AssertionError(f"legacy/API v2 route used in API v1 bridge: {path}")
        if path in {
            "/api/v1/relay/requests",
            "/api/v1/relay/responses",
        }:
            observed_posts.append((path, kwargs.get("json")))
        return real_relay_post(url, *args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", guard_legacy_requests)
    monkeypatch.setattr(relay_client_module.requests, "post", guard_relay_client_post)

    with live_relay_server() as base_url:
        monkeypatch.setattr(
            routes,
            "get_api_v1_compute_provider_for_mode",
            lambda **_kwargs: compute_provider.DistributedApiV1ComputeProvider(
                base_url=base_url,
                timeout_seconds=5,
            ),
        )

        desktop_done = threading.Event()
        desktop_thread, model_manager = _start_fake_desktop_loop(base_url, desktop_done)

        browser_crypto = EncryptionManager()
        user_text = "ping from browser"
        payload = {
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": browser_crypto.public_key_b64,
            "messages": _encrypt_browser_messages(
                [{"role": "user", "content": user_text}]
            ),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        }

        response = requests.post(
            f"{base_url}/api/v1/chat/completions",
            json=payload,
            timeout=10,
        )
        desktop_thread.join(timeout=5)

    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert (
        response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"]
        == "distributed_relay_e2ee"
    )
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"
    assert desktop_done.is_set()

    body = response.json()
    assert body["encrypted"] is True
    completion = _decrypt_browser_response(browser_crypto, body)
    assert completion["object"] == "chat.completion"
    assert completion["choices"][0]["message"]["content"] == "pong from desktop"
    assert len(model_manager.runtime.calls) == 1
    assert model_manager.runtime.calls[0]["stream"] is False

    request_posts = [
        payload for path, payload in observed_posts if path == "/api/v1/relay/requests"
    ]
    response_posts = [
        payload for path, payload in observed_posts if path == "/api/v1/relay/responses"
    ]
    assert len(request_posts) == 1
    assert len(response_posts) == 1
    assert 200 in observed_retrieve_statuses
    _assert_ciphertext_only(request_posts[0], forbidden_text=user_text)
    _assert_ciphertext_only(request_posts[0], forbidden_text="pong from desktop")
    _assert_ciphertext_only(response_posts[0], forbidden_text=user_text)
    _assert_ciphertext_only(response_posts[0], forbidden_text="pong from desktop")


def test_api_v1_encrypted_desktop_bridge_three_sequential_turns_clear_queue(monkeypatch):
    """One desktop node drains three sequential browser turns without stale queue depth."""

    with live_relay_server() as base_url:
        monkeypatch.setattr(
            routes,
            "get_api_v1_compute_provider_for_mode",
            lambda **_kwargs: compute_provider.DistributedApiV1ComputeProvider(
                base_url=base_url,
                timeout_seconds=5,
            ),
        )

        model_manager = FakeDesktopModelManager()
        desktop_client = RelayClient(
            base_url=base_url,
            port=None,
            crypto_manager=CryptoManager(),
            model_manager=model_manager,
            include_configured_servers=False,
        )
        desktop_client._request_timeout = 2
        assert desktop_client.register_api_v1_compute_node(base_url)["next_ping_in_x_seconds"] > 0

        stop_desktop = threading.Event()
        processed_request_ids = []

        def desktop_worker():
            deadline = time.time() + 10
            while time.time() < deadline and not stop_desktop.is_set():
                payload = desktop_client.poll_api_v1_encrypted_work()
                if payload.get("protocol") == "tokenplace_api_v1_relay_e2ee":
                    processed_request_ids.append(payload["request_id"])
                    desktop_client.process_client_request(payload)
                    if len(processed_request_ids) >= 3:
                        stop_desktop.set()
                        return
                    continue
                time.sleep(0.01)

        thread = threading.Thread(target=desktop_worker, daemon=True)
        thread.start()

        browser_crypto = EncryptionManager()
        for index in range(3):
            response = requests.post(
                f"{base_url}/api/v1/chat/completions",
                json={
                    "model": "llama-3-8b-instruct",
                    "encrypted": True,
                    "client_public_key": browser_crypto.public_key_b64,
                    "messages": _encrypt_browser_messages([{"role": "user", "content": f"turn {index}"}]),
                    "metadata": {
                        "inference_target": "desktop_bridge_api_v1_e2ee",
                        "relay_path": "api_v1_e2ee",
                    },
                },
                timeout=10,
            )
            assert response.status_code == 200, response.text
            completion = _decrypt_browser_response(browser_crypto, response.json())
            assert completion["choices"][0]["message"]["content"] == "pong from desktop"

            diagnostics = requests.get(f"{base_url}/relay/diagnostics", timeout=5).json()
            nodes = diagnostics["registered_compute_nodes"]
            assert len(nodes) == 1
            assert nodes[0]["queue_depth"] == 0

        thread.join(timeout=5)

    assert len(processed_request_ids) == 3
    assert len(model_manager.runtime.calls) == 3


def test_api_v1_desktop_bridge_without_response_post_times_out_instead_of_passing(
    monkeypatch,
):
    """Repeated retrieve 202 without a desktop /responses post is a failure signal."""

    retrieve_statuses = []
    real_request = requests.sessions.Session.request

    def observe_retrieve_pending(self, method, url, **kwargs):
        response = real_request(self, method, url, **kwargs)
        if (
            method.upper() == "POST"
            and urlparse(url).path == "/api/v1/relay/responses/retrieve"
        ):
            retrieve_statuses.append(response.status_code)
        return response

    monkeypatch.setattr(requests.sessions.Session, "request", observe_retrieve_pending)

    with live_relay_server() as base_url:
        desktop_client = RelayClient(
            base_url=base_url,
            port=None,
            crypto_manager=CryptoManager(),
            model_manager=FakeDesktopModelManager(),
            include_configured_servers=False,
        )
        assert desktop_client.register_api_v1_compute_node(base_url)["next_ping_in_x_seconds"] > 0

        monkeypatch.setattr(
            routes,
            "get_api_v1_compute_provider_for_mode",
            lambda **_kwargs: compute_provider.DistributedApiV1ComputeProvider(
                base_url=base_url,
                timeout_seconds=1,
            ),
        )

        browser_crypto = EncryptionManager()
        response = requests.post(
            f"{base_url}/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                "client_public_key": browser_crypto.public_key_b64,
                "messages": _encrypt_browser_messages(
                    [{"role": "user", "content": "never answered"}]
                ),
                "metadata": {
                    "inference_target": "desktop_bridge_api_v1_e2ee",
                    "relay_path": "api_v1_e2ee",
                },
            },
            timeout=5,
        )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "compute_node_timeout"
    assert retrieve_statuses
    assert set(retrieve_statuses) == {202}


def test_api_v1_stream_true_fails_before_relay_queue():
    relay.client_inference_requests.clear()
    with relay.app.test_client() as client:
        response = client.post(
            "/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert 400 <= response.status_code < 500
    assert response.get_json()["error"]["param"] == "stream"
    assert relay.client_inference_requests == {}


def test_api_v1_desktop_bridge_registration_poll_no_work_heartbeat():
    """Desktop node register/poll should treat no queued work as healthy heartbeat."""

    with live_relay_server() as base_url:
        desktop_client = RelayClient(
            base_url=base_url,
            port=None,
            crypto_manager=CryptoManager(),
            model_manager=FakeDesktopModelManager(),
            include_configured_servers=False,
        )
        register = desktop_client.register_api_v1_compute_node(base_url)
        assert register["next_ping_in_x_seconds"] > 0
        assert register["poll_wait_seconds"] > 0

        poll_payload = desktop_client.poll_api_v1_encrypted_work()
        assert poll_payload["message"] == "No requests available"
        assert poll_payload["poll_wait_seconds"] > 0
        assert poll_payload["next_ping_in_x_seconds"] == 0



def test_api_v1_desktop_bridge_reregisters_after_idle_no_work_before_browser_request(monkeypatch):
    """A desktop node that idled after no-work polling should renew before browser work."""

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0.01")

    with live_relay_server() as base_url:
        desktop_client = RelayClient(
            base_url=base_url,
            port=None,
            crypto_manager=CryptoManager(),
            model_manager=FakeDesktopModelManager(),
            include_configured_servers=False,
        )
        desktop_client._request_timeout = 1
        server_key = desktop_client.crypto_manager.public_key_b64

        first_no_work = desktop_client.poll_api_v1_encrypted_work()
        assert first_no_work["message"] == "No requests available"
        assert server_key in relay.known_servers

        relay.known_servers[server_key]["last_ping"] = datetime.now() - timedelta(seconds=60)
        desktop_client._api_v1_last_heartbeat_at[base_url] -= 25.0

        renewed_no_work = desktop_client.poll_api_v1_encrypted_work()
        assert renewed_no_work["message"] == "No requests available"
        assert server_key in relay.known_servers
        assert requests.get(f"{base_url}/api/v1/relay/servers/next", timeout=2).status_code == 200

        monkeypatch.setattr(
            routes,
            "get_api_v1_compute_provider_for_mode",
            lambda **_kwargs: compute_provider.DistributedApiV1ComputeProvider(
                base_url=base_url,
                timeout_seconds=5,
            ),
        )
        browser_crypto = EncryptionManager()
        user_text = "ping after idle no-work"
        payload = {
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": browser_crypto.public_key_b64,
            "messages": _encrypt_browser_messages(
                [{"role": "user", "content": user_text}]
            ),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        }
        response_holder = {}

        def _browser_request():
            response_holder["response"] = requests.post(
                f"{base_url}/api/v1/chat/completions", json=payload, timeout=10
            )

        browser_thread = threading.Thread(target=_browser_request, daemon=True)
        browser_thread.start()

        deadline = time.time() + 5
        while time.time() < deadline:
            relay_payload = desktop_client.poll_api_v1_encrypted_work()
            if relay_payload.get("protocol") == "tokenplace_api_v1_relay_e2ee":
                assert desktop_client.process_client_request(relay_payload) is True
                break
            time.sleep(0.02)
        else:  # pragma: no cover - diagnostic failure path
            pytest.fail("desktop did not receive queued API v1 E2EE request after idle renewal")

        browser_thread.join(timeout=5)

    response = response_holder.get("response")
    assert response is not None
    assert response.status_code == 200, response.text
    completion = _decrypt_browser_response(browser_crypto, response.json())
    assert completion["choices"][0]["message"]["content"] == "pong from desktop"


@pytest.mark.parametrize("encrypted", [False, True])
def test_api_v1_image_content_fails_before_relay_queue(encrypted):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]
    if encrypted:
        browser_crypto = EncryptionManager()
        payload = {
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": browser_crypto.public_key_b64,
            "messages": _encrypt_browser_messages(messages),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        }
    else:
        payload = {"model": "llama-3-8b-instruct", "messages": messages}

    with relay.app.test_client() as client:
        response = client.post("/api/v1/chat/completions", json=payload)

    assert 400 <= response.status_code < 500
    assert "image" in response.get_json()["error"]["message"].lower()
    assert relay.client_inference_requests == {}


def _load_compute_node_bridge_module():
    import importlib.util
    import sys
    from pathlib import Path

    module_path = (
        Path(__file__).resolve().parents[2]
        / "desktop-tauri"
        / "src-tauri"
        / "python"
        / "compute_node_bridge.py"
    )
    module_dir = str(module_path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location("compute_node_bridge_for_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_desktop_bridge_summary_distinguishes_cloudflare_pre_app_rejection():
    bridge = _load_compute_node_bridge_module()

    summary = bridge._relay_response_summary(
        {
            "error": "HTTP 403",
            "http_status": 403,
            "relay_error_kind": "cloudflare_pre_app_rejection",
            "relay_http_diagnostic": {
                "headers": {
                    "server": "cloudflare",
                    "cf-ray": "84abcd-SJC",
                }
            },
            "next_ping_in_x_seconds": 15,
        },
        wait_seconds=15,
    )

    assert "kind=cloudflare_pre_app_rejection" in summary
    assert "status=403" in summary
    assert "cf_ray=84abcd-SJC" in summary
    assert "server=cloudflare" in summary


def test_desktop_bridge_summary_distinguishes_relay_json_http_and_timeout_errors():
    bridge = _load_compute_node_bridge_module()

    relay_json_summary = bridge._relay_response_summary(
        {
            "error": "HTTP 401",
            "http_status": 401,
            "relay_error_kind": "relay_json_error",
            "relay_error": "invalid relay registration token",
            "next_ping_in_x_seconds": 15,
        },
        wait_seconds=15,
    )
    http_summary = bridge._relay_response_summary(
        {
            "error": "HTTP 403",
            "http_status": 403,
            "relay_error_kind": "http_status_no_json_body",
            "next_ping_in_x_seconds": 15,
        },
        wait_seconds=15,
    )
    timeout_summary = bridge._relay_response_summary(
        {"error": "Read timed out", "next_ping_in_x_seconds": 15},
        wait_seconds=15,
    )

    assert "kind=relay_json_error" in relay_json_summary
    assert "status=401" in relay_json_summary
    assert "invalid relay registration token" in relay_json_summary
    assert "kind=http_status_no_json_body" in http_summary
    assert "status=403" in http_summary
    assert "kind=request_timeout" in timeout_summary
