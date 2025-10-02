import json

import pytest

from relay import app as relay_app
from api.v1 import routes as v1_routes
from api.v2 import routes as v2_routes


@pytest.fixture
def client():
    relay_app.config["TESTING"] = True
    with relay_app.test_client() as test_client:
        yield test_client


def test_v2_streaming_chat_completion(client, monkeypatch):
    """Streaming chat completions should return Server-Sent Events chunks."""
    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Count from 1 to 5"}
        ],
        "stream": True
    }

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    def fake_generate_response(model_id, messages, **model_options):
        assert model_id == "llama-3-8b-instruct"
        assert messages[-1]["content"] == "Count from 1 to 5"
        assert model_options == {}
        return messages + [{"role": "assistant", "content": "1, 2, 3, 4, 5"}]

    monkeypatch.setattr(v2_routes, "generate_response", fake_generate_response)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    events = []
    for raw_chunk in response.iter_encoded():
        text = raw_chunk.decode("utf-8")
        if not text.strip():
            continue
        assert text.startswith("data: ")
        events.append(text[len("data: "):].strip())

    assert events[-1] == "[DONE]"

    role_event = json.loads(events[0])
    content_event = json.loads(events[1])
    stop_event = json.loads(events[2])

    assert role_event["choices"][0]["delta"] == {"role": "assistant"}
    assert content_event["choices"][0]["delta"]["content"] == "1, 2, 3, 4, 5"
    assert stop_event["choices"][0]["finish_reason"] == "stop"


def test_v2_encrypted_streaming_falls_back_to_single_response(client, monkeypatch):
    """Encrypted streaming requests should fall back to encrypted single responses."""

    class DummyEncryptionManager:
        public_key_b64 = "server-public-key"

        def decrypt_message(self, encrypted_payload, cipherkey):
            _ = encrypted_payload, cipherkey
            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say hello."}
            ]
            return json.dumps(messages).encode("utf-8")

        def encrypt_message(self, response_data, client_public_key):
            assert client_public_key == "client-public-key"
            return {"ciphertext": "encrypted", "iv": "iv", "cipherkey": "key"}

    payload = {
        "model": "llama-3-8b-instruct",
        "stream": True,
        "encrypted": True,
        "client_public_key": "client-public-key",
        "messages": {
            "ciphertext": "Y2lwaGVydGV4dA==",
            "cipherkey": "Y2lwaGVya2V5",
            "iv": "aXY="
        }
    }

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v2_routes, "encryption_manager", DummyEncryptionManager())
    monkeypatch.setattr(v2_routes, "validate_encrypted_request", lambda data: None)

    def fake_generate_response(model_id, messages, **model_options):
        assert messages[-1]["content"] == "Say hello."
        assert model_options == {}
        return messages + [{"role": "assistant", "content": "Hello!"}]

    monkeypatch.setattr(v2_routes, "generate_response", fake_generate_response)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.is_json

    data = response.get_json()
    assert data == {
        "encrypted": True,
        "data": {"ciphertext": "encrypted", "iv": "iv", "cipherkey": "key"}
    }


def test_v2_streaming_with_tool_use(client, monkeypatch):
    """Streaming responses should surface tool call deltas when tools are requested."""

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant with tool use capabilities."},
            {"role": "user", "content": "What's the weather in San Francisco?"}
        ],
        "stream": True,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather in a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "The city and state, e.g. San Francisco, CA"
                            }
                        },
                        "required": ["location"]
                    }
                }
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    }

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    def fake_generate_response(model_id, messages, **model_options):
        assert model_options.get("tools") == payload["tools"]
        assert model_options.get("tool_choice") == payload["tool_choice"]
        call = {
            "id": "call_get_weather",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": json.dumps({"location": "San Francisco"})
            }
        }
        return messages + [{"role": "assistant", "tool_calls": [call], "content": None}]

    monkeypatch.setattr(v2_routes, "generate_response", fake_generate_response)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    events = []
    for raw_chunk in response.iter_encoded():
        text = raw_chunk.decode("utf-8")
        if not text.strip():
            continue
        assert text.startswith("data: ")
        events.append(text[len("data: "):].strip())

    assert events[-1] == "[DONE]"

    role_event = json.loads(events[0])
    tool_event = json.loads(events[1])
    finish_event = json.loads(events[2])

    assert role_event["choices"][0]["delta"] == {"role": "assistant"}

    tool_delta = tool_event["choices"][0]["delta"]["tool_calls"][0]
    assert tool_delta["id"] == "call_get_weather"
    assert tool_delta["type"] == "function"
    assert tool_delta["function"]["name"] == "get_weather"
    assert json.loads(tool_delta["function"]["arguments"]) == {"location": "San Francisco"}

    assert finish_event["choices"][0]["finish_reason"] == "tool_calls"


def test_v1_stream_flag_is_ignored(client, monkeypatch):
    """API v1 should ignore stream requests and return a JSON payload."""

    monkeypatch.setattr(v1_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v1_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v1_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v1_routes, "validate_chat_messages", lambda msgs: None)

    def fake_generate_response(model_id, messages, **model_options):
        assert model_options == {}
        return messages + [{"role": "assistant", "content": "Hello"}]

    monkeypatch.setattr(v1_routes, "generate_response", fake_generate_response)

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "Ping"}],
        "stream": True
    }

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json()["choices"][0]["message"]["content"] == "Hello"
