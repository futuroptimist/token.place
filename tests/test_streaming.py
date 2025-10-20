import base64
import json
import time
from itertools import accumulate

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


def test_v2_encrypted_streaming_emits_encrypted_chunks(client, monkeypatch):
    """Encrypted streaming requests should emit encrypted Server-Sent Events chunks."""

    class DummyEncryptionManager:
        public_key_b64 = "server-public-key"

        def __init__(self):
            self.calls = []

        def decrypt_message(self, encrypted_payload, cipherkey):
            _ = encrypted_payload, cipherkey
            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say hello."}
            ]
            return json.dumps(messages).encode("utf-8")

        def encrypt_message(self, response_data, client_public_key):
            assert client_public_key == "client-public-key"
            self.calls.append(response_data)
            payload_bytes = json.dumps(response_data).encode("utf-8")
            ciphertext = base64.b64encode(payload_bytes).decode("utf-8")
            index = len(self.calls)
            return {
                "encrypted": True,
                "ciphertext": ciphertext,
                "iv": f"iv-{index}",
                "cipherkey": f"key-{index}",
            }

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

    dummy_manager = DummyEncryptionManager()

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v2_routes, "encryption_manager", dummy_manager)
    monkeypatch.setattr(v2_routes, "validate_encrypted_request", lambda data: None)

    def fake_generate_response(model_id, messages, **model_options):
        assert messages[-1]["content"] == "Say hello."
        assert model_options == {}
        return messages + [{"role": "assistant", "content": "Hello!"}]

    monkeypatch.setattr(v2_routes, "generate_response", fake_generate_response)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    events = []
    for raw_chunk in response.iter_encoded():
        text = raw_chunk.decode("utf-8").strip()
        if not text:
            continue
        assert text.startswith("data: ")
        events.append(text[len("data: "):].strip())

    assert events[-1] == "[DONE]"

    decrypted_chunks = []
    for raw_event in events[:-1]:
        envelope = json.loads(raw_event)
        assert envelope["encrypted"] is True
        assert envelope["event"] == "delta"
        encoded_payload = envelope["data"]["ciphertext"]
        payload_json = base64.b64decode(encoded_payload.encode("utf-8")).decode("utf-8")
        decrypted_chunks.append(json.loads(payload_json))

    assert len(decrypted_chunks) == 3
    assert decrypted_chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert decrypted_chunks[1]["choices"][0]["delta"]["content"] == "Hello!"
    assert decrypted_chunks[2]["choices"][0]["finish_reason"] == "stop"

    # Ensure each chunk was encrypted independently
    assert [call["choices"][0]["delta"].get("content") for call in dummy_manager.calls] == [None, "Hello!", None]


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


def test_v2_streaming_handles_varied_chunk_sizes_and_delays(client, monkeypatch):
    """Streaming should deliver staggered content chunks with preserved ordering."""

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "Stream timing probe."},
            {"role": "user", "content": "Generate a long multiline explanation."}
        ],
        "stream": True,
    }

    long_content = (
        "token.place demonstrates streaming by gradually emitting chunks of "
        "assistant output so clients can render partial responses before the "
        "full completion arrives. This test feeds a lengthy body to exercise "
        "chunk boundaries and confirm delays are respected."
    )

    monkeypatch.setattr(v2_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v2_routes, "get_model_instance", lambda model_id: object())

    def fake_generate_response(model_id, messages, **model_options):
        assert model_id == "llama-3-8b-instruct"
        assert messages[-1]["content"] == "Generate a long multiline explanation."
        assert model_options == {}
        return messages + [{"role": "assistant", "content": long_content}]

    monkeypatch.setattr(v2_routes, "generate_response", fake_generate_response)

    segments = [
        long_content[:60],
        long_content[60:180],
        long_content[180:],
    ]
    delays = [0.015, 0.025, 0.01]

    def delayed_chunker(content: str, max_chunk_size: int = 512):
        assert content == long_content
        for segment, delay in zip(segments, delays):
            time.sleep(delay)
            yield segment

    monkeypatch.setattr(v2_routes, "iter_stream_content_chunks", delayed_chunker)

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    role_arrival = None
    content_segments = []
    content_arrivals = []

    for raw_chunk in response.iter_encoded():
        now = time.perf_counter()
        text = raw_chunk.decode("utf-8")
        if not text.strip():
            continue
        assert text.startswith("data: ")
        payload_text = text[len("data: "):].strip()
        if payload_text == "[DONE]":
            break

        chunk = json.loads(payload_text)
        delta = chunk["choices"][0]["delta"]
        if "role" in delta:
            role_arrival = now
            continue
        if "content" in delta:
            content_segments.append(delta["content"])
            content_arrivals.append(now)
            continue
        # Ignore stop chunks; they are validated in other tests

    assert role_arrival is not None, "Expected an assistant role chunk before content"
    assert content_segments == segments
    assert "".join(content_segments) == long_content

    expected_cumulative = list(accumulate(delays))
    observed_cumulative = []
    previous_time = role_arrival
    for arrival in content_arrivals:
        observed_cumulative.append(arrival - role_arrival)
        assert arrival >= previous_time
        previous_time = arrival

    for observed, expected in zip(observed_cumulative, expected_cumulative):
        # Allow small scheduling variance but require the delay to be respected
        assert observed >= expected - 0.01


