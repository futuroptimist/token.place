import pytest
import time
import threading
import base64
import json
from pathlib import Path
from flask import Flask
import sys
import os
from datetime import datetime, timedelta
import relay as relay_module
from utils.networking.relay_client import RelayClient

# Add project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from relay import app

# Import the global dictionaries from relay to inspect/manipulate state if needed
# Be cautious with direct manipulation in tests, prefer using API endpoints
from relay import (
    known_servers,
    client_inference_requests,
    client_pending_request_ids,
    client_terminal_request_ids,
    client_responses,
    streaming_sessions,
    streaming_sessions_by_client,
)

# Generate dummy keys for testing
# (You might want to use the generate_keys function from encrypt.py if needed)
DUMMY_SERVER_PUB_KEY = base64.b64encode(b"server_public_key_123").decode("utf-8")
DUMMY_CLIENT_PUB_KEY = base64.b64encode(b"client_public_key_456").decode("utf-8")


@pytest.fixture
def client():
    """Create a Flask test client fixture."""
    app.config["TESTING"] = True
    previous_legacy_flag = os.environ.get("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES")
    os.environ["TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES"] = "1"
    # Reset state before each test
    known_servers.clear()
    client_inference_requests.clear()
    client_pending_request_ids.clear()
    client_terminal_request_ids.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()

    with app.test_client() as client:
        yield client

    if previous_legacy_flag is None:
        os.environ.pop("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", None)
    else:
        os.environ["TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES"] = previous_legacy_flag
    # Clean up state after test (optional, as fixture resets before)
    known_servers.clear()
    client_inference_requests.clear()
    client_pending_request_ids.clear()
    client_terminal_request_ids.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()


def test_operational_endpoints_are_not_rate_limited_by_public_quota(client):
    """Health, liveness, metrics, and diagnostics stay outside user API quotas."""

    paths = ("/healthz", "/livez", "/metrics", "/relay/diagnostics")

    for path in paths:
        responses = [client.get(path) for _ in range(105)]
        assert [response.status_code for response in responses] == [200] * 105
        assert 429 not in {response.status_code for response in responses}


def test_api_v1_register_and_poll_are_not_rate_limited_by_public_quota(
    client, monkeypatch
):
    """Authenticated compute-provider heartbeats stay outside the public API quota."""

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", "relay-token")
    monkeypatch.setattr(relay_module, "SERVER_REGISTRATION_TOKENS", ["relay-token"])
    payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    headers = {"X-Relay-Server-Token": "relay-token"}

    register_responses = [
        client.post("/api/v1/relay/servers/register", json=payload, headers=headers)
        for _ in range(65)
    ]
    assert {response.status_code for response in register_responses} == {200}

    poll_responses = [
        client.post("/api/v1/relay/servers/poll", json=payload, headers=headers)
        for _ in range(65)
    ]
    assert {response.status_code for response in poll_responses} == {200}


def test_api_v1_client_relay_read_paths_are_not_rate_limited_by_public_quota(client):
    """Client discovery and response polling stay outside the public API quota."""

    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 60,
    }
    client_pending_request_ids[DUMMY_CLIENT_PUB_KEY] = {"request-1": time.time()}

    next_responses = [client.get("/api/v1/relay/servers/next") for _ in range(65)]
    assert {response.status_code for response in next_responses} == {200}

    retrieve_responses = [
        client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "request-1"},
        )
        for _ in range(65)
    ]
    assert {response.status_code for response in retrieve_responses} == {202}


def test_inference_endpoint_removed(client):
    """Ensure deprecated /inference endpoint is unavailable."""
    response = client.post("/inference", json={})
    assert response.status_code == 404


# --- Test /next_server ---


def test_next_server_no_servers(client):
    """Test /next_server when no servers are registered."""
    response = client.get("/next_server")
    assert response.status_code == 503
    data = response.get_json()
    assert "error" in data
    assert data["error"]["message"] == "No servers available"
    assert data["error"]["code"] == 503


def test_api_v1_next_server_no_registered_compute_nodes_message(client):
    """API v1 relay reports no registered compute nodes with a stable error code."""

    response = client.get("/api/v1/relay/servers/next")
    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["code"] == "no_registered_compute_nodes"
    assert (
        data["error"]["message"]
        == "No registered compute nodes are available on this relay."
    )


def test_next_server_one_server(client):
    """Test /next_server when one server is registered."""
    # Simulate server registration (directly modifying state for setup)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }

    response = client.get("/next_server")
    assert response.status_code == 200
    data = response.get_json()
    assert "error" not in data
    assert "server_public_key" in data
    assert data["server_public_key"] == DUMMY_SERVER_PUB_KEY


def test_next_server_evicts_stale_nodes(client):
    """Stale servers should be removed before /next_server selection."""
    stale_time = datetime.now() - timedelta(seconds=120)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": stale_time,
        "last_ping_duration": 10,
    }

    response = client.get("/next_server")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["error"]["message"] == "No servers available"
    assert DUMMY_SERVER_PUB_KEY not in known_servers


# --- Test /sink ---


def test_sink_register_new_server(client):
    """Test server registration via /sink."""
    payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert "next_ping_in_x_seconds" in data
    assert DUMMY_SERVER_PUB_KEY in known_servers
    assert known_servers[DUMMY_SERVER_PUB_KEY]["public_key"] == DUMMY_SERVER_PUB_KEY


def test_sink_update_existing_server(client):
    """Test server ping update via /sink."""
    # Initial registration using datetime
    initial_ping_time = datetime.now() - timedelta(seconds=20)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": initial_ping_time,
        "last_ping_duration": 10,
    }

    time.sleep(0.1)  # Ensure time progresses slightly

    # Send update ping
    payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200

    assert DUMMY_SERVER_PUB_KEY in known_servers
    # Compare datetime objects
    assert known_servers[DUMMY_SERVER_PUB_KEY]["last_ping"] > initial_ping_time


def test_sink_invalid_payload(client):
    """Test /sink with missing public key."""
    response = client.post("/sink", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert data["error"] == "Invalid public key"


def test_sink_drops_api_v1_only_queue(client):
    """Sink should drain stale API v1 plaintext entries without dispatching work."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {"api_v1_request": {"messages": [{"role": "user", "content": "stale"}]}},
        {"api_v1_request": {"messages": [{"role": "user", "content": "stale-2"}]}},
    ]

    response = client.post("/sink", json={"server_public_key": DUMMY_SERVER_PUB_KEY})
    assert response.status_code == 200
    payload = response.get_json()

    assert "next_ping_in_x_seconds" in payload
    assert "chat_history" not in payload
    assert client_inference_requests[DUMMY_SERVER_PUB_KEY] == []


def test_sink_skips_api_v1_and_returns_legacy_batch(client):
    """Sink should skip stale API v1 entries and still dispatch legacy E2EE work."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    legacy_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "legacy-ciphertext",
        "cipherkey": "legacy-cipherkey",
        "iv": "legacy-iv",
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {"api_v1_request": {"messages": [{"role": "user", "content": "stale"}]}},
        legacy_payload,
    ]

    response = client.post(
        "/sink",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY, "max_batch_size": 2},
    )
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["client_public_key"] == legacy_payload["client_public_key"]
    assert payload["chat_history"] == legacy_payload["chat_history"]
    assert payload["cipherkey"] == legacy_payload["cipherkey"]
    assert payload["iv"] == legacy_payload["iv"]
    assert payload["batch"] == [legacy_payload]
    assert client_inference_requests[DUMMY_SERVER_PUB_KEY] == []


