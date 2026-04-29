import pytest
import time
import base64
import json
from flask import Flask
import sys
import os
from datetime import datetime, timedelta
import relay as relay_module
from utils.networking.relay_client import RelayClient

# Add project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from relay import app

# Import the global dictionaries from relay to inspect/manipulate state if needed
# Be cautious with direct manipulation in tests, prefer using API endpoints
from relay import (
    known_servers,
    client_inference_requests,
    client_responses,
    streaming_sessions,
    streaming_sessions_by_client,
)

# Generate dummy keys for testing
# (You might want to use the generate_keys function from encrypt.py if needed)
DUMMY_SERVER_PUB_KEY = base64.b64encode(b"server_public_key_123").decode('utf-8')
DUMMY_CLIENT_PUB_KEY = base64.b64encode(b"client_public_key_456").decode('utf-8')


@pytest.fixture
def client():
    """Create a Flask test client fixture"""
    app.config['TESTING'] = True
    # Reset state before each test
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()

    with app.test_client() as client:
        yield client

    # Clean up state after test (optional, as fixture resets before)
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()


def test_inference_endpoint_removed(client):
    """Ensure deprecated /inference endpoint is unavailable."""
    response = client.post("/inference", json={})
    assert response.status_code == 404

# --- Test /next_server ---

def test_next_server_no_servers(client):
    """Test /next_server when no servers are registered."""
    response = client.get("/next_server")
    assert response.status_code == 200 # Endpoint itself works
    data = response.get_json()
    assert 'error' in data
    assert data['error']['message'] == 'No servers available'
    assert data['error']['code'] == 503

def test_next_server_one_server(client):
    """Test /next_server when one server is registered."""
    # Simulate server registration (directly modifying state for setup)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 10
    }

    response = client.get("/next_server")
    assert response.status_code == 200
    data = response.get_json()
    assert 'error' not in data
    assert 'server_public_key' in data
    assert data['server_public_key'] == DUMMY_SERVER_PUB_KEY


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

    assert response.status_code == 200
    assert payload["error"]["message"] == "No servers available"
    assert DUMMY_SERVER_PUB_KEY not in known_servers

# --- Test /sink ---

def test_sink_register_new_server(client):
    """Test server registration via /sink."""
    payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert 'next_ping_in_x_seconds' in data
    assert DUMMY_SERVER_PUB_KEY in known_servers
    assert known_servers[DUMMY_SERVER_PUB_KEY]['public_key'] == DUMMY_SERVER_PUB_KEY

def test_sink_update_existing_server(client):
    """Test server ping update via /sink."""
    # Initial registration using datetime
    initial_ping_time = datetime.now() - timedelta(seconds=20)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': initial_ping_time,
        'last_ping_duration': 10
    }

    time.sleep(0.1) # Ensure time progresses slightly

    # Send update ping
    payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200

    assert DUMMY_SERVER_PUB_KEY in known_servers
    # Compare datetime objects
    assert known_servers[DUMMY_SERVER_PUB_KEY]['last_ping'] > initial_ping_time

def test_sink_invalid_payload(client):
    """Test /sink with missing public key."""
    response = client.post("/sink", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid public key'


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
    sink_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
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

    assert 'batch' in batch_data
    assert isinstance(batch_data['batch'], list)
    assert len(batch_data['batch']) == 2

    first_request, second_request = batch_data['batch']
    assert first_request['chat_history'] == "encrypted_payload_0"
    assert first_request['client_public_key'] == batch_data['client_public_key']
    assert second_request['chat_history'] == "encrypted_payload_1"

    remaining_queue = client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])
    assert len(remaining_queue) == 1
    assert remaining_queue[0]['chat_history'] == "encrypted_payload_2"


