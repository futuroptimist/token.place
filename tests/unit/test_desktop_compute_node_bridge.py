"""Unit tests for the desktop compute-node bridge."""

import importlib.util
import json
import queue
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


class FakeModelManager:
    def __init__(self):
        self.model_path = ''
        self.default_n_gpu_layers = -1


class FakeRelayClient:
    relay_url = 'https://token.place'


class FakeRelayClientRouting(FakeRelayClient):
    def __init__(self):
        self.endpoint_calls = []

    def process_client_request(self, payload):
        endpoint = '/source'
        if payload.get('stream') is True and payload.get('stream_session_id'):
            endpoint = '/stream/source'
        self.endpoint_calls.append((endpoint, payload))
        return True


class FakeRuntime:
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClient()
        self._responses = [
            {'next_ping_in_x_seconds': 0, 'error': 'temporary outage'},
            {
                'next_ping_in_x_seconds': 0,
                'client_public_key': 'abc',
                'chat_history': 'ciphertext',
                'cipherkey': 'key',
                'iv': 'iv',
            },
        ]
        self._processed = []

    def ensure_model_ready(self):
        return True

    def register_and_poll_once(self):
        if self._responses:
            return self._responses.pop(0)
        return {'next_ping_in_x_seconds': 0}

    def process_relay_request(self, payload):
        self._processed.append(payload)
        return True

    def stop(self):
        return None


class StreamingRuntime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        StreamingRuntime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClientRouting()
        self._responses = [
            {
                'next_ping_in_x_seconds': 0,
                'client_public_key': 'abc',
                'chat_history': 'ciphertext',
                'cipherkey': 'key',
                'iv': 'iv',
                'stream': True,
                'stream_session_id': 'session-123',
            },
        ]
        self._processed = []

    def process_relay_request(self, payload):
        self._processed.append(payload)
        return self.relay_client.process_client_request(payload)


def _install_fake_runtime_module(monkeypatch, runtime_cls=FakeRuntime):
    module = ModuleType('utils.compute_node_runtime')
    module.ComputeNodeRuntimeConfig = lambda relay_url, relay_port: SimpleNamespace(
        relay_url=relay_url,
        relay_port=relay_port,
    )
    module.ComputeNodeRuntime = runtime_cls
    module.is_legacy_relay_payload = (
        lambda payload: {"client_public_key", "chat_history", "cipherkey", "iv"}.issubset(payload)
    )
    module.resolve_relay_url = lambda relay_url: relay_url
    module.resolve_relay_port = lambda relay_port, _relay_url: relay_port
    module.format_relay_target = (
        lambda relay_url, relay_port: f"{relay_url}:{relay_port}" if relay_port else relay_url
    )
    monkeypatch.setitem(sys.modules, 'utils.compute_node_runtime', module)


def _reset_cancel_queue():
    compute_node_bridge._stdin_lines = queue.Queue()
    compute_node_bridge._stdin_reader_started = True


def test_run_emits_operator_status_events_and_processes_requests(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)

    stop_counter = {'count': 0}

    def fake_stop_requested():
        stop_counter['count'] += 1
        return stop_counter['count'] > 4

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    event_types = [event['type'] for event in events]
    assert event_types[0] == 'started'
    assert 'status' in event_types
    assert event_types[-1] == 'stopped'
    assert any(event.get('registered') is False for event in events if event['type'] == 'status')
    assert any(event.get('registered') is True for event in events if event['type'] == 'status')


def test_run_reports_model_initialization_failures(capsys, monkeypatch):
    _reset_cancel_queue()

    class FailingRuntime(FakeRuntime):
        def ensure_model_ready(self):
            return False

    _install_fake_runtime_module(monkeypatch, runtime_cls=FailingRuntime)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload['type'] == 'error'
    assert 'failed to initialize model runtime' in payload['message']


def test_run_streaming_payload_uses_shared_runtime_relay_client_path(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=StreamingRuntime)

    stop_counter = {'count': 0}

    def fake_stop_requested():
        stop_counter['count'] += 1
        return stop_counter['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)
    assert status == 0

    runtime = StreamingRuntime.last_instance
    assert runtime is not None
    assert len(runtime._processed) == 1
    assert runtime._processed[0]['stream'] is True
    assert runtime._processed[0]['stream_session_id'] == 'session-123'

    assert len(runtime.relay_client.endpoint_calls) == 1
    endpoint, payload = runtime.relay_client.endpoint_calls[0]
    assert endpoint == '/stream/source'
    assert payload['stream'] is True
    assert payload['stream_session_id'] == 'session-123'

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    status_events = [event for event in events if event['type'] == 'status']
    assert any(event.get('registered') is True for event in status_events)