def test_sink_returns_batch_when_requested(client):
    """Servers can opt into batched work retrieval via max_batch_size."""
    sink_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200

    for idx in range(3):
        faucet_payload = {
            "client_public_key": base64.b64encode(f"client_{idx}".encode()).decode(),
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": f"encrypted_payload_{idx}",
            "cipherkey": f"cipher_{idx}",
            "iv": f"iv_{idx}",
        }
        faucet_response = client.post("/faucet", json=faucet_payload)
        assert faucet_response.status_code == 200

    batch_response = client.post(
        "/sink",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY, "max_batch_size": 2},
    )
    assert batch_response.status_code == 200
    batch_data = batch_response.get_json()

    assert "batch" in batch_data
    assert isinstance(batch_data["batch"], list)
    assert len(batch_data["batch"]) == 2

    first_request, second_request = batch_data["batch"]
    assert first_request["chat_history"] == "encrypted_payload_0"
    assert first_request["client_public_key"] == batch_data["client_public_key"]
    assert second_request["chat_history"] == "encrypted_payload_1"

    remaining_queue = client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])
    assert len(remaining_queue) == 1
    assert remaining_queue[0]["chat_history"] == "encrypted_payload_2"


def test_two_servers_receive_only_addressed_work(client):
    """Queued work should remain isolated by server public key."""
    server_one = base64.b64encode(b"server_public_key_1").decode("utf-8")
    server_two = base64.b64encode(b"server_public_key_2").decode("utf-8")

    assert (
        client.post("/sink", json={"server_public_key": server_one}).status_code == 200
    )
    assert (
        client.post("/sink", json={"server_public_key": server_two}).status_code == 200
    )

    first_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": server_one,
        "chat_history": "work-for-server-one",
        "cipherkey": "cipher-one",
        "iv": "iv-one",
    }
    second_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": server_two,
        "chat_history": "work-for-server-two",
        "cipherkey": "cipher-two",
        "iv": "iv-two",
    }
    assert client.post("/faucet", json=first_payload).status_code == 200
    assert client.post("/faucet", json=second_payload).status_code == 200

    server_two_work = client.post("/sink", json={"server_public_key": server_two})
    assert server_two_work.status_code == 200
    assert server_two_work.get_json()["chat_history"] == "work-for-server-two"

    server_one_work = client.post("/sink", json={"server_public_key": server_one})
    assert server_one_work.status_code == 200
    assert server_one_work.get_json()["chat_history"] == "work-for-server-one"


def test_relay_api_v1_fails_closed(client):
    response = client.post(
        "/relay/api/v1/chat/completions",
        data="[",
        content_type="application/json",
    )

    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["type"] == "service_unavailable_error"
    assert data["error"]["code"] == "distributed_api_v1_relay_disabled"


def test_relay_api_v1_source_fails_closed(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }

    response = client.post(
        "/relay/api/v1/source",
        json={
            "request_id": "req-1",
            "message": {"role": "assistant", "content": "hello"},
        },
    )

    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["type"] == "service_unavailable_error"
    assert data["error"]["code"] == "distributed_api_v1_relay_disabled"


class _RelayClientApiV1CryptoStub:
    def __init__(self, decrypted_payload):
        self.decrypted_payload = decrypted_payload
        self.last_encrypted_payload = None

    def decrypt_message(self, _request_data):
        return self.decrypted_payload

    def encrypt_message(self, payload, _client_pub_key):
        self.last_encrypted_payload = payload
        return {
            "chat_history": "ciphertext-only",
            "cipherkey": "cipher-key",
            "iv": "cipher-iv",
        }


def _build_relay_client_for_api_v1_tests(crypto_stub, model_manager=None):
    return RelayClient(
        base_url="https://relay.example",
        port=None,
        crypto_manager=crypto_stub,
        model_manager=model_manager or object(),
        include_configured_servers=False,
    )


def test_relay_client_api_v1_envelope_uses_model_and_posts_ciphertext_only(monkeypatch):
    captured = {}
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-1",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {"temperature": 0.2, "max_tokens": 42},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)

    class _FakeLlmInstance:
        @staticmethod
        def create_chat_completion(messages, **options):
            captured["messages"] = messages
            captured["options"] = options
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "bonjour"}},
                ]
            }

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(model):
            captured["model"] = model
            return model == "llama-3-8b-instruct:alignment"

        @staticmethod
        def get_llm_instance():
            return _FakeLlmInstance()

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(url, json, timeout, **_kwargs):
        assert url == "https://relay.example/api/v1/relay/responses"
        assert timeout == relay_client._request_timeout
        assert "chat_history" in json and "cipherkey" in json and "iv" in json
        assert json["request_id"] == "req-1"
        assert json["protocol"] == "tokenplace_api_v1_relay_e2ee"
        assert json["version"] == 1
        assert "messages" not in json
        assert "prompt" not in json
        assert "model" not in json
        assert "api_v1_response" not in json

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-1"
    assert encrypted_payload["api_v1_response"]["message"]["content"] == "bonjour"
    assert captured["model"] == "llama-3-8b-instruct:alignment"


def test_relay_client_rejects_invalid_client_public_key_encoding(monkeypatch):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-invalid-client-key",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    post_calls = {"count": 0}

    def fake_post(*_args, **_kwargs):
        post_calls["count"] += 1

        class _Response:
            status_code = 200

        return _Response()

    def fake_generate_response(_model, messages, **_options):
        return messages + [{"role": "assistant", "content": "bonjour"}]

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": "%%%not-base64%%%",
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is False
    assert post_calls["count"] == 0


def test_relay_client_api_v1_normalizes_client_public_key_binding(monkeypatch):
    normalized_client_key = DUMMY_CLIENT_PUB_KEY
    request_client_key = f"  {normalized_client_key}\n"
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": normalized_client_key,
        "request_id": "req-normalized-client-key",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(_model, messages, **_options):
        return messages + [{"role": "assistant", "content": "bonjour"}]

    def fake_post(_url, json, timeout, **_kwargs):
        assert timeout == relay_client._request_timeout
        assert json["client_public_key"] == normalized_client_key

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": request_client_key,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True