def test_two_servers_receive_only_addressed_work(client):
    """Queued work should remain isolated by server public key."""
    server_one = base64.b64encode(b"server_public_key_1").decode("utf-8")
    server_two = base64.b64encode(b"server_public_key_2").decode("utf-8")

    assert client.post("/sink", json={"server_public_key": server_one}).status_code == 200
    assert client.post("/sink", json={"server_public_key": server_two}).status_code == 200

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
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(model, messages, **options):
        captured["model"] = model
        captured["messages"] = messages
        captured["options"] = options
        return messages + [{"role": "assistant", "content": "bonjour"}]

    def fake_post(url, json, timeout, **_kwargs):
        assert url == "https://relay.example/source"
        assert timeout == relay_client._request_timeout
        assert "chat_history" in json and "cipherkey" in json and "iv" in json
        assert "messages" not in json
        assert "prompt" not in json
        assert "model" not in json
        assert "api_v1_response" not in json

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    assert captured["model"] == "llama-3-8b-instruct:alignment"
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["options"] == {"temperature": 0.2, "max_tokens": 42}
    assert crypto_stub.last_encrypted_payload["api_v1_response"]["message"]["content"] == "bonjour"


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
        raise ModelError("Model 'unknown-model-id' not found", status_code=404, error_type="model_not_found")

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None
        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
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
    assert encrypted_payload["api_v1_response"]["error"]["code"] == "compute_node_model_unsupported"


def test_relay_client_api_v1_falls_back_to_runtime_model_when_catalog_model_unavailable(monkeypatch):
    from api.v1.models import ModelError

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

    class _RuntimeModelManager:
        @staticmethod
        def llama_cpp_get_response(messages):
            return messages + [{"role": "assistant", "content": "Paris"}]

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_generate_response(*_args, **_kwargs):
        raise ModelError("Model file missing", status_code=500, error_type="model_load_error")

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
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