def test_v1_chat_completion_stream_flag_returns_error(client, monkeypatch):
    """API v1 chat completions should reject stream flags with an error."""

    monkeypatch.setattr(v1_routes, "get_models_info", lambda: [{"id": "llama-3-8b-instruct"}])
    monkeypatch.setattr(v1_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v1_routes, "get_model_instance", lambda model_id: object())
    monkeypatch.setattr(v1_routes, "validate_chat_messages", lambda msgs: None)

    class AllowDecision:
        allowed = True

    monkeypatch.setattr(v1_routes, "evaluate_messages_for_policy", lambda msgs: AllowDecision())

    def fake_generate_response(model_id, messages, **model_options):
        assert model_options == {}
        return messages + [{"role": "assistant", "content": "Hello"}]

    monkeypatch.setattr(v1_routes, "generate_response", fake_generate_response)

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "Ping"}],
        "stream": True,
    }

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 400
    assert response.is_json
    body = response.get_json()
    assert body == {
        "error": {
            "message": (
                "Streaming is not supported for API v1 chat completions. "
                "Use /api/v2/chat/completions for Server-Sent Events."
            ),
            "type": "invalid_request_error",
            "param": "stream",
        }
    }


def test_v1_text_completion_stream_flag_returns_error(client, monkeypatch):
    """Legacy text completions should reject stream flags with an error."""

    monkeypatch.setattr(v1_routes, "get_models_info", lambda: [{"id": "text-davinci-003"}])
    monkeypatch.setattr(v1_routes, "validate_model_name", lambda *a, **k: None)
    monkeypatch.setattr(v1_routes, "validate_required_fields", lambda *a, **k: None)
    monkeypatch.setattr(v1_routes, "validate_field_type", lambda *a, **k: None)
    monkeypatch.setattr(v1_routes, "validate_chat_messages", lambda *a, **k: None)
    monkeypatch.setattr(v1_routes, "get_model_instance", lambda model_id: object())

    def fake_generate_response(model_id, messages, **model_options):
        assert model_id == "text-davinci-003"
        assert model_options == {}
        assert messages[-1]["content"] == "Write a haiku"
        return messages + [{"role": "assistant", "content": "Five syllables start"}]

    monkeypatch.setattr(v1_routes, "generate_response", fake_generate_response)

    payload = {
        "model": "text-davinci-003",
        "prompt": "Write a haiku",
        "stream": True,
    }

    response = client.post("/api/v1/completions", json=payload)

    assert response.status_code == 400
    assert response.is_json
    body = response.get_json()
    assert body == {
        "error": {
            "message": (
                "Streaming is not supported for API v1 completions. "
                "Use /api/v2/chat/completions for Server-Sent Events."
            ),
            "type": "invalid_request_error",
            "param": "stream",
        }
    }