def test_relay_client_api_v1_posts_encrypted_model_unsupported_error(monkeypatch):
    from api.v1.models import ModelError

    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-unsupported",
        "api_v1_request": {
            "model": "unknown-model-id",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(*_args, **_kwargs):
        raise ModelError(
            "Model 'unknown-model-id' not found",
            status_code=404,
            error_type="model_not_found",
        )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-unsupported"
    assert (
        encrypted_payload["api_v1_response"]["error"]["code"]
        == "compute_node_model_unsupported"
    )


def test_relay_client_api_v1_falls_back_to_runtime_model_when_catalog_model_unavailable(
    monkeypatch,
):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-runtime-fallback",
        "api_v1_request": {
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {"temperature": 0.2},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)

    class _FakeLlmInstance:
        @staticmethod
        def create_chat_completion(messages, **_options):
            return {"choices": [{"message": {"role": "assistant", "content": "Paris"}}]}

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(model):
            return model == "llama-3-8b-instruct"

        @staticmethod
        def get_llm_instance():
            return _FakeLlmInstance()

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-runtime-fallback"
    assert encrypted_payload["api_v1_response"]["message"]["content"] == "Paris"


def test_relay_client_api_v1_posts_encrypted_internal_error_for_unexpected_exception(
    monkeypatch,
):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-internal",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {"temperature": 0.2},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(_model):
            return True

        @staticmethod
        def get_llm_instance():
            raise RuntimeError("backend crashed")

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-internal"
    assert (
        encrypted_payload["api_v1_response"]["error"]["code"]
        == "compute_node_internal_error"
    )


def test_relay_client_api_v1_source_post_failure_returns_false(monkeypatch):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-source-post-failure",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(_model, messages, **_options):
        return messages + [{"role": "assistant", "content": "bonjour"}]

    def raising_post(*_args, **_kwargs):
        raise RuntimeError("relay /source unavailable")

    monkeypatch.setattr("utils.networking.relay_client.requests.post", raising_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is False


@pytest.mark.parametrize(
    ("generated_response",),
    [
        ([],),
        ([{"role": "assistant", "content": "ok"}, "bad-last-message"],),
    ],
)
def test_relay_client_api_v1_posts_encrypted_internal_error_for_invalid_inference_output(
    monkeypatch,
    generated_response,
):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-invalid-inference-output",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)

    class _FakeLlmInstance:
        @staticmethod
        def create_chat_completion(*, messages, **_options):
            return generated_response

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(_model):
            return True

        @staticmethod
        def get_llm_instance():
            return _FakeLlmInstance()

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-invalid-inference-output"
    assert (
        encrypted_payload["api_v1_response"]["error"]["code"]
        == "compute_node_invalid_model_output"
    )


def test_relay_client_submit_api_v1_error_response_posts_ciphertext_only(monkeypatch):
    crypto_stub = _RelayClientApiV1CryptoStub({})
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)
    captured = {}

    def fake_post(url, json=None, timeout=None, **_kwargs):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-runtime-not-ready",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }

    assert (
        relay_client.submit_api_v1_error_response(
            request_data,
            code="compute_node_runtime_not_ready",
            message="API v1 model runtime is not ready",
        )
        is True
    )

    assert captured["url"] == "https://relay.example/api/v1/relay/responses"
    assert captured["timeout"] == relay_client._request_timeout
    posted = captured["payload"]
    assert posted["request_id"] == "req-runtime-not-ready"
    assert posted["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert posted["version"] == 1
    assert "chat_history" in posted and "cipherkey" in posted and "iv" in posted
    assert "api_v1_response" not in posted
    assert "API v1 model runtime is not ready" not in json.dumps(posted)

    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-runtime-not-ready"
    assert encrypted_payload["api_v1_response"]["error"] == {
        "code": "compute_node_runtime_not_ready",
        "message": "API v1 model runtime is not ready",
    }


# --- Test /faucet ---


def test_faucet_submit_request(client):
    """Test submitting a valid inference request via /faucet."""
    # Register server first
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": time.time(),
        "last_ping_duration": 10,
    }

    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector",
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["message"] == "Request received"

    # Check internal state
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1
    queued_req = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert queued_req["client_public_key"] == DUMMY_CLIENT_PUB_KEY
    assert queued_req["chat_history"] == "encrypted_chat_history_data"


def test_faucet_invalid_payload(client):
    """Test /faucet with missing fields."""
    # Register server
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": time.time(),
        "last_ping_duration": 10,
    }

    payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}  # Missing other fields
    response = client.post("/faucet", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert data["error"]["message"] == "Invalid request data"


def test_faucet_unknown_server(client):
    """Test /faucet request to an unknown server."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": "unknown_server_key",  # This server is not registered
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector",
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 404
    data = response.get_json()
    assert "error" in data
    assert data["error"] == {
        "message": "Server with the specified public key not found",
        "code": 404,
    }


def test_relay_diagnostics_distinguishes_configured_and_live_nodes(client, monkeypatch):
    """Diagnostics should expose configured URLs and live compute registrations."""
    monkeypatch.delenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setitem(
        app.config,
        "relay_configured_servers",
        [
            "https://configured-one.example.com:8000",
            "https://configured-two.example.com:8000",
        ],
    )
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {
            "chat_history": "pending",
            "client_public_key": "c",
            "cipherkey": "k",
            "iv": "i",
        }
    ]

    response = client.get("/relay/diagnostics")
    assert response.status_code == 200
    payload = response.get_json()

    assert (
        payload["configured_upstream_servers"] == app.config["relay_configured_servers"]
    )
    assert payload["legacy_configured_upstream_servers"] == []
    assert payload["upstream_health_required"] is False
    assert payload["relay_only"] is False
    assert payload["total_registered_compute_nodes"] == 1
    assert (
        payload["registered_compute_nodes"][0]["server_public_key"]
        == DUMMY_SERVER_PUB_KEY
    )
    assert payload["registered_compute_nodes"][0]["queue_depth"] == 1


def test_relay_diagnostics_reports_explicit_upstream_env(client, monkeypatch):
    """Diagnostics should retain configured_upstream_servers for explicit upstream env config."""
    configured_servers = ["https://configured-one.example.com:8000"]
    monkeypatch.setitem(app.config, "relay_configured_servers", configured_servers)
    monkeypatch.setenv("TOKENPLACE_RELAY_UPSTREAM_URL", "https://gpu.example.com:5015")

    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["configured_upstream_servers"] == configured_servers
    assert payload["legacy_configured_upstream_servers"] == []
    assert payload["relay_only"] is False
    assert payload["upstream_health_required"] is False


def test_healthz_reports_configured_upstreams_and_live_queue_depth(client, monkeypatch):
    """Healthz should separate configured upstream URLs from live registered nodes."""
    monkeypatch.setitem(app.config, "gpu_host", None)
    configured_servers = [
        "https://configured-one.example.com:8000",
        "https://configured-two.example.com:8000",
    ]
    app.config["relay_configured_servers"] = configured_servers
    live_server_key = base64.b64encode(b"live_server_key").decode("utf-8")
    known_servers[live_server_key] = {
        "public_key": live_server_key,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[live_server_key] = [
        {
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "pending-work",
            "cipherkey": "cipher",
            "iv": "iv",
        }
    ]

    monkeypatch.setenv("TOKEN_PLACE_RELAY_UPSTREAMS", ",".join(configured_servers))
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["configuredUpstreamServers"] == configured_servers
    assert payload["upstreamHealthRequired"] is False
    assert payload["relayOnly"] is False
    assert payload["legacyConfiguredUpstreamServers"] == []
    assert payload["registeredServers"][0]["server_public_key"] == live_server_key
    assert payload["registeredServers"][0]["age_seconds"] >= 0
    assert payload["registeredServers"][0]["queue_depth"] == 1
    assert "https://configured-one.example.com:8000" not in {
        node["server_public_key"] for node in payload["registeredServers"]
    }


def test_healthz_returns_draining_when_shutdown_flag_set(client):
    """healthz should switch to draining status and 503 during shutdown."""
    relay_module.DRAINING.set()
    try:
        response = client.get("/healthz")
    finally:
        relay_module.DRAINING.clear()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "0"
    payload = response.get_json()
    assert payload["status"] == "draining"
    assert payload["details"]["shutdown"] is True


def test_livez_remains_alive_when_draining(client):
    """livez should stay green so orchestrators can distinguish readiness from liveness."""
    from relay import DRAINING

    DRAINING.set()
    try:
        response = client.get("/livez")
    finally:
        DRAINING.clear()

    assert response.status_code == 200
    assert response.get_json()["status"] == "alive"


def test_healthz_default_allows_unresolvable_upstream_host(client, monkeypatch):
    """healthz should stay ready by default for relay-only deployments."""
    monkeypatch.setitem(app.config, "gpu_host", "definitely-not-resolvable.invalid")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])
    monkeypatch.setattr(relay_module, "_can_resolve_gpu_host", lambda _host: False)
    monkeypatch.delenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["gpuHost"] == "definitely-not-resolvable.invalid"
    assert payload["upstreamHealthRequired"] is False
    assert payload["relayOnly"] is True
    assert payload["legacyConfiguredUpstreamServers"] == ["https://token.place"]
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert payload.get("details", {}).get("gpuHostResolution") != "failed"


def test_healthz_requires_upstream_health_when_env_enabled(client, monkeypatch):
    """healthz should degrade when upstream resolution is required and fails."""
    monkeypatch.setitem(app.config, "gpu_host", "definitely-not-resolvable.invalid")
    monkeypatch.setattr(relay_module, "_can_resolve_gpu_host", lambda _host: False)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "1")

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["upstreamHealthRequired"] is True
    assert payload["relayOnly"] is False
    assert payload["details"]["gpuHostResolution"] == "failed"


def test_healthz_staging_relay_only_does_not_imply_prod_upstream(client, monkeypatch):
    """Staging relay-only health should be OK with zero registered external nodes."""
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://staging.token.place")
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setitem(app.config, "public_base_url", "https://staging.token.place")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["publicBaseUrl"] == "https://staging.token.place"
    assert payload["knownServers"] == 0
    assert payload["relayOnly"] is True
    assert payload["upstreamHealthRequired"] is False
    assert payload["legacyConfiguredUpstreamServers"] == ["https://token.place"]
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert payload.get("details", {}).get("knownServers") == "empty"


def test_healthz_malformed_upstreams_env_keeps_default_as_legacy(client, monkeypatch):
    """Malformed upstream list env should not mark fallback default as explicit config."""
    monkeypatch.setenv("TOKEN_PLACE_RELAY_UPSTREAMS", '{"url":"https://ignored"}')
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relayOnly"] is True
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert payload["legacyConfiguredUpstreamServers"] == ["https://token.place"]


def test_healthz_custom_configured_servers_are_not_reported_as_legacy(
    client, monkeypatch
):
    """Custom configured server pools should be treated as explicit/non-legacy."""
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.setitem(
        app.config, "relay_configured_servers", ["https://custom.upstream.example"]
    )

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relayOnly"] is False
    assert payload["configuredUpstreamServers"] == ["https://custom.upstream.example"]
    assert payload["legacyConfiguredUpstreamServers"] == []


def test_relay_entrypoint_defaults_to_one_worker_and_multiple_threads():
    """Container entrypoint should keep one worker and default to thread concurrency."""
    entrypoint_path = (
        Path(__file__).resolve().parents[1] / "docker" / "relay" / "entrypoint.sh"
    )
    with entrypoint_path.open(encoding="utf-8") as file:
        content = file.read()
    assert 'WORKERS="${RELAY_WORKERS:-1}"' in content
    assert 'THREADS="${RELAY_THREADS:-4}"' in content


# --- Test /source ---


def test_source_submit_response(client):
    """Test server submitting a response via /source."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_encrypted_response_history",
        "cipherkey": "server_encrypted_aes_key",
        "iv": "server_iv",
    }
    response = client.post("/source", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["message"] == "Response received and queued for client"

    # Check internal state
    assert DUMMY_CLIENT_PUB_KEY in client_responses
    queued_resp = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert queued_resp["chat_history"] == "server_encrypted_response_history"


def test_source_invalid_payload(client):
    """Test /source with missing fields."""
    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}  # Missing other fields
    response = client.post("/source", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert data["error"] == "Invalid request data"


# --- Test /retrieve ---


def test_retrieve_get_response(client):
    """Test client retrieving a queued response via /retrieve."""
    # Queue a response first (directly modify state for setup)
    client_responses[DUMMY_CLIENT_PUB_KEY] = {
        "chat_history": "server_encrypted_response_history",
        "cipherkey": "server_encrypted_aes_key",
        "iv": "server_iv",
    }

    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200
    data = response.get_json()

    assert data["chat_history"] == "server_encrypted_response_history"
    assert data["cipherkey"] == "server_encrypted_aes_key"
    assert data["iv"] == "server_iv"

    # Check state - response should be removed after retrieval
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_retrieve_no_response_available(client):
    """Test /retrieve when no response is queued for the client."""
    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200  # Endpoint works, just no data
    data = response.get_json()
    assert "error" in data
    assert data["error"] == "No response available for the given public key"


def test_retrieve_invalid_payload(client):
    """Test /retrieve with missing client public key."""
    response = client.post("/retrieve", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert data["error"] == "Invalid request data"


# --- Integration Test ---


def test_full_relay_flow(client):
    """Test the full flow: register, faucet, sink poll, source, retrieve."""
    # 1. Server registers via /sink
    sink_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in known_servers

    # 2. Client requests inference via /faucet
    faucet_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "client_request_data",
        "cipherkey": "client_key_data",
        "iv": "client_iv_data",
    }
    response = client.post("/faucet", json=faucet_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    # 3. Server polls /sink and gets the request
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    sink_data = response.get_json()
    assert sink_data["client_public_key"] == DUMMY_CLIENT_PUB_KEY
    assert sink_data["chat_history"] == "client_request_data"
    assert sink_data["cipherkey"] == "client_key_data"
    assert sink_data["iv"] == "client_iv_data"
    # Request should be removed from queue
    assert not client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])

    # 4. Server processes and submits response via /source
    source_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_response_data",
        "cipherkey": "server_key_data",
        "iv": "server_iv_data",
    }
    response = client.post("/source", json=source_payload)
    assert response.status_code == 200
    assert DUMMY_CLIENT_PUB_KEY in client_responses

    # 5. Client retrieves response via /retrieve
    retrieve_payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=retrieve_payload)
    assert response.status_code == 200
    retrieve_data = response.get_json()
    assert retrieve_data["chat_history"] == "server_response_data"
    assert retrieve_data["cipherkey"] == "server_key_data"
    assert retrieve_data["iv"] == "server_iv_data"