def test_relay_client_api_v1_posts_encrypted_internal_error_for_unexpected_exception(monkeypatch):
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
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(*_args, **_kwargs):
        raise RuntimeError("backend crashed")

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
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
    assert encrypted_payload["api_v1_response"]["error"]["code"] == "compute_node_internal_error"


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

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
    monkeypatch.setattr("utils.networking.relay_client.requests.post", raising_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is False


@pytest.mark.parametrize(
    ("generated_response", "expected_error_message"),
    [
        ([], "LLM returned invalid response history"),
        ([{"role": "assistant", "content": "ok"}, "bad-last-message"], "LLM returned invalid assistant message"),
    ],
)
def test_relay_client_api_v1_posts_encrypted_internal_error_for_invalid_inference_output(
    monkeypatch,
    generated_response,
    expected_error_message,
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
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(*_args, **_kwargs):
        return generated_response

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
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
    assert encrypted_payload["api_v1_response"]["error"]["code"] == "compute_node_internal_error"
    assert encrypted_payload["api_v1_response"]["error"]["message"] == expected_error_message

# --- Test /faucet ---

def test_faucet_submit_request(client):
    """Test submitting a valid inference request via /faucet."""
    # Register server first
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': time.time(),
        'last_ping_duration': 10
    }

    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector"
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['message'] == 'Request received'

    # Check internal state
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1
    queued_req = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert queued_req['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert queued_req['chat_history'] == "encrypted_chat_history_data"

def test_faucet_invalid_payload(client):
    """Test /faucet with missing fields."""
    # Register server
    known_servers[DUMMY_SERVER_PUB_KEY] = {'public_key': DUMMY_SERVER_PUB_KEY, 'last_ping': time.time(), 'last_ping_duration': 10}

    payload = { "server_public_key": DUMMY_SERVER_PUB_KEY } # Missing other fields
    response = client.post("/faucet", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error']['message'] == 'Invalid request data'

def test_faucet_unknown_server(client):
    """Test /faucet request to an unknown server."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": "unknown_server_key", # This server is not registered
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector"
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 404
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == {'message': 'Server with the specified public key not found', 'code': 404}


def test_relay_diagnostics_distinguishes_configured_and_live_nodes(client):
    """Diagnostics should expose configured URLs and live compute registrations."""
    app.config["relay_configured_servers"] = [
        "https://configured-one.example.com:8000",
        "https://configured-two.example.com:8000",
    ]
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {"chat_history": "pending", "client_public_key": "c", "cipherkey": "k", "iv": "i"}
    ]

    response = client.get("/relay/diagnostics")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["configured_upstream_servers"] == app.config["relay_configured_servers"]
    assert payload["total_registered_compute_nodes"] == 1
    assert payload["registered_compute_nodes"][0]["server_public_key"] == DUMMY_SERVER_PUB_KEY
    assert payload["registered_compute_nodes"][0]["queue_depth"] == 1


def test_healthz_reports_configured_upstreams_and_live_queue_depth(client):
    """Healthz should separate configured upstream URLs from live registered nodes."""
    app.config["gpu_host"] = None
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

    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["configuredUpstreamServers"] == configured_servers
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

# --- Test /source ---

def test_source_submit_response(client):
    """Test server submitting a response via /source."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_encrypted_response_history",
        "cipherkey": "server_encrypted_aes_key",
        "iv": "server_iv"
    }
    response = client.post("/source", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['message'] == 'Response received and queued for client'

    # Check internal state
    assert DUMMY_CLIENT_PUB_KEY in client_responses
    queued_resp = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert queued_resp['chat_history'] == "server_encrypted_response_history"

def test_source_invalid_payload(client):
    """Test /source with missing fields."""
    payload = { "client_public_key": DUMMY_CLIENT_PUB_KEY } # Missing other fields
    response = client.post("/source", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid request data'

# --- Test /retrieve ---

def test_retrieve_get_response(client):
    """Test client retrieving a queued response via /retrieve."""
    # Queue a response first (directly modify state for setup)
    client_responses[DUMMY_CLIENT_PUB_KEY] = {
        'chat_history': "server_encrypted_response_history",
        'cipherkey': "server_encrypted_aes_key",
        'iv': "server_iv"
    }

    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200
    data = response.get_json()

    assert data['chat_history'] == "server_encrypted_response_history"
    assert data['cipherkey'] == "server_encrypted_aes_key"
    assert data['iv'] == "server_iv"

    # Check state - response should be removed after retrieval
    assert DUMMY_CLIENT_PUB_KEY not in client_responses

def test_retrieve_no_response_available(client):
    """Test /retrieve when no response is queued for the client."""
    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200 # Endpoint works, just no data
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'No response available for the given public key'

def test_retrieve_invalid_payload(client):
    """Test /retrieve with missing client public key."""
    response = client.post("/retrieve", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid request data'

# --- Integration Test ---

def test_full_relay_flow(client):
    """Test the full flow: register, faucet, sink poll, source, retrieve."""
    # 1. Server registers via /sink
    sink_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in known_servers

    # 2. Client requests inference via /faucet
    faucet_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "client_request_data",
        "cipherkey": "client_key_data",
        "iv": "client_iv_data"
    }
    response = client.post("/faucet", json=faucet_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    # 3. Server polls /sink and gets the request
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    sink_data = response.get_json()
    assert sink_data['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert sink_data['chat_history'] == "client_request_data"
    assert sink_data['cipherkey'] == "client_key_data"
    assert sink_data['iv'] == "client_iv_data"
    # Request should be removed from queue
    assert not client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])

    # 4. Server processes and submits response via /source
    source_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_response_data",
        "cipherkey": "server_key_data",
        "iv": "server_iv_data"
    }
    response = client.post("/source", json=source_payload)
    assert response.status_code == 200
    assert DUMMY_CLIENT_PUB_KEY in client_responses

    # 5. Client retrieves response via /retrieve
    retrieve_payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=retrieve_payload)
    assert response.status_code == 200
    retrieve_data = response.get_json()
    assert retrieve_data['chat_history'] == "server_response_data"
    assert retrieve_data['cipherkey'] == "server_key_data"
    assert retrieve_data['iv'] == "server_iv_data"


def test_streaming_state_lifecycle(client):
    """Streaming sessions should store and release chunk state per client."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': time.time(),
        'last_ping_duration': 10,
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

    sink_response = client.post("/sink", json={"server_public_key": DUMMY_SERVER_PUB_KEY})
    assert sink_response.status_code == 200
    sink_data = sink_response.get_json()
    assert sink_data.get('stream') is True
    session_id = sink_data.get('stream_session_id')
    assert session_id
    assert session_id in streaming_sessions
    session_state = streaming_sessions[session_id]
    assert session_state['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert session_state['status'] == 'open'

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
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200

    request_payload = {
        'request_id': 'req-123',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    }
    queued = client.post('/api/v1/relay/requests', json=request_payload)
    assert queued.status_code == 200

    poll = client.post('/api/v1/relay/servers/poll', json=server_payload)
    assert poll.status_code == 200
    polled_payload = poll.get_json()
    assert polled_payload['chat_history'] == 'ciphertext-request'
    assert polled_payload['cipherkey'] == 'cipherkey-request'
    assert polled_payload['iv'] == 'iv-request'
    assert polled_payload['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert polled_payload['request_id'] == 'req-123'
    assert polled_payload['protocol'] == 'tokenplace_api_v1_relay_e2ee'
    assert polled_payload['version'] == 1

    response_payload = {
        'request_id': 'req-123',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response',
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    }
    source = client.post('/api/v1/relay/responses', json=response_payload)
    assert source.status_code == 200

    retrieved = client.post('/api/v1/relay/responses/retrieve', json={'client_public_key': DUMMY_CLIENT_PUB_KEY})
    assert retrieved.status_code == 200
    retrieved_payload = retrieved.get_json()
    assert retrieved_payload['chat_history'] == 'ciphertext-response'
    assert retrieved_payload['cipherkey'] == 'cipherkey-response'
    assert retrieved_payload['iv'] == 'iv-response'
    assert retrieved_payload['request_id'] == 'req-123'
    assert retrieved_payload['protocol'] == 'tokenplace_api_v1_relay_e2ee'


def test_api_v1_relay_plaintext_messages_not_stored(client):
    client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    plaintext = 'PLAINTEXT_SENTINEL_DO_NOT_STORE'
    payload = {
        'request_id': 'req-no-messages',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-only',
        'cipherkey': 'cipherkey-only',
        'iv': 'iv-only',
        'messages': [{'role': 'user', 'content': plaintext}],
        'prompt': plaintext,
    }
    response = client.post('/api/v1/relay/requests', json=payload)
    assert response.status_code == 200

    queued_payload = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert 'messages' not in queued_payload
    assert 'prompt' not in queued_payload
    assert plaintext not in json.dumps(queued_payload)


def test_api_v1_relay_response_plaintext_not_stored(client):
    client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    plaintext = 'PLAINTEXT_RESPONSE_SENTINEL_DO_NOT_STORE'
    response_payload = {
        'request_id': 'req-response-no-plaintext',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response-only',
        'cipherkey': 'cipherkey-response-only',
        'iv': 'iv-response-only',
        'messages': [{'role': 'assistant', 'content': plaintext}],
        'prompt': plaintext,
        'assistant_output': plaintext,
        'tool_arguments': plaintext,
        'model_output_text': plaintext,
    }
    source = client.post('/api/v1/relay/responses', json=response_payload)
    assert source.status_code == 200

    queued_payload = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert 'messages' not in queued_payload
    assert 'prompt' not in queued_payload
    assert 'assistant_output' not in queued_payload
    assert 'tool_arguments' not in queued_payload
    assert 'model_output_text' not in queued_payload
    assert plaintext not in json.dumps(queued_payload)

    retrieved = client.post('/api/v1/relay/responses/retrieve', json={'client_public_key': DUMMY_CLIENT_PUB_KEY})
    assert retrieved.status_code == 200
    assert plaintext not in json.dumps(retrieved.get_json())


def test_api_v1_relay_chat_completions_fail_closed_and_queue_unchanged(client):
    client_inference_requests.clear()
    response = client.post('/relay/api/v1/chat/completions', json={
        'model': 'x',
        'messages': [{'role': 'user', 'content': 'should-not-queue'}],
    })
    assert response.status_code == 503
    assert client_inference_requests == {}


def test_api_v1_register_does_not_dequeue_requests(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200

    request_payload = {
        'request_id': 'req-register-heartbeat',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    }
    queued = client.post('/api/v1/relay/requests', json=request_payload)
    assert queued.status_code == 200

    heartbeat = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert heartbeat.status_code == 200

    # Register/heartbeat should not claim work.
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    poll = client.post('/api/v1/relay/servers/poll', json=server_payload)
    assert poll.status_code == 200
    claimed = poll.get_json()
    assert claimed['request_id'] == 'req-register-heartbeat'
    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests or len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 0
