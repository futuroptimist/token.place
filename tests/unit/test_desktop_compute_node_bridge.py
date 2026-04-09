"""Integration-style tests for desktop compute-node bridge with mocked runtime."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'src-tauri'
    / 'python'
    / 'compute_node_bridge.py'
)
SPEC = importlib.util.spec_from_file_location('desktop_compute_node_bridge', MODULE_PATH)
compute_node_bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(compute_node_bridge)


class FakeRelayClient:
    relay_url = 'https://relay.test'

    def __init__(self):
        self.calls = []

    def _auth_headers(self):
        return {}


class FakeRuntime:
    def __init__(self):
        self.relay_client = FakeRelayClient()
        self.processed = []
        self.crypto_manager = SimpleNamespace(decrypt_message=lambda _payload: [{'role': 'user', 'content': 'hi'}])
        self.model_manager = SimpleNamespace(
            get_llm_instance=lambda: SimpleNamespace(
                create_chat_completion=lambda **_kwargs: iter([
                    {'choices': [{'delta': {'content': 'hello'}, 'finish_reason': None}]},
                    {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                ])
            ),
            config=SimpleNamespace(get=lambda _key, default=None: default),
        )

    def process_relay_request(self, payload):
        self.processed.append(payload)
        return True


class FakeResponse:
    def raise_for_status(self):
        return None


def test_process_sink_payload_uses_source_flow_for_non_streaming_requests(monkeypatch):
    runtime = FakeRuntime()
    bridge = compute_node_bridge.ComputeNodeBridge(
        runtime,
        stream_enabled=False,
        mode='auto',
        model_path='/tmp/model.gguf',
    )

    payload = {
        'client_public_key': 'client-key',
        'chat_history': 'cipher',
        'cipherkey': 'key',
        'iv': 'iv',
    }

    assert bridge.process_sink_payload(payload) is True
    assert runtime.processed == [payload]


def test_process_sink_payload_posts_stream_chunks_when_enabled(monkeypatch):
    runtime = FakeRuntime()
    bridge = compute_node_bridge.ComputeNodeBridge(
        runtime,
        stream_enabled=True,
        mode='auto',
        model_path='/tmp/model.gguf',
    )

    posted = []

    def fake_post(url, json=None, headers=None, timeout=10):
        posted.append((url, json, headers, timeout))
        return FakeResponse()

    def fake_encrypt_stream_chunk(plaintext, _public_key, *, session=None, **_kwargs):
        ciphertext_dict = {'ciphertext': plaintext, 'iv': b'iv-bytes'}
        encrypted_key = b'cipherkey' if session is None else None
        return ciphertext_dict, encrypted_key, object()

    monkeypatch.setattr(compute_node_bridge.requests, 'post', fake_post)
    monkeypatch.setattr(compute_node_bridge, 'encrypt_stream_chunk', fake_encrypt_stream_chunk)

    payload = {
        'client_public_key': 'Y2xpZW50LWtleQ==',
        'chat_history': 'cipher',
        'cipherkey': 'key',
        'iv': 'iv',
        'stream': True,
        'stream_session_id': 'sess-1',
    }

    assert bridge.process_sink_payload(payload) is True
    assert posted, 'expected /stream/source POST calls'
    assert posted[0][0].endswith('/stream/source')
    assert posted[-1][1]['final'] is True


def test_process_sink_payload_uses_batch_entries(monkeypatch):
    runtime = FakeRuntime()
    bridge = compute_node_bridge.ComputeNodeBridge(
        runtime,
        stream_enabled=False,
        mode='auto',
        model_path='/tmp/model.gguf',
    )

    payload = {
        'next_ping_in_x_seconds': 1,
        'batch': [
            {
                'client_public_key': 'a',
                'chat_history': 'b',
                'cipherkey': 'c',
                'iv': 'd',
            },
            {
                'client_public_key': 'e',
                'chat_history': 'f',
                'cipherkey': 'g',
                'iv': 'h',
            },
        ],
    }

    assert bridge.process_sink_payload(payload) is True
    assert len(runtime.processed) == 2