def test_streaming_state_lifecycle(client):
    """Streaming sessions should store and release chunk state per client."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": time.time(),
        "last_ping_duration": 10,
    }

    faucet_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector",
        "stream": True,
    }
    faucet_response = client.post("/faucet", json=faucet_payload)
    assert faucet_response.status_code == 200

    sink_response = client.post(
        "/sink", json={"server_public_key": DUMMY_SERVER_PUB_KEY}
    )
    assert sink_response.status_code == 200
    sink_data = sink_response.get_json()
    assert sink_data.get("stream") is True
    session_id = sink_data.get("stream_session_id")
    assert session_id
    assert session_id in streaming_sessions
    session_state = streaming_sessions[session_id]
    assert session_state["client_public_key"] == DUMMY_CLIENT_PUB_KEY
    assert session_state["status"] == "open"

    chunk_payload = {
        "session_id": session_id,
        "chunk": {"content": "hello"},
    }
    chunk_response = client.post("/stream/source", json=chunk_payload)
    assert chunk_response.status_code == 200

    retrieve_response = client.post(
        "/stream/retrieve", json={"client_public_key": DUMMY_CLIENT_PUB_KEY}
    )
    assert retrieve_response.status_code == 200
    retrieved = retrieve_response.get_json()
    assert retrieved["stream"] is True
    assert retrieved["session_id"] == session_id
    assert retrieved["chunks"] == [{"content": "hello"}]
    assert "final" not in retrieved

    final_payload = {
        "session_id": session_id,
        "chunk": {"content": "goodbye"},
        "final": True,
    }
    final_response = client.post("/stream/source", json=final_payload)
    assert final_response.status_code == 200

    final_retrieve = client.post(
        "/stream/retrieve", json={"client_public_key": DUMMY_CLIENT_PUB_KEY}
    )
    assert final_retrieve.status_code == 200
    final_data = final_retrieve.get_json()
    assert final_data["stream"] is True
    assert final_data["session_id"] == session_id
    assert final_data["chunks"] == [{"content": "goodbye"}]
    assert final_data["final"] is True

    assert session_id not in streaming_sessions
    assert DUMMY_CLIENT_PUB_KEY not in streaming_sessions_by_client
    # Response should be removed from queue
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_relay_route_contract_e2ee_flow(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    register = client.post("/api/v1/relay/servers/register", json=server_payload)
    assert register.status_code == 200

    request_payload = {
        "request_id": "req-123",
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "ciphertext-request",
        "cipherkey": "cipherkey-request",
        "iv": "iv-request",
    }
    queued = client.post("/api/v1/relay/requests", json=request_payload)
    assert queued.status_code == 200

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    polled_payload = poll.get_json()
    assert polled_payload["chat_history"] == "ciphertext-request"
    assert polled_payload["cipherkey"] == "cipherkey-request"
    assert polled_payload["iv"] == "iv-request"
    assert polled_payload["client_public_key"] == DUMMY_CLIENT_PUB_KEY
    assert polled_payload["request_id"] == "req-123"
    assert polled_payload["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert polled_payload["version"] == 1

    response_payload = {
        "request_id": "req-123",
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "ciphertext-response",
        "cipherkey": "cipherkey-response",
        "iv": "iv-response",
    }
    source = client.post("/api/v1/relay/responses", json=response_payload)
    assert source.status_code == 200

    retrieved = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY},
    )
    assert retrieved.status_code == 200
    retrieved_payload = retrieved.get_json()
    assert retrieved_payload["chat_history"] == "ciphertext-response"
    assert retrieved_payload["cipherkey"] == "cipherkey-response"
    assert retrieved_payload["iv"] == "iv-response"
    assert retrieved_payload["request_id"] == "req-123"
    assert retrieved_payload["protocol"] == "tokenplace_api_v1_relay_e2ee"


def test_queue_client_response_serializes_concurrent_updates(monkeypatch):
    class SlowSnapshotDict(dict):
        def __init__(self):
            super().__init__()
            self._active_lock = threading.Lock()
            self._active_gets = 0
            self.concurrent_get_seen = False

        def get(self, key, default=None):
            value = super().get(key, default)
            with self._active_lock:
                self._active_gets += 1
                if self._active_gets > 1:
                    self.concurrent_get_seen = True
            time.sleep(0.05)
            with self._active_lock:
                self._active_gets -= 1
            return value

    response_queue = SlowSnapshotDict()
    monkeypatch.setattr(relay_module, "client_responses", response_queue)
    envelopes = [
        {
            "request_id": "req-1",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-response-1",
            "cipherkey": "cipherkey-response-1",
            "iv": "iv-response-1",
        },
        {
            "request_id": "req-2",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-response-2",
            "cipherkey": "cipherkey-response-2",
            "iv": "iv-response-2",
        },
    ]
    threads = [
        threading.Thread(
            target=relay_module._queue_client_response,
            args=(DUMMY_CLIENT_PUB_KEY, envelope),
        )
        for envelope in envelopes
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert response_queue.concurrent_get_seen is False
    queued = response_queue[DUMMY_CLIENT_PUB_KEY]
    assert isinstance(queued, list)
    assert sorted(item["request_id"] for item in queued) == ["req-1", "req-2"]


def test_api_v1_response_retrieve_matches_request_id_without_dropping_other_responses(
    client,
):
    response_one = {
        "request_id": "req-1",
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "ciphertext-response-1",
        "cipherkey": "cipherkey-response-1",
        "iv": "iv-response-1",
    }
    response_two = {
        "request_id": "req-2",
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "ciphertext-response-2",
        "cipherkey": "cipherkey-response-2",
        "iv": "iv-response-2",
    }

    assert client.post("/api/v1/relay/responses", json=response_one).status_code == 200
    assert client.post("/api/v1/relay/responses", json=response_two).status_code == 200

    missing = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-missing"},
    )
    assert missing.status_code == 404
    assert len(client_responses[DUMMY_CLIENT_PUB_KEY]) == 2

    retrieved_two = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-2"},
    )
    assert retrieved_two.status_code == 200
    assert retrieved_two.get_json()["request_id"] == "req-2"
    assert client_responses[DUMMY_CLIENT_PUB_KEY]["request_id"] == "req-1"

    retrieved_one = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-1"},
    )
    assert retrieved_one.status_code == 200
    assert retrieved_one.get_json()["request_id"] == "req-1"
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_response_retrieve_returns_pending_for_known_request_id(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-pending",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
        },
    )
    assert queued.status_code == 200

    pending = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-pending"},
    )
    assert pending.status_code == 202
    assert pending.get_json() == {"status": "pending"}


def test_api_v1_response_retrieve_stays_pending_for_long_running_valid_interval(
    client, monkeypatch
):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-long-running",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
        },
    )
    assert queued.status_code == 200

    queued_at = client_pending_request_ids[DUMMY_CLIENT_PUB_KEY]["req-long-running"][
        "queued_at"
    ]
    monkeypatch.setattr(relay_module, "PENDING_REQUEST_TTL_SECONDS", 300.0)
    monkeypatch.setattr(relay_module.time, "time", lambda: queued_at + 299.0)

    pending = client.post(
        "/api/v1/relay/responses/retrieve",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-long-running",
        },
    )
    assert pending.status_code == 202
    assert pending.get_json() == {"status": "pending"}


def test_api_v1_response_retrieve_returns_404_after_unregistered_server_drops_queue(
    client,
):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-abandoned",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
        },
    )
    assert queued.status_code == 200

    pending = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-abandoned"},
    )
    assert pending.status_code == 202
    assert pending.get_json() == {"status": "pending"}

    unregistered = client.post(
        "/unregister", json={"server_public_key": DUMMY_SERVER_PUB_KEY}
    )
    assert unregistered.status_code == 200

    unknown = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-abandoned"},
    )
    assert unknown.status_code == 404


def test_api_v1_cancel_removes_queued_request_and_retrieve_returns_gone(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-cancelled",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "cancel_token": "cancel-proof-req-cancelled",
        },
    )
    assert queued.status_code == 200
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    cancelled = client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-cancelled",
            "status": "expired",
            "reason": "provider_timeout",
            "cancel_token": "cancel-proof-req-cancelled",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["removed_from_queue"] == 1
    assert client_inference_requests.get(DUMMY_SERVER_PUB_KEY, []) == []

    diagnostics = client.get("/relay/diagnostics").get_json()
    server = next(
        item
        for item in diagnostics["registered_compute_nodes"]
        if item["server_public_key"] == DUMMY_SERVER_PUB_KEY
    )
    assert server["queue_depth"] == 0

    retrieve = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-cancelled"},
    )
    assert retrieve.status_code == 410
    assert retrieve.get_json()["error"]["code"] == "expired"

    polled = client.post(
        "/api/v1/relay/servers/poll", json={"server_public_key": DUMMY_SERVER_PUB_KEY}
    )
    assert polled.status_code == 200
    assert polled.get_json()["message"] == "No requests available"


def test_api_v1_pending_request_ttl_expires_and_removes_queue_depth(
    client, monkeypatch
):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-expired",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
        },
    )
    assert queued.status_code == 200
    queued_at = client_pending_request_ids[DUMMY_CLIENT_PUB_KEY]["req-expired"][
        "queued_at"
    ]
    monkeypatch.setattr(relay_module, "PENDING_REQUEST_TTL_SECONDS", 1.0)
    monkeypatch.setattr(relay_module.time, "time", lambda: queued_at + 2.0)

    retrieve = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-expired"},
    )
    assert retrieve.status_code == 410
    assert retrieve.get_json()["error"]["code"] == "expired"
    assert client_inference_requests.get(DUMMY_SERVER_PUB_KEY, []) == []


def test_api_v1_cancel_requires_matching_cancel_token(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-auth-cancel",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "cancel_token": "expected-cancel-proof",
        },
    )
    assert queued.status_code == 200

    unauthorized = client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-auth-cancel",
            "status": "expired",
            "reason": "provider_timeout",
            "cancel_token": "wrong-proof",
        },
    )
    assert unauthorized.status_code == 401
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1
    assert (
        relay_module._get_terminal_request(DUMMY_CLIENT_PUB_KEY, "req-auth-cancel")
        is None
    )


def test_api_v1_cancel_sanitizes_invalid_status_and_reason(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    assert (
        client.post(
            "/api/v1/relay/requests",
            json={
                "request_id": "req-sanitize-cancel",
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": "ciphertext-request",
                "cipherkey": "cipherkey-request",
                "iv": "iv-request",
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "cancel_token": "sanitize-proof",
            },
        ).status_code
        == 200
    )

    cancelled = client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-sanitize-cancel",
            "status": "evil-status",
            "reason": "contains unsanitized caller text",
            "cancel_token": "sanitize-proof",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"

    retrieve = client.post(
        "/api/v1/relay/responses/retrieve",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-sanitize-cancel",
        },
    )
    assert retrieve.status_code == 410
    error = retrieve.get_json()["error"]
    assert error["code"] == "cancelled"
    assert error["reason"] == "cancelled"


def test_api_v1_cancelled_queued_response_retrieve_returns_gone(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/relay/requests",
            json={
                "request_id": "req-response-then-cancel",
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": "ciphertext-request",
                "cipherkey": "cipherkey-request",
                "iv": "iv-request",
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "cancel_token": "response-then-cancel-proof",
            },
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/relay/servers/poll", json=server_payload).status_code
        == 200
    )
    response_payload = {
        "request_id": "req-response-then-cancel",
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "ciphertext-response",
        "cipherkey": "cipherkey-response",
        "iv": "iv-response",
    }
    assert (
        client.post("/api/v1/relay/responses", json=response_payload).status_code == 200
    )

    cancelled = client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-response-then-cancel",
            "status": "expired",
            "reason": "provider_timeout",
            "cancel_token": "response-then-cancel-proof",
        },
    )
    assert cancelled.status_code == 200

    retrieve = client.post(
        "/api/v1/relay/responses/retrieve",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-response-then-cancel",
        },
    )
    assert retrieve.status_code == 410
    assert retrieve.get_json()["error"]["code"] == "expired"
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_response_after_in_flight_cancel_is_rejected_and_queue_depth_zero(
    client,
):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/relay/requests",
            json={
                "request_id": "req-dispatched-cancelled",
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": "ciphertext-request",
                "cipherkey": "cipherkey-request",
                "iv": "iv-request",
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "cancel_token": "dispatched-proof",
            },
        ).status_code
        == 200
    )
    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    assert poll.get_json()["request_id"] == "req-dispatched-cancelled"

    cancelled = client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-dispatched-cancelled",
            "status": "cancelled",
            "reason": "requester_cancelled",
            "cancel_token": "dispatched-proof",
        },
    )
    assert cancelled.status_code == 200

    late_response = client.post(
        "/api/v1/relay/responses",
        json={
            "request_id": "req-dispatched-cancelled",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-response",
            "cipherkey": "cipherkey-response",
            "iv": "iv-response",
        },
    )
    assert late_response.status_code == 410
    assert (
        client.get("/relay/diagnostics").get_json()["registered_compute_nodes"][0][
            "queue_depth"
        ]
        == 0
    )
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_cancel_only_clears_matching_client_in_flight_entry(client):
    other_client_key = base64.b64encode(b"other_client_public_key_789").decode("utf-8")
    other_server_key = base64.b64encode(b"other_server_public_key_789").decode("utf-8")
    for server_key in (DUMMY_SERVER_PUB_KEY, other_server_key):
        assert (
            client.post(
                "/api/v1/relay/servers/register", json={"server_public_key": server_key}
            ).status_code
            == 200
        )
    known_servers[DUMMY_SERVER_PUB_KEY]["api_v1_in_flight_requests"] = {
        "shared-request-id": {
            "expires_at": time.monotonic() + 30,
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "cancel_token": "matching-proof",
        }
    }
    known_servers[other_server_key]["api_v1_in_flight_requests"] = {
        "shared-request-id": {
            "expires_at": time.monotonic() + 30,
            "client_public_key": other_client_key,
            "cancel_token": "other-proof",
        }
    }
    relay_module._mark_request_pending(
        DUMMY_CLIENT_PUB_KEY,
        "shared-request-id",
        cancel_token="matching-proof",
    )

    cancelled = client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "shared-request-id",
            "status": "cancelled",
            "reason": "requester_cancelled",
            "cancel_token": "matching-proof",
        },
    )
    assert cancelled.status_code == 200
    assert "api_v1_in_flight_requests" not in known_servers[DUMMY_SERVER_PUB_KEY]
    assert (
        "shared-request-id"
        in known_servers[other_server_key]["api_v1_in_flight_requests"]
    )
    assert (
        relay_module._get_terminal_request(DUMMY_CLIENT_PUB_KEY, "shared-request-id")
        is not None
    )
    assert (
        relay_module._get_terminal_request(other_client_key, "shared-request-id")
        is None
    )


def test_api_v1_pending_ttl_cleanup_runs_without_retrieve(client, monkeypatch):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-expire-without-retrieve",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "cancel_token": "ttl-proof",
        },
    )
    assert queued.status_code == 200
    queued_at = client_pending_request_ids[DUMMY_CLIENT_PUB_KEY][
        "req-expire-without-retrieve"
    ]["queued_at"]
    monkeypatch.setattr(relay_module, "PENDING_REQUEST_TTL_SECONDS", 1.0)
    monkeypatch.setattr(relay_module.time, "time", lambda: queued_at + 2.0)

    diagnostics = client.get("/relay/diagnostics")
    assert diagnostics.status_code == 200
    assert client_inference_requests.get(DUMMY_SERVER_PUB_KEY, []) == []
    assert (
        relay_module._get_terminal_request(
            DUMMY_CLIENT_PUB_KEY, "req-expire-without-retrieve"
        )
        is not None
    )


def test_api_v1_terminal_records_are_pruned_without_retrieve(client, monkeypatch):
    base_time = time.time()
    monkeypatch.setattr(relay_module, "TERMINAL_REQUEST_TTL_SECONDS", 1.0)
    monkeypatch.setattr(relay_module.time, "time", lambda: base_time)
    relay_module._mark_request_terminal(DUMMY_CLIENT_PUB_KEY, "req-terminal-pruned")
    assert (
        relay_module._get_terminal_request(DUMMY_CLIENT_PUB_KEY, "req-terminal-pruned")
        is not None
    )

    monkeypatch.setattr(relay_module.time, "time", lambda: base_time + 2.0)
    client.get("/relay/diagnostics")
    assert relay_module.client_terminal_request_ids == {}


def test_api_v1_sequential_single_node_queue_depth_returns_to_zero(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )

    for index in range(3):
        request_id = f"req-turn-{index}"
        queued = client.post(
            "/api/v1/relay/requests",
            json={
                "request_id": request_id,
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": f"ciphertext-request-{index}",
                "cipherkey": f"cipherkey-request-{index}",
                "iv": f"iv-request-{index}",
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
            },
        )
        assert queued.status_code == 200

        polled = client.post(
            "/api/v1/relay/servers/poll",
            json={"server_public_key": DUMMY_SERVER_PUB_KEY},
        )
        assert polled.status_code == 200
        assert polled.get_json()["request_id"] == request_id

        submitted = client.post(
            "/api/v1/relay/responses",
            json={
                "request_id": request_id,
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "chat_history": f"ciphertext-response-{index}",
                "cipherkey": f"cipherkey-response-{index}",
                "iv": f"iv-response-{index}",
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
            },
        )
        assert submitted.status_code == 200

        retrieved = client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": request_id},
        )
        assert retrieved.status_code == 200
        diagnostics = client.get("/relay/diagnostics").get_json()
        server = next(
            item
            for item in diagnostics["registered_compute_nodes"]
            if item["server_public_key"] == DUMMY_SERVER_PUB_KEY
        )
        assert server["queue_depth"] == 0


def test_api_v1_response_retrieve_request_id_mismatch_keeps_single_response(client):
    response_payload = {
        "request_id": "req-1",
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "ciphertext-response",
        "cipherkey": "cipherkey-response",
        "iv": "iv-response",
    }
    assert (
        client.post("/api/v1/relay/responses", json=response_payload).status_code == 200
    )

    mismatch = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-other"},
    )
    assert mismatch.status_code == 404
    assert client_responses[DUMMY_CLIENT_PUB_KEY]["request_id"] == "req-1"

    retrieved = client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "req-1"},
    )
    assert retrieved.status_code == 200
    assert retrieved.get_json()["request_id"] == "req-1"
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_relay_plaintext_messages_are_rejected(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )

    plaintext = "PLAINTEXT_SENTINEL_DO_NOT_STORE"
    payload = {
        "request_id": "req-no-messages",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "ciphertext-only",
        "cipherkey": "cipherkey-only",
        "iv": "iv-only",
        "messages": [{"role": "user", "content": plaintext}],
        "prompt": plaintext,
    }
    response = client.post("/api/v1/relay/requests", json=payload)
    assert response.status_code == 400
    assert (
        "forbidden; send ciphertext envelope only"
        in response.get_json()["error"]["message"]
    )
    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests


def test_api_v1_relay_requests_requires_client_public_key(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )

    response = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-missing-client-key",
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "ciphertext": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {"message": "Missing client public key", "code": 400}
    }


def test_api_v1_register_and_poll_do_not_delegate_to_legacy_sink(client, monkeypatch):
    def _sink_should_not_be_called():
        raise AssertionError(
            "legacy sink() should not be called by API v1 register/poll"
        )

    monkeypatch.setattr("relay.sink", _sink_should_not_be_called)

    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    register = client.post("/api/v1/relay/servers/register", json=server_payload)
    assert register.status_code == 200

    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-no-sink-delegation",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "ciphertext": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )
    assert queued.status_code == 200

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    polled = poll.get_json()
    assert polled["request_id"] == "req-no-sink-delegation"


def test_api_v1_register_advertises_configured_poll_wait(client, monkeypatch):
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "30")
    response = client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["next_ping_in_x_seconds"] == 30
    assert payload["poll_wait_seconds"] == 30.0


def test_api_v1_poll_skips_legacy_queue_items_and_claims_e2ee_only(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    register = client.post("/api/v1/relay/servers/register", json=server_payload)
    assert register.status_code == 200

    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "legacy-plaintext",
            "cipherkey": "legacy-key",
            "iv": "legacy-iv",
        },
        {
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "request_id": "req-e2ee-only",
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "e2ee_v1": True,
        },
    ]

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    payload = poll.get_json()
    assert payload["request_id"] == "req-e2ee-only"
    assert payload["chat_history"] == "ciphertext-request"
    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests


def test_api_v1_relay_response_plaintext_is_rejected(client):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )

    plaintext = "PLAINTEXT_RESPONSE_SENTINEL_DO_NOT_STORE"
    response_payload = {
        "request_id": "req-response-no-plaintext",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "ciphertext-response-only",
        "cipherkey": "cipherkey-response-only",
        "iv": "iv-response-only",
        "messages": [{"role": "assistant", "content": plaintext}],
        "prompt": plaintext,
        "assistant_output": plaintext,
        "tool_arguments": plaintext,
        "model_output_text": plaintext,
    }
    source = client.post("/api/v1/relay/responses", json=response_payload)
    assert source.status_code == 400
    assert (
        "forbidden; send ciphertext envelope only"
        in source.get_json()["error"]["message"]
    )
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_relay_chat_completions_fail_closed_and_queue_unchanged(client):
    client_inference_requests.clear()
    response = client.post(
        "/relay/api/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "should-not-queue"}],
        },
    )
    assert response.status_code == 503
    assert client_inference_requests == {}


def test_api_v1_register_does_not_dequeue_requests(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    register = client.post("/api/v1/relay/servers/register", json=server_payload)
    assert register.status_code == 200

    request_payload = {
        "request_id": "req-register-heartbeat",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "ciphertext-request",
        "cipherkey": "cipherkey-request",
        "iv": "iv-request",
    }
    queued = client.post("/api/v1/relay/requests", json=request_payload)
    assert queued.status_code == 200

    heartbeat = client.post("/api/v1/relay/servers/register", json=server_payload)
    assert heartbeat.status_code == 200

    # Register/heartbeat should not claim work.
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    claimed = poll.get_json()
    assert claimed["request_id"] == "req-register-heartbeat"
    assert (
        DUMMY_SERVER_PUB_KEY not in client_inference_requests
        or len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 0
    )


def test_api_v1_poll_requires_registration_token_when_configured(client, monkeypatch):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {
            "request_id": "req-auth",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
            "e2ee_v1": True,
        }
    ]

    monkeypatch.setattr(relay_module, "SERVER_REGISTRATION_TOKENS", ["expected-token"])

    unauthorized = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert unauthorized.status_code == 401
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    authorized = client.post(
        "/api/v1/relay/servers/poll",
        json=server_payload,
        headers={"X-Relay-Server-Token": "expected-token"},
    )
    assert authorized.status_code == 200
    assert authorized.get_json()["request_id"] == "req-auth"


def test_legacy_relay_routes_return_410_by_default(client, monkeypatch):
    """Legacy relay routes fail closed with 410 unless compatibility is enabled."""
    monkeypatch.delenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", raising=False)

    checks = [
        ("get", "/next_server", None),
        ("post", "/sink", {"server_public_key": DUMMY_SERVER_PUB_KEY}),
        (
            "post",
            "/faucet",
            {
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": "x",
                "cipherkey": "y",
                "iv": "z",
            },
        ),
        (
            "post",
            "/source",
            {
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": "x",
                "cipherkey": "y",
                "iv": "z",
            },
        ),
        ("post", "/retrieve", {"client_public_key": DUMMY_CLIENT_PUB_KEY}),
    ]

    for method, route, payload in checks:
        if method == "get":
            response = client.get(route)
        else:
            response = client.post(route, json=payload)
        assert response.status_code == 410
        body = response.get_json()
        assert body["error"]["code"] == "legacy_relay_endpoint_deprecated"


def test_legacy_next_server_can_be_enabled_with_compatibility_flag(client, monkeypatch):
    """Compatibility flag restores legacy next_server behavior where still supported."""
    monkeypatch.setenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", "1")

    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    response = client.get("/next_server")
    assert response.status_code == 200
    assert response.get_json()["server_public_key"] == DUMMY_SERVER_PUB_KEY


def test_api_v1_provider_envelope_is_queued_polled_responded_and_retrieved_ciphertext_only(
    client,
):
    client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )

    request_plaintext = "PLAINTEXT_REQUEST_SENTINEL_DO_NOT_STORE"
    request_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-provider-style",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "ciphertext": "ciphertext-request-provider-style",
        "cipherkey": "cipherkey-request-provider-style",
        "iv": "iv-request-provider-style",
    }

    queued = client.post("/api/v1/relay/requests", json=request_payload)
    assert queued.status_code == 200
    relay_state = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert relay_state["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert relay_state["version"] == 1
    assert relay_state["request_id"] == "req-provider-style"
    assert relay_state["e2ee_v1"] is True
    assert "messages" not in relay_state
    assert request_plaintext not in json.dumps(relay_state)

    polled = client.post(
        "/api/v1/relay/servers/poll",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY},
    )
    assert polled.status_code == 200
    polled_payload = polled.get_json()
    assert polled_payload["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert polled_payload["version"] == 1
    assert polled_payload["request_id"] == "req-provider-style"
    assert polled_payload["chat_history"] == "ciphertext-request-provider-style"
    assert request_plaintext not in json.dumps(polled_payload)

    response_plaintext = "PLAINTEXT_RESPONSE_SENTINEL_DO_NOT_STORE"
    response_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-provider-style",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "ciphertext": "ciphertext-response-provider-style",
        "cipherkey": "cipherkey-response-provider-style",
        "iv": "iv-response-provider-style",
    }
    submitted = client.post("/api/v1/relay/responses", json=response_payload)
    assert submitted.status_code == 200
    queued_response = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert queued_response["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert queued_response["request_id"] == "req-provider-style"
    assert "api_v1_response" not in queued_response
    assert response_plaintext not in json.dumps(queued_response)

    retrieved = client.post(
        "/api/v1/relay/responses/retrieve",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "request_id": "req-provider-style",
        },
    )
    assert retrieved.status_code == 200
    retrieved_payload = retrieved.get_json()
    assert retrieved_payload["request_id"] == "req-provider-style"
    assert retrieved_payload["chat_history"] == "ciphertext-response-provider-style"
    assert response_plaintext not in json.dumps(retrieved_payload)


def test_api_v1_poll_requeues_popped_work_if_server_unregistered_before_dispatch(
    client, monkeypatch
):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )

    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-requeue-on-unregister-race",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )
    assert queued.status_code == 200

    original_pop = relay_module._pop_next_api_v1_request

    def _pop_then_unregister(public_key):
        popped = original_pop(public_key)
        if popped is not None:
            known_servers.pop(public_key, None)
        return popped

    monkeypatch.setattr(relay_module, "_pop_next_api_v1_request", _pop_then_unregister)

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 404

    queued_after = client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])
    assert len(queued_after) == 1
    assert queued_after[0]["request_id"] == "req-requeue-on-unregister-race"


def test_api_v1_poll_long_wait_dispatches_when_request_arrives(client, monkeypatch):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0.5")

    result = {}

    def _poll():
        with app.test_client() as polling_client:
            response = polling_client.post(
                "/api/v1/relay/servers/poll", json=server_payload
            )
            result["status"] = response.status_code
            result["json"] = response.get_json()

    poll_thread = threading.Thread(target=_poll)
    poll_thread.start()
    time.sleep(0.05)

    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-long-poll-dispatch",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )
    assert queued.status_code == 200

    poll_thread.join(timeout=1.0)
    assert not poll_thread.is_alive()
    assert result["status"] == 200
    assert result["json"]["request_id"] == "req-long-poll-dispatch"
    assert "_queued_at" not in result["json"]


def test_api_v1_poll_long_wait_timeout_returns_no_work(client, monkeypatch):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0.01")

    started = time.monotonic()
    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    elapsed = time.monotonic() - started
    assert poll.status_code == 200
    payload = poll.get_json()
    assert payload["message"] == "No requests available"
    assert payload["next_ping_in_x_seconds"] == 0
    assert payload["poll_wait_seconds"] == 0.01
    assert elapsed >= 0.008


def test_api_v1_poll_delivers_fifo_for_multiple_requests(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )

    for request_id in ("req-fifo-1", "req-fifo-2"):
        queued = client.post(
            "/api/v1/relay/requests",
            json={
                "request_id": request_id,
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": f"ciphertext-{request_id}",
                "cipherkey": "cipherkey-request",
                "iv": "iv-request",
            },
        )
        assert queued.status_code == 200

    first = client.post("/api/v1/relay/servers/poll", json=server_payload)
    second = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["request_id"] == "req-fifo-1"
    assert second.get_json()["request_id"] == "req-fifo-2"


def test_api_v1_poll_refreshes_server_lease(client, monkeypatch):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    time.sleep(0.6)
    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    time.sleep(0.6)
    assert (
        client.post("/api/v1/relay/servers/poll", json=server_payload).status_code
        == 200
    )


def test_api_v1_stale_server_expires_without_poll_heartbeat(client, monkeypatch):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS", "1")
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    known_servers[DUMMY_SERVER_PUB_KEY]["last_ping"] = datetime.now() - timedelta(
        seconds=2
    )
    expired = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert expired.status_code == 404


def test_api_v1_poll_long_wait_ignores_unrelated_server_wakeups(client, monkeypatch):
    server_one = base64.b64encode(b"server_public_key_1").decode("utf-8")
    server_two = base64.b64encode(b"server_public_key_2").decode("utf-8")
    assert (
        client.post(
            "/api/v1/relay/servers/register", json={"server_public_key": server_one}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/relay/servers/register", json={"server_public_key": server_two}
        ).status_code
        == 200
    )
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0.25")

    result = {}

    def _poll_server_one():
        with app.test_client() as polling_client:
            response = polling_client.post(
                "/api/v1/relay/servers/poll", json={"server_public_key": server_one}
            )
            result["status"] = response.status_code
            result["json"] = response.get_json()

    poll_thread = threading.Thread(target=_poll_server_one)
    poll_thread.start()
    time.sleep(0.05)

    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-for-server-two",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": server_two,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )
    assert queued.status_code == 200

    time.sleep(0.05)
    assert poll_thread.is_alive()

    poll_server_two = client.post(
        "/api/v1/relay/servers/poll", json={"server_public_key": server_two}
    )
    assert poll_server_two.status_code == 200
    assert poll_server_two.get_json()["request_id"] == "req-for-server-two"

    poll_thread.join(timeout=0.5)
    assert not poll_thread.is_alive()
    assert result["status"] == 200
    assert result["json"]["message"] == "No requests available"


def test_api_v1_poll_long_wait_wakes_on_shared_queue_legacy_compat_enqueue(
    client, monkeypatch
):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0.5")

    result = {}

    def _poll():
        with app.test_client() as polling_client:
            response = polling_client.post(
                "/api/v1/relay/servers/poll", json=server_payload
            )
            result["status"] = response.status_code
            result["json"] = response.get_json()

    poll_thread = threading.Thread(target=_poll)
    poll_thread.start()
    time.sleep(0.05)

    queued = client.post(
        "/faucet",
        json={
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "legacy-ciphertext-request",
            "cipherkey": "legacy-cipherkey-request",
            "iv": "legacy-iv-request",
        },
    )
    assert queued.status_code == 200

    poll_thread.join(timeout=1.0)
    assert not poll_thread.is_alive()
    assert result["status"] == 200
    assert result["json"]["chat_history"] == "legacy-ciphertext-request"


def test_api_v1_next_keeps_in_flight_server_alive_then_expires(client, monkeypatch):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    monkeypatch.setenv("TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS", "3")
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )

    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-inflight-1",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )
    assert queued.status_code == 200

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    assert poll.get_json()["request_id"] == "req-inflight-1"

    time.sleep(1.2)
    next_response = client.get("/api/v1/relay/servers/next")
    assert next_response.status_code == 200
    assert next_response.get_json().get("server_public_key") == DUMMY_SERVER_PUB_KEY

    time.sleep(2.1)
    expired = client.get("/api/v1/relay/servers/next")
    assert expired.status_code == 503


def test_api_v1_next_does_not_keep_stale_server_alive_after_in_flight_response_removed(
    client, monkeypatch
):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    monkeypatch.setenv("TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS", "10")
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )

    queued = client.post(
        "/api/v1/relay/requests",
        json={
            "request_id": "req-race-finished",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": "ciphertext-request",
            "cipherkey": "cipherkey-request",
            "iv": "iv-request",
        },
    )
    assert queued.status_code == 200

    poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert poll.status_code == 200
    assert poll.get_json()["request_id"] == "req-race-finished"

    # Complete/remove the only in-flight request, then force stale lease.
    response = client.post(
        "/api/v1/relay/responses",
        json={
            "request_id": "req-race-finished",
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-response",
            "cipherkey": "cipherkey-response",
            "iv": "iv-response",
        },
    )
    assert response.status_code == 200

    known_servers[DUMMY_SERVER_PUB_KEY]["last_ping"] = datetime.now() - timedelta(
        seconds=5
    )

    next_response = client.get("/api/v1/relay/servers/next")
    assert next_response.status_code == 503


def test_api_v1_next_keeps_server_alive_while_any_in_flight_request_remains(
    client, monkeypatch
):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    monkeypatch.setenv("TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS", "3")
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )

    for request_id in ("req-inflight-a", "req-inflight-b"):
        queued = client.post(
            "/api/v1/relay/requests",
            json={
                "request_id": request_id,
                "client_public_key": DUMMY_CLIENT_PUB_KEY,
                "server_public_key": DUMMY_SERVER_PUB_KEY,
                "chat_history": f"ciphertext-{request_id}",
                "cipherkey": "cipherkey-request",
                "iv": "iv-request",
            },
        )
        assert queued.status_code == 200

    first_poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    second_poll = client.post("/api/v1/relay/servers/poll", json=server_payload)
    assert first_poll.status_code == 200
    assert second_poll.status_code == 200

    first_request_id = first_poll.get_json()["request_id"]
    second_request_id = second_poll.get_json()["request_id"]
    assert {first_request_id, second_request_id} == {"req-inflight-a", "req-inflight-b"}

    response = client.post(
        "/api/v1/relay/responses",
        json={
            "request_id": second_request_id,
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "ciphertext-response",
            "cipherkey": "cipherkey-response",
            "iv": "iv-response",
        },
    )
    assert response.status_code == 200

    time.sleep(1.2)
    next_response = client.get("/api/v1/relay/servers/next")
    assert next_response.status_code == 200
    assert next_response.get_json().get("server_public_key") == DUMMY_SERVER_PUB_KEY


def test_api_v1_unregister_removes_known_server_and_next_skips_it(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    assert (
        client.post("/api/v1/relay/servers/register", json=server_payload).status_code
        == 200
    )
    assert client.get("/api/v1/relay/servers/next").status_code == 200

    unregistered = client.post("/unregister", json=server_payload)

    assert unregistered.status_code == 200
    assert unregistered.get_json()["removed"] is True
    diagnostics = client.get("/relay/diagnostics").get_json()
    assert diagnostics["total_registered_compute_nodes"] == 0
    next_response = client.get("/api/v1/relay/servers/next")
    assert next_response.status_code == 503


def test_api_v1_unregister_is_idempotent_when_server_already_gone(client):
    server_payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}

    first = client.post("/unregister", json=server_payload)
    second = client.post("/unregister", json=server_payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["removed"] is False
    assert second.get_json()["removed"] is False
