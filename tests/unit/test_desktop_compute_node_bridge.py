"""Unit tests for the desktop compute-node bridge."""

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest
import yaml
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
        self.requested_compute_mode = 'auto'
        self.last_compute_diagnostics = None


class FakeRelayClient:
    relay_url = 'https://token.place'

    def api_v1_registration_fresh(self, _relay_url=None):
        return True


class StaleRelayClient(FakeRelayClient):
    def api_v1_registration_fresh(self, _relay_url=None):
        return False


class FakeRelayClientRouting(FakeRelayClient):
    def __init__(self):
        self.endpoint_calls = []

    def process_client_request(self, payload):
        endpoint = '/source'
        if payload.get('stream') is True and payload.get('stream_session_id'):
            endpoint = '/stream/source'
        self.endpoint_calls.append((endpoint, payload))
        return True

    def process_api_v1_chat_request(self, payload):
        self.endpoint_calls.append(('/api/v1/relay/responses', payload))
        return True

    def submit_api_v1_error_response(self, payload, *, code, message):
        self.endpoint_calls.append((
            '/api/v1/relay/responses',
            {
                'request_id': payload.get('request_id'),
                'error': {'code': code, 'message': message},
            },
        ))
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

    def ensure_api_v1_runtime_ready(self):
        return self.ensure_model_ready()

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




class ApiV1Runtime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        ApiV1Runtime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClientRouting()
        self._responses = [
            {
                'protocol': 'tokenplace_api_v1_relay_e2ee',
                'version': 1,
                'request_id': 'req-1',
                'client_public_key': 'client-key',
                'chat_history': 'ciphertext',
                'cipherkey': 'key',
                'iv': 'iv',
                'next_ping_in_x_seconds': 0,
            },
        ]
        self._processed = []

    def process_relay_request(self, payload):
        self._processed.append(payload)
        return self.relay_client.process_api_v1_chat_request(payload)


class WarmingThenApiV1Runtime(ApiV1Runtime):
    last_instance = None

    def __init__(self, _config):
        super().__init__(_config)
        WarmingThenApiV1Runtime.last_instance = self
        self.ready_started = threading.Event()
        self.ready_release = threading.Event()

    def ensure_api_v1_runtime_ready(self):
        self.ready_started.set()
        self.ready_release.wait(timeout=1)
        return True


class WarmingTimeoutApiV1Runtime(ApiV1Runtime):
    last_instance = None

    def __init__(self, _config):
        super().__init__(_config)
        WarmingTimeoutApiV1Runtime.last_instance = self
        self.ready_started = threading.Event()

    def ensure_api_v1_runtime_ready(self):
        self.ready_started.set()
        time.sleep(0.2)
        return True


class MalformedWaitThenApiV1Runtime(ApiV1Runtime):
    last_instance = None

    def __init__(self, _config):
        MalformedWaitThenApiV1Runtime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClientRouting()
        self.relay_client._request_timeout = 2
        self._responses = [
            {'next_ping_in_x_seconds': 'not-a-number', 'error': None},
            {
                'protocol': 'tokenplace_api_v1_relay_e2ee',
                'version': 1,
                'request_id': 'req-after-bad-wait',
                'client_public_key': 'client-key',
                'chat_history': 'ciphertext',
                'cipherkey': 'key',
                'iv': 'iv',
                'next_ping_in_x_seconds': 0,
            },
        ]
        self._processed = []


class ProcessingFailureRuntime(FakeRuntime):
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClient()
        self._responses = [
            {
                'next_ping_in_x_seconds': 0,
                'client_public_key': 'abc',
                'chat_history': 'ciphertext',
                'cipherkey': 'key',
                'iv': 'iv',
            },
        ]
        self._processed = []

    def process_relay_request(self, payload):
        self._processed.append(payload)
        return False


class NullErrorHeartbeatRuntime(FakeRuntime):
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClient()
        self._responses = [
            {
                'next_ping_in_x_seconds': 0,
                'error': None,
            },
        ]
        self._processed = []


class IncompatibleRelayRuntime(FakeRuntime):
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClient()
        self._responses = [{'relay_version': 'outdated'}]
        self._processed = []


class FalseErrorHeartbeatRuntime(FakeRuntime):
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClient()
        self._responses = [
            {
                'next_ping_in_x_seconds': 0,
                'error': False,
            },
        ]
        self._processed = []


def _install_fake_runtime_module(monkeypatch, runtime_cls=FakeRuntime):
    module = ModuleType('utils.compute_node_runtime')

    supported_modes = {'auto', 'cpu', 'gpu', 'hybrid'}

    def _normalize_compute_mode(mode):
        normalized = {'cuda': 'gpu', 'metal': 'gpu'}.get(str(mode).lower(), str(mode).lower())
        return normalized if normalized in supported_modes else 'auto'

    def _apply_compute_mode(model_manager, mode):
        normalized = _normalize_compute_mode(mode)
        model_manager.requested_compute_mode = normalized
        if normalized == 'cpu':
            model_manager.default_n_gpu_layers = 0
        elif normalized == 'hybrid':
            model_manager.default_n_gpu_layers = getattr(model_manager, 'hybrid_n_gpu_layers', 24)
        else:
            model_manager.default_n_gpu_layers = -1
        model_manager.last_compute_diagnostics = {
            'requested_mode': normalized,
            'effective_mode': 'cpu' if normalized == 'cpu' else 'pending',
            'backend_available': 'unknown',
            'backend_selected': 'cpu' if normalized == 'cpu' else 'unknown',
            'backend_used': 'cpu' if normalized == 'cpu' else 'unknown',
            'n_gpu_layers': model_manager.default_n_gpu_layers,
            'offloaded_layers': model_manager.default_n_gpu_layers,
            'kv_cache_device': 'cpu' if normalized == 'cpu' else 'gpu',
            'fallback_reason': None,
        }
        return normalized

    def _compute_mode_diagnostics(model_manager):
        requested_mode = _normalize_compute_mode(
            getattr(model_manager, 'requested_compute_mode', 'auto')
        )
        runtime = getattr(model_manager, 'last_compute_diagnostics', None)
        if isinstance(runtime, dict) and runtime.get('requested_mode') == requested_mode:
            return dict(runtime)
        if requested_mode == 'cpu':
            return {
                'requested_mode': requested_mode,
                'effective_mode': 'cpu',
                'backend_available': 'unknown',
                'backend_selected': 'cpu',
                'backend_used': 'cpu',
                'n_gpu_layers': 0,
                'offloaded_layers': 0,
                'kv_cache_device': 'cpu',
                'fallback_reason': None,
            }
        return {
            'requested_mode': requested_mode,
            'effective_mode': 'pending',
            'backend_available': 'unknown',
            'backend_selected': 'unknown',
            'backend_used': 'unknown',
            'n_gpu_layers': model_manager.default_n_gpu_layers,
            'offloaded_layers': model_manager.default_n_gpu_layers,
            'kv_cache_device': 'gpu',
            'fallback_reason': None,
        }

    module.ComputeNodeRuntimeConfig = lambda relay_url, relay_port, **kwargs: SimpleNamespace(
        relay_url=relay_url,
        relay_port=relay_port,
        **kwargs,
    )
    module.ComputeNodeRuntime = runtime_cls
    module.is_api_v1_relay_payload = lambda payload: (
        isinstance(payload, dict)
        and payload.get('protocol') == 'tokenplace_api_v1_relay_e2ee'
        and payload.get('version') == 1
        and all(isinstance(payload.get(key), str) for key in (
            'request_id',
            'client_public_key',
            'chat_history',
            'cipherkey',
            'iv',
        ))
    )
    module.resolve_relay_url = lambda relay_url, **_kwargs: relay_url
    module.resolve_relay_port = lambda relay_port, _relay_url: relay_port
    module.SUPPORTED_COMPUTE_MODES = supported_modes
    module.normalize_compute_mode = _normalize_compute_mode
    module.apply_compute_mode = _apply_compute_mode
    module.compute_mode_diagnostics = _compute_mode_diagnostics
    monkeypatch.setitem(sys.modules, 'utils.compute_node_runtime', module)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'cpu',
            'runtime_action': 'skipped',
            'interpreter': sys.executable,
            'llama_module_path': 'missing',
            'fallback_reason': '',
        },
    )
    monkeypatch.setattr(
        compute_node_bridge,
        'maybe_reexec_for_runtime_refresh',
        lambda _setup, *, allow_reexec=True: None,
    )


def _reset_cancel_queue():
    compute_node_bridge._stdin_lines = queue.Queue()
    compute_node_bridge._stdin_reader_started = True
    compute_node_bridge._stop_requested_latched.clear()


def _load_desktop_operator_parity_matrix():
    matrix_path = Path(__file__).resolve().parents[1] / 'fixtures' / 'desktop_operator_parity_matrix.json'
    return json.loads(matrix_path.read_text(encoding='utf-8'))


def test_macos_packaged_operator_lifecycle_parity_uses_shared_entrypoint():
    matrix = _load_desktop_operator_parity_matrix()
    assert all(
        item.get('id') != 'macos_packaged_operator_lifecycle_parity'
        for item in matrix.get('known_gaps', [])
    )

    coverage = next(
        item
        for item in matrix.get('lifecycle_coverage', [])
        if item.get('id') == 'macos_packaged_operator_lifecycle_parity'
    )
    assert coverage['platform'] == 'darwin'
    assert coverage['status'] == 'covered_by_shared_entrypoint'
    assert (
        coverage['shared_entrypoint']
        == 'desktop-tauri/scripts/run_desktop_parity_checks.py'
    )
    assert coverage['covered_checks'] == [
        'packaged_resource_resolution',
        'dependency_isolation',
        'warm_load',
        'register',
        'multi_turn_api_v1_relay_chat',
        'stop',
        'start_after_stop',
        'diagnostics',
    ]

    workflow_path = (
        Path(__file__).resolve().parents[2]
        / '.github'
        / 'workflows'
        / 'desktop-operator-e2e.yml'
    )
    workflow = yaml.load(
        workflow_path.read_text(encoding='utf-8'),
        Loader=yaml.BaseLoader,
    )
    macos_job = workflow['jobs']['desktop-operator-packaged-e2e-macos']
    macos_run_commands = [
        step.get('run', '')
        for step in macos_job['steps']
        if isinstance(step, dict)
    ]
    runner_path = Path(__file__).resolve().parents[2] / coverage['shared_entrypoint']
    runner = runner_path.read_text(encoding='utf-8')

    assert macos_job['runs-on'] == 'macos-latest'
    assert f"python {coverage['shared_entrypoint']}" in macos_run_commands
    assert any(
        'test_desktop_no_relay_autostart_e2e.py' in command
        for command in macos_run_commands
    )
    for script in coverage['required_scripts']:
        assert Path(script).name in runner


class RestartTrackingRuntime(FakeRuntime):
    instances = []

    def __init__(self, _config):
        super().__init__(_config)
        self.register_attempts = 0
        self.stopped = False
        self.relay_session_starts = 0
        RestartTrackingRuntime.instances.append(self)

    def start_relay_session(self):
        self.relay_session_starts += 1

    def register_and_poll_once(self):
        self.register_attempts += 1
        return {'next_ping_in_x_seconds': 0, 'error': None}

    def stop(self):
        self.stopped = True


def test_run_start_stop_start_resets_stale_cancel_and_registers_again(capsys, monkeypatch):
    _reset_cancel_queue()
    RestartTrackingRuntime.instances = []
    _install_fake_runtime_module(monkeypatch, runtime_cls=RestartTrackingRuntime)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    first_stop_counter = {'count': 0}

    def first_stop_requested():
        first_stop_counter['count'] += 1
        return first_stop_counter['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', first_stop_requested)
    assert compute_node_bridge.run(args) == 0
    first_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first_events[-1]['type'] == 'stopped'
    assert RestartTrackingRuntime.instances[0].relay_session_starts == 1
    assert RestartTrackingRuntime.instances[0].register_attempts >= 1
    assert RestartTrackingRuntime.instances[0].stopped is True

    compute_node_bridge._stop_requested_latched.set()
    compute_node_bridge._stdin_lines.put('{"type":"cancel"}')
    second_stop_counter = {'count': 0}

    def second_stop_requested():
        second_stop_counter['count'] += 1
        return second_stop_counter['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', second_stop_requested)
    assert compute_node_bridge.run(args) == 0
    second_output = capsys.readouterr()
    second_events = [json.loads(line) for line in second_output.out.splitlines()]

    assert len(RestartTrackingRuntime.instances) == 2
    assert RestartTrackingRuntime.instances[1].relay_session_starts == 1
    assert RestartTrackingRuntime.instances[1].register_attempts >= 1
    assert second_events[0]['type'] == 'started'
    assert any(event['type'] == 'status' and event['registered'] is True for event in second_events)
    assert second_events[-1]['type'] == 'stopped'
    assert 'stale_cancel_messages_drained=1' in second_output.err


def test_run_restart_falls_back_to_relay_client_start(capsys, monkeypatch):
    _reset_cancel_queue()
    start_calls = []

    class StartableRelayClient(FakeRelayClient):
        def start(self):
            start_calls.append('start')

    class RuntimeWithoutSessionHook(FakeRuntime):
        def __init__(self, _config):
            super().__init__(_config)
            self.relay_client = StartableRelayClient()

        def register_and_poll_once(self):
            return {'next_ping_in_x_seconds': 0, 'error': None}

    _install_fake_runtime_module(monkeypatch, runtime_cls=RuntimeWithoutSessionHook)
    stop_counter = {'count': 0}

    def stop_after_first_poll():
        stop_counter['count'] += 1
        return stop_counter['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_first_poll)
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    assert compute_node_bridge.run(args) == 0

    assert start_calls == ['start']
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.relay_client.reset' in output.err


def test_run_emits_operator_status_events_and_heartbeat_registration(capsys, monkeypatch):
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
    started = events[0]
    assert started['offloaded_layers'] == 0
    assert started['kv_cache_device'] == 'cpu'
    required_status_fields = {
        'running',
        'registered',
        'relay_runtime_state',
        'runtime_path',
        'relay_runtime_path',
        'active_relay_url',
        'requested_mode',
        'effective_mode',
        'backend_available',
        'backend_selected',
        'backend_used',
        'fallback_reason',
        'model_path',
        'last_error',
        'operator_session_id',
        'sequence',
        'updated_at_ms',
    }
    for event in events:
        if event['type'] in {'started', 'status', 'stopped'}:
            assert required_status_fields <= set(event)
    warming_event = next(event for event in events if event.get('relay_runtime_state') == 'warming')
    assert warming_event['registered'] is False
    assert warming_event['effective_mode'] == 'pending'
    assert any(event.get('registered') is False for event in events if event['type'] == 'status')
    assert any(event.get('registered') is True for event in events if event['type'] == 'status')
    stopped = events[-1]
    assert stopped['running'] is False
    assert stopped['registered'] is False
    assert stopped['relay_runtime_state'] == 'stopped'


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
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    payload = next(event for event in events if event.get("type") == "error")
    assert payload['type'] == 'error'
    assert 'failed to initialize API v1 model runtime' in payload['message']


def test_run_allows_runtime_reexec_when_cuda_runtime_is_repaired(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    reexec_flags = []
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {'runtime_action': 'installed_cuda_reexec'},
    )
    monkeypatch.setattr(
        compute_node_bridge,
        'maybe_reexec_for_runtime_refresh',
        lambda _setup, *, allow_reexec=True: reexec_flags.append(allow_reexec),
    )
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    assert reexec_flags == [True]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'stopped'


def test_run_continues_when_runtime_setup_reports_missing_packaged_requirements(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'cpu',
            'runtime_action': 'failed',
            'fallback_reason': (
                'requirements file not found at '
                'C:/Users/testuser/AppData/Local/token.place/requirements.txt; '
                'skipping pinned CUDA source reinstall'
            ),
            'interpreter': sys.executable,
            'llama_module_path': 'missing',
        },
    )
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)
    monkeypatch.setattr(sys.modules['desktop_runtime_setup'].sys, 'platform', 'linux')

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'stopped'
    assert '[Errno 2]' not in json.dumps(events)


def test_run_probe_only_runtime_setup_does_not_trigger_repair_reexec(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    reexec_calls = []
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'cpu',
            'runtime_action': 'probe_only',
            'fallback_reason': 'startup default is probe-only without bootstrap opt-in',
            'interpreter': sys.executable,
            'llama_module_path': 'missing',
        },
    )
    monkeypatch.setattr(
        compute_node_bridge,
        'maybe_reexec_for_runtime_refresh',
        lambda setup, *, allow_reexec=True: reexec_calls.append(
            (setup.get('runtime_action'), allow_reexec)
        ),
    )
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'stopped'
    assert reexec_calls == [('probe_only', True)]


def test_run_windows_gpu_mode_emits_error_when_runtime_bootstrap_fails(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'cpu',
            'runtime_action': 'failed',
            'fallback_reason': 'cuda wheel install failed',
            'interpreter': sys.executable,
            'llama_module_path': 'missing',
        },
    )
    monkeypatch.setattr(sys.modules['desktop_runtime_setup'].sys, 'platform', 'win32')

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 1
    payload = events[0]
    expected_message = (
        'GPU provisioning failed for desktop Windows launch '
        '(mode=auto, action=failed): cuda wheel install failed. '
        'Verify CUDA runtime prerequisites and llama-cpp-python CUDA build support.'
    )
    assert payload['type'] == 'error'
    assert payload['message'] == expected_message
    assert payload['last_error'] == expected_message
    assert payload['running'] is False
    assert payload['registered'] is False
    assert payload['relay_runtime_state'] == 'failed'
    assert payload['operator_session_id']
    assert payload['sequence'] == 1
    assert isinstance(payload['updated_at_ms'], int)


def test_run_windows_gpu_mode_emits_error_when_runtime_is_shadowed(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'cpu',
            'runtime_action': 'shadowed_repo_llama_cpp',
            'fallback_reason': 'llama_cpp import shadowed by repo-local shim',
            'interpreter': sys.executable,
            'llama_module_path': 'repo/llama_cpp.py',
        },
    )
    monkeypatch.setattr(sys.modules['desktop_runtime_setup'].sys, 'platform', 'win32')

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 1
    payload = events[0]
    expected_message = (
        'GPU provisioning failed for desktop Windows launch '
        '(mode=auto, action=shadowed_repo_llama_cpp): '
        'llama_cpp import shadowed by repo-local shim. '
        'Verify CUDA runtime prerequisites and llama-cpp-python CUDA build support.'
    )
    assert payload['type'] == 'error'
    assert payload['message'] == expected_message
    assert payload['last_error'] == expected_message
    assert payload['running'] is False
    assert payload['registered'] is False
    assert payload['relay_runtime_state'] == 'failed'
    assert payload['operator_session_id']
    assert payload['sequence'] == 1
    assert isinstance(payload['updated_at_ms'], int)


def test_run_windows_gpu_mode_accepts_bootstrap_enabled_cuda_runtime(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cuda',
            'detected_device': 'cuda:0',
            'runtime_action': 'installed',
            'fallback_reason': '',
            'interpreter': sys.executable,
            'llama_module_path': 'site-packages/llama_cpp',
        },
    )
    monkeypatch.setattr(sys.modules['desktop_runtime_setup'].sys, 'platform', 'win32')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'stopped'


def test_run_windows_gpu_mode_allows_probe_only_when_bootstrap_is_disabled(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'cpu',
            'runtime_action': 'probe_only',
            'fallback_reason': 'bootstrap disabled by TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP',
            'interpreter': sys.executable,
            'llama_module_path': 'missing',
        },
    )
    monkeypatch.setattr(sys.modules['desktop_runtime_setup'].sys, 'platform', 'win32')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP', '1')

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'stopped'


def test_run_ignores_legacy_shaped_ciphertext_payload(capsys, monkeypatch):
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
    assert runtime._processed == []
    assert runtime.relay_client.endpoint_calls == []

    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines()]
    status_events = [event for event in events if event['type'] == 'status']
    assert any(event.get('registered') is True for event in status_events)
    assert 'legacy_payload' not in output.err


def test_run_api_v1_payload_uses_relay_api_v1_response_endpoint(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=ApiV1Runtime)

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

    runtime = ApiV1Runtime.last_instance
    assert runtime is not None
    assert len(runtime._processed) == 1
    assert runtime._processed[0]['request_id'] == 'req-1'

    assert len(runtime.relay_client.endpoint_calls) == 1
    endpoint, payload = runtime.relay_client.endpoint_calls[0]
    assert endpoint == '/api/v1/relay/responses'
    assert payload['request_id'] == 'req-1'

    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines()]
    status_events = [event for event in events if event['type'] == 'status']
    assert any(event.get('registered') is True for event in status_events)
    assert 'api_v1_payload=True' in output.err
    assert 'desktop.compute_node_bridge.api_v1_e2ee.work_received' in output.err
    assert 'desktop.compute_node_bridge.api_v1_e2ee.response_submitted' in output.err
    assert "ModuleNotFoundError: No module named 'api'" not in output.err


def test_run_api_v1_payload_polls_immediately_after_work_even_with_long_lease(capsys, monkeypatch):
    _reset_cancel_queue()

    class LongLeaseApiV1Runtime(ApiV1Runtime):
        last_instance = None

        def __init__(self, config):
            super().__init__(config)
            LongLeaseApiV1Runtime.last_instance = self
            self._responses = [
                {
                    'protocol': 'tokenplace_api_v1_relay_e2ee',
                    'version': 1,
                    'request_id': 'req-long-lease',
                    'client_public_key': 'client-key',
                    'chat_history': 'ciphertext',
                    'cipherkey': 'key',
                    'iv': 'iv',
                    'next_ping_in_x_seconds': 30,
                },
            ]

    _install_fake_runtime_module(monkeypatch, runtime_cls=LongLeaseApiV1Runtime)

    sleep_values = []

    def fake_sleep_with_cancel(seconds):
        sleep_values.append(seconds)
        return True

    monkeypatch.setattr(compute_node_bridge, '_sleep_with_cancel', fake_sleep_with_cancel)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: False)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    assert sleep_values == [0.0]
    runtime = LongLeaseApiV1Runtime.last_instance
    assert runtime is not None
    assert [payload['request_id'] for payload in runtime._processed] == ['req-long-lease']
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.api_v1_e2ee.work_processed_next_poll_immediate' in output.err
    assert 'request_id=req-long-lease' in output.err


def test_run_api_v1_payload_waits_boundedly_then_processes(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=WarmingThenApiV1Runtime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS', '0.5')

    stop_counter = {'count': 0}

    def fake_stop_requested():
        stop_counter['count'] += 1
        runtime = WarmingThenApiV1Runtime.last_instance
        assert runtime is not None
        assert runtime.ready_started.wait(timeout=0.5)
        runtime.ready_release.set()
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

    runtime = WarmingThenApiV1Runtime.last_instance
    assert runtime is not None
    assert [payload['request_id'] for payload in runtime._processed] == ['req-1']
    assert runtime.relay_client.endpoint_calls[0][0] == '/api/v1/relay/responses'

    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.registration.gate_wait_start' in output.err
    assert 'desktop.compute_node_bridge.registration.gate_wait_done' in output.err
    assert 'desktop.compute_node_bridge.api_v1_e2ee.runtime_wait.start' not in output.err
    assert 'desktop.compute_node_bridge.api_v1_e2ee.runtime_wait.timeout' not in output.err
    assert 'desktop.compute_node_bridge.process_request relay=https://token.place request_id=req-1' in output.err
    assert 'desktop.compute_node_bridge.api_v1_e2ee.response_submitted' in output.err


def test_run_slow_pre_registration_warm_load_processes_without_runtime_not_ready_error(
    capsys, monkeypatch
):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=WarmingTimeoutApiV1Runtime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS', '0.5')
    monkeypatch.setattr(compute_node_bridge, 'PRE_REGISTRATION_PROGRESS_INTERVAL_SECONDS', 0.01)

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

    runtime = WarmingTimeoutApiV1Runtime.last_instance
    assert runtime is not None
    assert runtime.ready_started.is_set()
    assert [payload['request_id'] for payload in runtime._processed] == ['req-1']
    assert runtime.relay_client.endpoint_calls[0][0] == '/api/v1/relay/responses'
    assert runtime.relay_client.endpoint_calls[0][1]['request_id'] == 'req-1'

    output = capsys.readouterr()
    assert 'api_v1_payload=True' in output.err
    assert 'desktop.compute_node_bridge.registration.gate_wait_start' in output.err
    assert 'desktop.compute_node_bridge.model_init.still_warming' in output.err
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    warming_status_events = [
        event
        for event in events
        if event.get('type') == 'status' and event.get('warm_load_state') == 'warming'
    ]
    assert len(warming_status_events) >= 2
    assert 'desktop.compute_node_bridge.api_v1_e2ee.error_response.submitted' not in output.err
    assert 'compute_node_runtime_not_ready' not in output.err


def test_run_malformed_wait_value_does_not_stop_future_api_v1_polling(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=MalformedWaitThenApiV1Runtime)

    sleep_values = []

    def fake_sleep_with_cancel(seconds):
        sleep_values.append(seconds)
        return False

    stop_counter = {'count': 0}

    def fake_stop_requested():
        stop_counter['count'] += 1
        return stop_counter['count'] > 3

    monkeypatch.setattr(compute_node_bridge, '_sleep_with_cancel', fake_sleep_with_cancel)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)
    assert status == 0

    runtime = MalformedWaitThenApiV1Runtime.last_instance
    assert runtime is not None
    assert [payload['request_id'] for payload in runtime._processed] == ['req-after-bad-wait']
    assert sleep_values[:2] == [2.0, 0.0]

    output = capsys.readouterr()
    assert 'wait=2.0' in output.err
    assert 'request_id=req-after-bad-wait' in output.err


def test_run_treats_null_error_heartbeat_as_registered(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=NullErrorHeartbeatRuntime)
    call_count = {'n': 0}

    def fake_stop_requested():
        call_count['n'] += 1
        return call_count['n'] > 1

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines()]
    status_events = [event for event in events if event['type'] == 'status']
    assert status_events
    assert any(event['registered'] is False and event.get('warm_load_state') == 'warming' for event in status_events)
    assert any(event['registered'] is True and event['last_error'] is None for event in status_events)
    stderr = output.err
    assert 'desktop.compute_node_bridge.start' in stderr
    assert 'desktop.compute_node_bridge.relay_target.resolved' in stderr
    assert 'desktop.compute_node_bridge.model_init.start' in stderr
    assert 'desktop.compute_node_bridge.model_init.ready' in stderr
    assert 'desktop.compute_node_bridge.runtime_state' in stderr
    assert 'desktop.compute_node_bridge.relay_poll' in stderr
    assert 'desktop.compute_node_bridge.stop' in stderr


def test_run_treats_false_error_heartbeat_as_registered(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=FalseErrorHeartbeatRuntime)
    call_count = {'n': 0}

    def fake_stop_requested():
        call_count['n'] += 1
        return call_count['n'] > 1

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
    status_events = [event for event in events if event['type'] == 'status']
    assert status_events
    assert any(event['registered'] is False and event.get('warm_load_state') == 'warming' for event in status_events)
    assert any(event['registered'] is True and event['last_error'] is None for event in status_events)


def test_run_reports_actionable_error_for_incompatible_relay(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=IncompatibleRelayRuntime)
    call_count = {'n': 0}

    def fake_stop_requested():
        call_count['n'] += 1
        return call_count['n'] > 1

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
    status_events = [event for event in events if event['type'] == 'status']
    assert status_events
    actionable_errors = [
        event
        for event in status_events
        if isinstance(event.get('last_error'), str)
        and 'unreachable, old, or incompatible' in event['last_error']
    ]
    assert actionable_errors
    assert actionable_errors[0]['registered'] is False
    assert 'update relay.py to repo HEAD' in actionable_errors[0]['last_error']


def test_run_ignores_legacy_shaped_processing_failure(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=ProcessingFailureRuntime)
    call_count = {'n': 0}

    def fake_stop_requested():
        call_count['n'] += 1
        return call_count['n'] > 1

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
    status_events = [event for event in events if event['type'] == 'status']
    assert status_events
    assert any(event['registered'] is False and event.get('warm_load_state') == 'warming' for event in status_events)
    assert any(event['registered'] is True and event['last_error'] is None for event in status_events)


def test_apply_compute_mode_supports_gpu_and_cpu_modes(monkeypatch):
    _install_fake_runtime_module(monkeypatch)
    manager = FakeModelManager()
    from utils.compute_node_runtime import apply_compute_mode

    assert apply_compute_mode(manager, 'auto') == 'auto'
    assert manager.default_n_gpu_layers == -1

    assert apply_compute_mode(manager, 'gpu') == 'gpu'
    assert manager.default_n_gpu_layers == -1

    assert apply_compute_mode(manager, 'cpu') == 'cpu'
    assert manager.default_n_gpu_layers == 0

    manager.hybrid_n_gpu_layers = 9
    assert apply_compute_mode(manager, 'hybrid') == 'hybrid'
    assert manager.default_n_gpu_layers == 9


def test_run_normalizes_unknown_mode_to_auto_in_status(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='UNSUPPORTED',
        relay_url='https://token.place',
        relay_port=None,
    )
    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[0]['requested_mode'] == 'auto'


def test_run_prefers_explicit_desktop_relay_url_and_disables_configured_fallbacks(monkeypatch):
    _reset_cancel_queue()
    captured = {}

    class CapturingRuntime:
        def __init__(self, config):
            captured['config'] = config
            self.model_manager = FakeModelManager()
            self.relay_client = SimpleNamespace(relay_url=config.relay_url)

        def ensure_model_ready(self):
            return True

        def ensure_api_v1_runtime_ready(self):
            return self.ensure_model_ready()

        def register_and_poll_once(self):
            return {'next_ping_in_x_seconds': 0}

        def process_relay_request(self, _payload):
            return True

        def stop(self):
            return None

    module = ModuleType('utils.compute_node_runtime')
    module.ComputeNodeRuntimeConfig = lambda relay_url, relay_port, **kwargs: SimpleNamespace(
        relay_url=relay_url,
        relay_port=relay_port,
        **kwargs,
    )
    module.ComputeNodeRuntime = CapturingRuntime
    module.is_api_v1_relay_payload = lambda _payload: False
    module.resolve_relay_port = lambda relay_port, _relay_url: relay_port
    module.normalize_compute_mode = lambda mode: mode
    module.apply_compute_mode = lambda _model_manager, mode: mode
    module.SUPPORTED_COMPUTE_MODES = {'auto', 'cpu', 'cuda', 'metal'}

    def _resolve_relay_url(relay_url, **kwargs):
        prefer_cli = bool(kwargs.get('prefer_cli', False))
        env_override = os.environ.get('TOKENPLACE_RELAY_URL')
        return relay_url if prefer_cli else (env_override or relay_url)

    module.resolve_relay_url = _resolve_relay_url
    module.compute_mode_diagnostics = lambda _model_manager: {}
    monkeypatch.setitem(sys.modules, 'utils.compute_node_runtime', module)
    monkeypatch.setenv('TOKENPLACE_RELAY_URL', 'https://token.place')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='http://127.0.0.1:5010',
        relay_port=None,
    )

    status = compute_node_bridge.run(args)
    assert status == 0
    assert captured['config'].relay_url == 'http://127.0.0.1:5010'
    assert captured['config'].use_configured_relay_fallbacks is False


def test_main_emits_structured_error_when_compute_runtime_missing(capsys, monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'utils.compute_node_runtime':
            raise ModuleNotFoundError("No module named 'utils.compute_node_runtime'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'compute_node_bridge.py',
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'auto',
        ],
    )

    status = compute_node_bridge.main()

    assert status == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    payload = next(event for event in events if event.get("type") == "error")
    assert payload['type'] == 'error'
    assert payload['message'] == "runtime unavailable: No module named 'utils.compute_node_runtime'"
    assert payload['running'] is False
    assert payload['registered'] is False
    assert payload['relay_runtime_state'] == 'failed'
    assert payload['last_error'] == payload['message']
    assert payload['operator_session_id']
    assert payload['sequence'] == 1
    assert isinstance(payload['updated_at_ms'], int)


def test_main_normalizes_mode_before_run(monkeypatch):
    captured = {}

    def fake_run(args):
        captured['mode'] = args.mode
        return 0

    monkeypatch.setattr(compute_node_bridge, 'run', fake_run)
    monkeypatch.setattr(
        sys,
        'argv',
        ['compute_node_bridge.py', '--model', '/tmp/model.gguf', '--mode', 'CUDA'],
    )

    status = compute_node_bridge.main()
    assert status == 0
    assert captured['mode'] == 'gpu'

    monkeypatch.setattr(
        sys,
        'argv',
        ['compute_node_bridge.py', '--model', '/tmp/model.gguf', '--mode', ' CUDA '],
    )

    status = compute_node_bridge.main()
    assert status == 0
    assert captured['mode'] == 'gpu'

    monkeypatch.setattr(
        sys,
        'argv',
        ['compute_node_bridge.py', '--model', '/tmp/model.gguf', '--mode', ' cpu '],
    )

    status = compute_node_bridge.main()
    assert status == 0
    assert captured['mode'] == 'cpu'

    monkeypatch.setattr(
        sys,
        'argv',
        ['compute_node_bridge.py', '--model', '/tmp/model.gguf', '--mode', 'unsupported'],
    )

    status = compute_node_bridge.main()
    assert status == 0
    assert captured['mode'] == 'auto'


def test_main_emits_structured_error_when_last_resort_exception_path_runs(capsys, monkeypatch):
    def fake_run(_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(compute_node_bridge, 'run', fake_run)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'compute_node_bridge.py',
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'CUDA',
            '--relay-url',
            'https://relay.example',
        ],
    )
    monkeypatch.setenv('TOKENPLACE_COMPUTE_NODE_SESSION_ID', 'fallback-session')

    status = compute_node_bridge.main()

    assert status == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    payload = next(event for event in events if event.get("type") == "error")
    assert payload["running"] is False
    assert payload["registered"] is False
    assert payload["relay_runtime_state"] == "failed"
    assert payload["active_relay_url"] == "https://relay.example"
    assert payload["requested_mode"] == "gpu"
    assert payload["effective_mode"] == "pending"
    assert payload["backend_available"] == "pending"
    assert payload["backend_selected"] == "pending"
    assert payload["backend_used"] == "pending"
    assert payload["model_path"] == "/tmp/model.gguf"
    assert payload["last_error"] == payload["message"]
    assert "compute-node bridge exited before emitting a startup event: boom" == payload["message"]
    assert payload["warm_load_state"] == "failed"
    assert "warm_load_enabled" in payload
    assert payload["runtime_path"] in {"bridge", "sidecar"}
    assert payload["relay_runtime_path"] == "bridge"
    assert payload["operator_session_id"] == "fallback-session"
    assert payload["sequence"] == 1
    assert isinstance(payload["updated_at_ms"], int)


def test_main_does_not_import_compute_runtime_for_mode_normalization(monkeypatch):
    monkeypatch.setattr(compute_node_bridge, 'run', lambda _args: 0)
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'utils.compute_node_runtime':
            raise AssertionError('main() should not import compute runtime before run()')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    monkeypatch.setattr(
        sys,
        'argv',
        ['compute_node_bridge.py', '--model', '/tmp/model.gguf', '--mode', 'CUDA'],
    )

    assert compute_node_bridge.main() == 0


def test_main_subprocess_succeeds_for_packaged_layout_without_pythonpath(tmp_path):
    python_dir = tmp_path / 'bin' / 'resources' / 'python'
    import_root = tmp_path / 'bin' / 'resources' / '_up_' / '_up_'
    utils_dir = import_root / 'utils'
    python_dir.mkdir(parents=True)
    utils_dir.mkdir(parents=True)

    (python_dir / 'compute_node_bridge.py').write_text(
        MODULE_PATH.read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    path_bootstrap_path = MODULE_PATH.parent / 'path_bootstrap.py'
    (python_dir / 'path_bootstrap.py').write_text(
        path_bootstrap_path.read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (python_dir / 'desktop_runtime_setup.py').write_text(
        """
def desktop_gpu_runtime_failure_message(_mode, _runtime_setup):
    return None


def ensure_desktop_llama_runtime(_mode):
    return {"selected_backend": "cpu", "detected_device": "cpu", "runtime_action": "skipped"}


def ensure_desktop_python_dependencies(*, repo_root=None):
    return {"ok": "true", "action": "already_satisfied", "missing": ""}


def maybe_reexec_for_runtime_refresh(_runtime_setup, *, allow_reexec=True):
    return None
""".strip()
        + "\n",
        encoding='utf-8',
    )
    (utils_dir / '__init__.py').write_text('', encoding='utf-8')
    (utils_dir / 'compute_node_runtime.py').write_text(
        """
SUPPORTED_COMPUTE_MODES = {"auto", "cpu", "gpu", "hybrid"}


def normalize_compute_mode(mode):
    mode = str(mode).lower()
    mode = {"cuda": "gpu", "metal": "gpu"}.get(mode, mode)
    return mode if mode in SUPPORTED_COMPUTE_MODES else "auto"


def apply_compute_mode(_model_manager, mode):
    return normalize_compute_mode(mode)


def compute_mode_diagnostics(_model_manager):
    return {
        "requested_mode": "auto",
        "effective_mode": "pending",
        "backend_available": "unknown",
        "backend_selected": "unknown",
        "backend_used": "unknown",
        "n_gpu_layers": -1,
        "fallback_reason": None,
    }


def resolve_relay_url(relay_url, **_kwargs):
    return relay_url


def resolve_relay_port(relay_port, _relay_url):
    return relay_port



def is_api_v1_relay_payload(_payload):
    return False


class ComputeNodeRuntimeConfig:
    def __init__(self, relay_url, relay_port, **_kwargs):
        self.relay_url = relay_url
        self.relay_port = relay_port


class _RelayClient:
    def __init__(self, relay_url):
        self.relay_url = relay_url


class _ModelManager:
    model_path = ""


class ComputeNodeRuntime:
    def __init__(self, config):
        self.relay_client = _RelayClient(config.relay_url)
        self.model_manager = _ModelManager()

    def ensure_model_ready(self):
        return True

    def ensure_api_v1_runtime_ready(self):
        return self.ensure_model_ready()

    def register_and_poll_once(self):
        return {"next_ping_in_x_seconds": 1}

    def process_relay_request(self, _payload):
        return True

    def stop(self):
        return None
""".strip()
        + "\n",
        encoding='utf-8',
    )

    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(python_dir / 'compute_node_bridge.py'),
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'auto',
            '--relay-url',
            'https://token.place',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc.stdin.write('{"type":"cancel"}\n')
    proc.stdin.flush()
    proc.stdin.close()
    proc.wait(timeout=10)
    stdout = proc.stdout.read()
    stderr = proc.stderr.read()

    assert proc.returncode == 0, stderr
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert any(event.get('type') == 'started' for event in events)
    assert any(event.get('type') == 'stopped' for event in events)
    assert "No module named 'utils'" not in stdout


def test_module_level_fallback_when_desktop_runtime_setup_is_missing(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'desktop_runtime_setup':
            raise ModuleNotFoundError("No module named 'desktop_runtime_setup'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)

    module = ModuleType('compute_node_bridge_no_runtime_setup')
    module.__file__ = str(MODULE_PATH)
    code = compile(MODULE_PATH.read_text(encoding='utf-8'), str(MODULE_PATH), 'exec')
    exec(code, module.__dict__)

    setup = module.ensure_desktop_llama_runtime('auto')
    assert setup['runtime_action'] == 'unavailable'
    assert 'module missing' in setup['fallback_reason']
    assert module.maybe_reexec_for_runtime_refresh(setup, allow_reexec=False) is None


def test_module_level_fallback_when_model_manager_is_missing(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'utils.llm'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)

    module = ModuleType('compute_node_bridge_no_model_manager')
    module.__file__ = str(MODULE_PATH)
    code = compile(MODULE_PATH.read_text(encoding='utf-8'), str(MODULE_PATH), 'exec')
    exec(code, module.__dict__)

    assert module._is_repo_llama_cpp_shim('/tmp/llama_cpp.py') is False


def test_sanitize_relay_target_redacts_credentials_query_and_fragment():
    sanitized = compute_node_bridge._sanitize_relay_target(
        'https://user:pass@token.place:8443/sink?token=abc#debug'
    )
    assert sanitized == 'https://token.place:8443'


def test_sanitize_relay_target_returns_unknown_for_invalid_values():
    assert compute_node_bridge._sanitize_relay_target(None) == 'unknown'
    assert compute_node_bridge._sanitize_relay_target('not-a-valid-url') == 'unknown'
    assert compute_node_bridge._sanitize_relay_target('https://token.place:bad') == 'unknown'
    assert compute_node_bridge._sanitize_relay_target('http://[::1') == 'unknown'


def test_sanitize_relay_target_preserves_ipv6_brackets():
    sanitized = compute_node_bridge._sanitize_relay_target('http://[::1]:8000/path?token=abc')

    assert sanitized == 'http://[::1]:8000'


def test_relay_key_fingerprint_uses_safe_helper_and_fallbacks():
    class CryptoManager:
        public_key_b64 = 'abcdefghijklmno'

    class RelayClient:
        crypto_manager = CryptoManager()

        def _api_v1_public_key_fingerprint(self, public_key):
            assert public_key == 'abcdefghijklmno'
            return 'fp:abcd'

    assert compute_node_bridge._relay_key_fingerprint(RelayClient()) == 'fp:abcd'

    class BrokenFingerprintRelay(RelayClient):
        def _api_v1_public_key_fingerprint(self, _public_key):
            raise RuntimeError('fingerprint failed')

    assert compute_node_bridge._relay_key_fingerprint(BrokenFingerprintRelay()) == 'unknown'

    class NoHelperRelay:
        crypto_manager = CryptoManager()

    assert compute_node_bridge._relay_key_fingerprint(NoHelperRelay()) == 'abcdefgh...lmno'

    class ShortKeyRelay:
        crypto_manager = SimpleNamespace(public_key_b64='short')

    assert compute_node_bridge._relay_key_fingerprint(ShortKeyRelay()) == 'unknown'


def test_relay_response_summary_handles_non_dict_payloads():
    summary = compute_node_bridge._relay_response_summary(["unexpected"])
    assert summary == "non-dict response type=list"


def test_relay_error_message_normalizes_non_string_truthy_values():
    assert compute_node_bridge._relay_error_message({"error": 503}) == "503"


def test_run_reports_stale_registration_status_after_missed_heartbeat(capsys, monkeypatch):
    _reset_cancel_queue()

    class StaleHeartbeatRuntime(FakeRuntime):
        def __init__(self, _config):
            self.model_manager = FakeModelManager()
            self.relay_client = StaleRelayClient()
            self._processed = []

        def register_and_poll_once(self):
            return {"next_ping_in_x_seconds": 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=StaleHeartbeatRuntime)
    stop_calls = {"count": 0}

    def fake_stop_requested():
        stop_calls["count"] += 1
        return stop_calls["count"] > 1

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    status_events = [event for event in events if event.get("type") == "status"]
    assert status_events
    assert all(event["registered"] is False for event in status_events)
    assert any(
        "relay appears unreachable" in (event.get("last_error") or "")
        for event in status_events
    )


def test_run_warms_runtime_after_first_successful_registration(capsys, monkeypatch):
    _reset_cancel_queue()
    call_order = []

    class OrderedRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            call_order.append("warm")
            return True

        def register_and_poll_once(self):
            call_order.append("poll")
            return {"next_ping_in_x_seconds": 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=OrderedRuntime)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: len(call_order) > 2)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 0
    assert call_order[:3] == ["warm", "poll", "poll"]
    _ = capsys.readouterr()


def test_run_does_not_wait_for_active_warmup_before_runtime_stop(capsys, monkeypatch):
    _reset_cancel_queue()
    events = []
    warm_started = threading.Event()
    release_warmup = threading.Event()

    class SlowWarmupRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            events.append("warm-start")
            warm_started.set()
            release_warmup.wait(timeout=1.0)
            events.append("warm-done")
            return True

        def register_and_poll_once(self):
            events.append("poll")
            return {"next_ping_in_x_seconds": 0}

        def stop(self):
            events.append("stop")

    _install_fake_runtime_module(monkeypatch, runtime_cls=SlowWarmupRuntime)
    stop_calls = {"count": 0}

    def fake_stop_requested():
        stop_calls["count"] += 1
        if stop_calls["count"] == 1:
            return False
        assert warm_started.wait(timeout=1.0)
        return True

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    started_at = time.perf_counter()
    status = compute_node_bridge.run(args)
    elapsed = time.perf_counter() - started_at

    try:
        assert status == 0
        assert elapsed < 0.5
        assert "stop" in events
        assert "warm-done" not in events[: events.index("stop") + 1]
    finally:
        release_warmup.set()
    _ = capsys.readouterr()


def test_run_pre_registration_warmup_times_out_without_registering(capsys, monkeypatch):
    _reset_cancel_queue()
    events = []
    warm_started = threading.Event()
    never_release_warmup = threading.Event()

    class StuckWarmupRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            events.append("warm-start")
            warm_started.set()
            never_release_warmup.wait()
            events.append("warm-done")
            return True

        def register_and_poll_once(self):
            events.append("poll")
            return {"next_ping_in_x_seconds": 0}

        def stop(self):
            events.append("stop")

    _install_fake_runtime_module(monkeypatch, runtime_cls=StuckWarmupRuntime)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: False)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    monkeypatch.setenv("TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS", "0.05")
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    started_at = time.perf_counter()
    status = compute_node_bridge.run(args)
    elapsed = time.perf_counter() - started_at

    assert status == 1
    assert elapsed < 0.5
    assert warm_started.is_set()
    assert any(
        thread.daemon
        for thread in threading.enumerate()
        if thread.name == "tokenplace-warm-load" and thread.is_alive()
    )
    assert "poll" not in events
    assert "warm-done" not in events
    assert "stop" in events
    captured = capsys.readouterr()
    output_events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    error_event = next(event for event in output_events if event.get("type") == "error")
    assert error_event["warm_load_state"] == "failed"
    assert error_event["relay_runtime_state"] == "failed"
    assert error_event["running"] is False
    assert error_event["registered"] is False
    assert error_event["message"] == "API v1 relay runtime warm-load timed out after 0.05s"
    assert error_event["last_error"] == error_event["message"]
    warming_status_events = [
        event
        for event in output_events
        if event.get("type") == "status" and event.get("warm_load_state") == "warming"
    ]
    assert len(warming_status_events) == 1
    assert "registration.gate_wait_timeout" in captured.err
    assert "api_v1_e2ee.runtime_wait.start" not in captured.err
    assert "api_v1_e2ee.runtime_wait.timeout" not in captured.err


def test_run_cancel_stays_responsive_during_active_relay_poll(capsys, monkeypatch):
    _reset_cancel_queue()
    events = []
    poll_started = threading.Event()
    release_poll = threading.Event()

    class BlockingPollRuntime(FakeRuntime):
        def register_and_poll_once(self):
            events.append("poll-start")
            poll_started.set()
            release_poll.wait(timeout=1.0)
            events.append("poll-done")
            return {"next_ping_in_x_seconds": 0}

        def stop(self):
            events.append("stop")

    _install_fake_runtime_module(monkeypatch, runtime_cls=BlockingPollRuntime)
    stop_calls = {"count": 0}

    def fake_stop_requested():
        stop_calls["count"] += 1
        if stop_calls["count"] == 1:
            return False
        assert poll_started.wait(timeout=1.0)
        return True

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    try:
        assert status == 0
        assert "stop" in events
        assert "poll-done" not in events[: events.index("stop") + 1]
    finally:
        release_poll.set()
    _ = capsys.readouterr()


def test_run_stops_when_pre_registration_runtime_warmup_fails(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class FailingWarmupRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            calls.append("warm")
            return False

        def register_and_poll_once(self):
            calls.append("poll")
            return {"next_ping_in_x_seconds": 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=FailingWarmupRuntime)
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    status = compute_node_bridge.run(args)
    assert status == 1
    assert calls == ["warm"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    payload = next(event for event in events if event.get("type") == "error")
    assert payload["type"] == "error"


def test_run_does_not_warm_when_disabled(capsys, monkeypatch):
    _reset_cancel_queue()
    call_order = []

    class OrderedRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            call_order.append("warm")
            return True

        def register_and_poll_once(self):
            call_order.append("poll")
            return {"next_ping_in_x_seconds": 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=OrderedRuntime)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: len(call_order) > 1)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 0
    assert call_order == ["poll", "poll"]
    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    status_events = [event for event in events if event.get("type") == "status"]
    assert any(event.get("registered") is True for event in status_events)
    assert all(event.get("relay_runtime_state") == "ready" for event in status_events)


def test_run_sidecar_runtime_path_warms_bridge_before_registration_without_dual_opt_in(capsys, monkeypatch):
    _reset_cancel_queue()
    call_order = []

    class OrderedRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            call_order.append("warm")
            return True

        def register_and_poll_once(self):
            call_order.append("poll")
            return {"next_ping_in_x_seconds": 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=OrderedRuntime)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: len(call_order) > 1)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    monkeypatch.setenv("TOKENPLACE_DESKTOP_RUNTIME_PATH", "sidecar")
    monkeypatch.delenv("TOKENPLACE_DESKTOP_DUAL_RUNTIME", raising=False)
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 0
    assert call_order == ["warm", "poll"]
    output = capsys.readouterr()
    assert "runtime_path.relay_uses_bridge" in output.err
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    started = next(event for event in events if event.get("type") == "started")
    assert started["runtime_path"] == "sidecar"


def test_run_sidecar_runtime_path_with_dual_mode_warms_and_logs_opt_in(capsys, monkeypatch):
    _reset_cancel_queue()
    call_order = []

    class OrderedRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            call_order.append("warm")
            return True

        def register_and_poll_once(self):
            call_order.append("poll")
            return {"next_ping_in_x_seconds": 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=OrderedRuntime)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: len(call_order) > 2)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    monkeypatch.setenv("TOKENPLACE_DESKTOP_RUNTIME_PATH", "sidecar")
    monkeypatch.setenv("TOKENPLACE_DESKTOP_DUAL_RUNTIME", "1")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 0
    assert call_order[:3] == ["warm", "poll", "poll"]
    output = capsys.readouterr()
    assert "runtime_path.relay_uses_bridge" in output.err


def test_run_api_v1_payload_not_dropped_when_warm_not_started(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class ApiPayloadFirstRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            calls.append("warm")
            return True

        def register_and_poll_once(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return {
                    "protocol": "tokenplace_api_v1_relay_e2ee",
                    "version": 1,
                    "request_id": "req-bridge-1",
                    "client_public_key": "abc",
                    "chat_history": "ciphertext",
                    "cipherkey": "key",
                    "iv": "iv",
                    "next_ping_in_x_seconds": 0,
                }
            return {"next_ping_in_x_seconds": 0}

        def process_relay_request(self, payload):
            calls.append("process")
            return bool(payload.get("request_id"))

    _install_fake_runtime_module(monkeypatch, runtime_cls=ApiPayloadFirstRuntime)
    stop_counter = {"n": 0}

    def fake_stop_requested():
        stop_counter["n"] += 1
        return stop_counter["n"] > 3

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 0
    assert calls == ["warm", "process"]
    output = capsys.readouterr()
    assert "runtime_path=bridge" in output.err


def test_run_first_api_v1_payload_fails_closed_when_warm_load_fails(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class ApiPayloadWarmFailRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            calls.append("warm")
            return False

        def register_and_poll_once(self):
            return {
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "request_id": "req-bridge-fail-1",
                "client_public_key": "abc",
                "chat_history": "ciphertext",
                "cipherkey": "key",
                "iv": "iv",
                "next_ping_in_x_seconds": 0,
            }

        def process_relay_request(self, _payload):
            calls.append("process")
            return True

    _install_fake_runtime_module(monkeypatch, runtime_cls=ApiPayloadWarmFailRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 1
    assert calls == ["warm"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get("type") == "error")
    assert error_event["warm_load_state"] == "failed"


def test_run_first_api_v1_payload_fails_closed_when_warm_load_raises(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class ApiPayloadWarmRaiseRuntime(ApiV1Runtime):
        last_instance = None

        def __init__(self, config):
            super().__init__(config)
            ApiPayloadWarmRaiseRuntime.last_instance = self

        def ensure_api_v1_runtime_ready(self):
            calls.append("warm")
            raise RuntimeError("sensitive model init failure details")

        def process_relay_request(self, _payload):
            calls.append("process")
            return True

    _install_fake_runtime_module(monkeypatch, runtime_cls=ApiPayloadWarmRaiseRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 1
    assert calls == ["warm"]
    assert "request_id=none" in output.err
    assert "runtime_wait.exception" in output.err or "model_init.exception" in output.err
    assert "error_response.submitted" not in output.err
    assert "code=compute_node_runtime_unavailable" not in output.err
    assert "exc_type=RuntimeError" in output.err
    assert "sensitive model init failure details" not in output.err
    assert ApiPayloadWarmRaiseRuntime.last_instance is not None
    assert ApiPayloadWarmRaiseRuntime.last_instance.relay_client.endpoint_calls == []
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get("type") == "error")
    assert error_event["warm_load_state"] == "failed"


def test_run_first_api_v1_payload_fails_closed_when_blocking_warm_load_raises(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class BlockingWarmRaiseRuntime(ApiV1Runtime):
        last_instance = None

        def __init__(self, config):
            super().__init__(config)
            BlockingWarmRaiseRuntime.last_instance = self

        def ensure_api_v1_runtime_ready(self):
            calls.append("warm")
            time.sleep(0.05)
            raise RuntimeError("sensitive delayed model init details")

        def process_relay_request(self, _payload):
            calls.append("process")
            return True

    _install_fake_runtime_module(monkeypatch, runtime_cls=BlockingWarmRaiseRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    monkeypatch.setenv("TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS", "1")
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 1
    assert calls == ["warm"]
    assert "request_id=none" in output.err
    assert "runtime_wait.exception" in output.err
    assert "error_response.submitted" not in output.err
    assert "exc_type=RuntimeError" in output.err
    assert "sensitive delayed model init details" not in output.err
    assert BlockingWarmRaiseRuntime.last_instance is not None
    assert BlockingWarmRaiseRuntime.last_instance.relay_client.endpoint_calls == []
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get("type") == "error")
    assert error_event["warm_load_state"] == "failed"


def test_run_fails_fast_when_dependency_preflight_fails(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_python_dependencies',
        lambda: {
            'ok': 'false',
            'action': 'requirements_missing',
            'missing': 'psutil',
            'interpreter': 'python',
            'import_root': '/runtime',
            'detail': 'requirements missing',
        },
    )
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)
    status = compute_node_bridge.run(args)
    assert status == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get("type") == "error")
    assert "dependency preflight failed" in error_event["message"]
    assert error_event["running"] is False
    assert error_event["registered"] is False
    assert error_event["relay_runtime_state"] == "failed"
    assert error_event["last_error"] == error_event["message"]
    assert error_event["operator_session_id"]
    assert error_event["sequence"] == 1
    assert isinstance(error_event["updated_at_ms"], int)


def test_cancelable_poll_worker_returns_after_empty_poll_and_reraises():
    worker = compute_node_bridge._CancelablePollWorker()
    call_started = threading.Event()
    release_call = threading.Event()

    def delayed_result():
        call_started.set()
        release_call.wait(timeout=1.0)
        return {"ok": True}

    try:
        def cancel_after_first_empty_poll():
            if call_started.is_set():
                release_call.set()
            return False

        assert worker.call(
            delayed_result, cancel_after_first_empty_poll, poll_interval=0.01
        ) == {"ok": True}

        with pytest.raises(RuntimeError, match="poll exploded"):
            worker.call(lambda: (_ for _ in ()).throw(RuntimeError("poll exploded")), lambda: False)
    finally:
        worker.shutdown()

    assert worker.call(lambda: {"ok": False}, lambda: False) is compute_node_bridge._POLL_CANCELLED


def test_registration_fresh_supports_legacy_no_arg_helper_and_missing_helper():
    class LegacyFreshClient:
        def api_v1_registration_fresh(self):
            return True

    assert compute_node_bridge._registration_fresh(LegacyFreshClient(), "https://relay.example") is True
    assert compute_node_bridge._registration_fresh(object(), "https://relay.example") is False


def test_cached_poll_wait_seconds_normalizes_hints_and_rejects_bad_values():
    class RelayClient:
        _api_v1_relay_wait_hints = {
            "https://relay.example": {"poll_wait_seconds": "12.5"},
            "https://bool.example": {"poll_wait_seconds": True},
            "https://negative.example": {"poll_wait_seconds": -1},
            "https://nan.example": {"poll_wait_seconds": float("nan")},
            "https://bad.example": {"poll_wait_seconds": "soon"},
        }

    client = RelayClient()
    assert compute_node_bridge._cached_poll_wait_seconds(client, "https://relay.example", 3) == 12.5
    assert compute_node_bridge._cached_poll_wait_seconds(client, "https://bool.example", 3) == 3
    assert compute_node_bridge._cached_poll_wait_seconds(client, "https://negative.example", 3) == 3
    assert compute_node_bridge._cached_poll_wait_seconds(client, "https://nan.example", 3) == 3
    assert compute_node_bridge._cached_poll_wait_seconds(client, "https://bad.example", 3) == 3
    assert compute_node_bridge._cached_poll_wait_seconds(object(), "https://relay.example", 3) == 3


def test_run_sidecar_api_v1_payload_warms_bridge_before_polling_without_dual_opt_in(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class SidecarApiPayloadRuntime(ApiV1Runtime):
        def ensure_api_v1_runtime_ready(self):
            calls.append("warm")
            return True

        def process_relay_request(self, payload):
            calls.append("process")
            return super().process_relay_request(payload)

    _install_fake_runtime_module(monkeypatch, runtime_cls=SidecarApiPayloadRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    monkeypatch.setenv("TOKENPLACE_DESKTOP_RUNTIME_PATH", "sidecar")
    monkeypatch.delenv("TOKENPLACE_DESKTOP_DUAL_RUNTIME", raising=False)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: 'process' in calls)
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    assert calls == ["warm", "process"]
    output = capsys.readouterr()
    assert "runtime_path.relay_uses_bridge" in output.err


def test_run_reports_api_v1_processing_failure(capsys, monkeypatch):
    _reset_cancel_queue()

    class ApiV1ProcessingFailureRuntime(ApiV1Runtime):
        def process_relay_request(self, payload):
            self._processed.append(payload)
            return False

    _install_fake_runtime_module(monkeypatch, runtime_cls=ApiV1ProcessingFailureRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: bool(ApiV1ProcessingFailureRuntime.last_instance._processed))
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    status_events = [event for event in events if event.get("type") == "status"]
    assert status_events[-1]["last_error"] == "failed to process relay request"


def test_run_logs_api_v1_processing_exception_type(capsys, monkeypatch):
    _reset_cancel_queue()

    class ApiV1ProcessingExceptionRuntime(ApiV1Runtime):
        def process_relay_request(self, payload):
            self._processed.append(payload)
            raise ValueError("sensitive payload details")

    _install_fake_runtime_module(monkeypatch, runtime_cls=ApiV1ProcessingExceptionRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    monkeypatch.setattr(
        compute_node_bridge,
        'stop_requested',
        lambda: bool(ApiV1ProcessingExceptionRuntime.last_instance._processed),
    )
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert "process_request.exception" in output.err
    assert "exc_type=ValueError" in output.err
    assert "sensitive payload details" not in output.err


def test_run_reports_unreachable_for_non_heartbeat_response(capsys, monkeypatch):
    _reset_cancel_queue()

    class NonHeartbeatRuntime(FakeRuntime):
        def register_and_poll_once(self):
            return {"relay_version": "desktop-v0.1.0"}

    _install_fake_runtime_module(monkeypatch, runtime_cls=NonHeartbeatRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: False)
    monkeypatch.setattr(compute_node_bridge, '_sleep_with_cancel', lambda _seconds: True)
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    status_event = next(event for event in events if event.get("type") == "status")
    assert status_event["registered"] is False
    assert "relay appears unreachable" in status_event["last_error"]


def test_run_handles_keyboard_interrupt_from_poll_worker(capsys, monkeypatch):
    _reset_cancel_queue()

    class KeyboardInterruptRuntime(FakeRuntime):
        def register_and_poll_once(self):
            raise KeyboardInterrupt

        def stop(self):
            self.stopped = True

    _install_fake_runtime_module(monkeypatch, runtime_cls=KeyboardInterruptRuntime)
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events[-1]["type"] == "stopped"


class ShutdownTrackingRelayClient(FakeRelayClient):
    def __init__(self):
        self.stop_calls = 0
        self.unregister_calls = 0
        self._unregistered = False

    def stop(self):
        self.stop_calls += 1

    def unregister_from_relay(self):
        if self._unregistered:
            return True
        self._unregistered = True
        self.unregister_calls += 1
        return True


class ShutdownTrackingRuntime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        ShutdownTrackingRuntime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = ShutdownTrackingRelayClient()
        self.poll_calls = 0
        self.stop_calls = 0
        self._processed = []

    def register_and_poll_once(self):
        self.poll_calls += 1
        return {'next_ping_in_x_seconds': 60}

    def stop(self):
        self.stop_calls += 1
        self.relay_client.stop()
        self.relay_client.unregister_from_relay()


def test_run_unregisters_once_and_does_not_poll_after_cancel(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=ShutdownTrackingRuntime)
    stop_calls = {'count': 0}

    def fake_stop_requested():
        stop_calls['count'] += 1
        return stop_calls['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    runtime = ShutdownTrackingRuntime.last_instance
    assert runtime.poll_calls == 1
    assert runtime.stop_calls == 1
    assert runtime.relay_client.stop_calls >= 1
    assert runtime.relay_client.unregister_calls == 1
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.poll.cancel_requested' in output.err
    assert 'desktop.compute_node_bridge.unregister.succeeded' in output.err
    assert 'desktop.compute_node_bridge.poll.worker_stopped' in output.err


class StopFailureRelayClient(FakeRelayClient):
    def __init__(self):
        self.stop_calls = 0
        self.unregister_calls = 0

    def stop(self):
        self.stop_calls += 1
        raise RuntimeError('relay stop failed')

    def unregister_from_relay(self):
        self.unregister_calls += 1
        return True


class StopFailureRuntime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        StopFailureRuntime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = StopFailureRelayClient()
        self.stop_calls = 0
        self._processed = []

    def register_and_poll_once(self):
        return {'next_ping_in_x_seconds': 60}

    def stop(self):
        self.stop_calls += 1


def test_run_continues_shutdown_when_relay_stop_raises(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=StopFailureRuntime)
    stop_calls = {'count': 0}

    def fake_stop_requested():
        stop_calls['count'] += 1
        return stop_calls['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    runtime = StopFailureRuntime.last_instance
    assert runtime.stop_calls == 1
    assert runtime.relay_client.stop_calls == 1
    assert runtime.relay_client.unregister_calls == 1
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.relay.stop_failed' in output.err
    assert 'exc_type=RuntimeError' in output.err
    assert 'desktop.compute_node_bridge.unregister.succeeded' in output.err
    assert json.loads(output.out.splitlines()[-1])['type'] == 'stopped'


class UnregisterFailureRelayClient(FakeRelayClient):
    def __init__(self):
        self.stop_calls = 0
        self.unregister_calls = 0

    def stop(self):
        self.stop_calls += 1

    def unregister_from_relay(self):
        self.unregister_calls += 1
        raise TimeoutError('relay unregister timed out')


class UnregisterFailureRuntime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        UnregisterFailureRuntime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = UnregisterFailureRelayClient()
        self.stop_calls = 0
        self._processed = []

    def register_and_poll_once(self):
        return {'next_ping_in_x_seconds': 60}

    def stop(self):
        self.stop_calls += 1


def test_run_continues_shutdown_when_unregister_raises(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=UnregisterFailureRuntime)
    stop_calls = {'count': 0}

    def fake_stop_requested():
        stop_calls['count'] += 1
        return stop_calls['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    runtime = UnregisterFailureRuntime.last_instance
    assert runtime.stop_calls == 1
    assert runtime.relay_client.stop_calls == 1
    assert runtime.relay_client.unregister_calls == 1
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.unregister.failed' in output.err
    assert 'exc_type=TimeoutError' in output.err
    assert 'desktop.compute_node_bridge.poll.worker_stopped' in output.err
    assert json.loads(output.out.splitlines()[-1])['type'] == 'stopped'


def test_cancelable_poll_worker_invokes_cancel_callback_promptly_during_long_poll():
    worker = compute_node_bridge._CancelablePollWorker()
    call_started = threading.Event()
    release_call = threading.Event()
    cancel_calls = []

    def long_poll():
        call_started.set()
        release_call.wait(timeout=1.0)
        return {'ok': True}

    try:
        result = worker.call(
            long_poll,
            lambda: call_started.is_set(),
            poll_interval=0.01,
            on_cancel=lambda: cancel_calls.append('cancelled'),
        )
    finally:
        release_call.set()
        worker.shutdown()

    assert result is compute_node_bridge._POLL_CANCELLED
    assert cancel_calls == ['cancelled']


def test_stop_requested_latches_cancel_from_stdin_queue():
    _reset_cancel_queue()
    compute_node_bridge._stdin_lines.put(json.dumps({'type': 'cancel'}))

    assert compute_node_bridge.stop_requested() is True
    assert compute_node_bridge.stop_requested() is True


def test_run_cancel_during_warm_load_exits_without_registering(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=WarmingThenApiV1Runtime)
    stop_after_warm_started = {'armed': False}

    def fake_stop_requested():
        instance = WarmingThenApiV1Runtime.last_instance
        if instance is not None and instance.ready_started.is_set():
            stop_after_warm_started['armed'] = True
            return True
        return False

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '1')
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    status = compute_node_bridge.run(args)

    assert status == 0
    runtime = WarmingThenApiV1Runtime.last_instance
    assert stop_after_warm_started['armed'] is True
    assert runtime._processed == []
    assert runtime._responses == [
        {
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'version': 1,
            'request_id': 'req-1',
            'client_public_key': 'client-key',
            'chat_history': 'ciphertext',
            'cipherkey': 'key',
            'iv': 'iv',
            'next_ping_in_x_seconds': 0,
        }
    ]
    assert capsys.readouterr().out.splitlines()[-1]

class StaleFreshnessRuntime(FakeRuntime):
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = StaleRelayClient()
        self._responses = [{"next_ping_in_x_seconds": 0, "error": None}]
        self._processed = []


class MetalApiV1ParityRuntime(ApiV1Runtime):
    def __init__(self, _config):
        super().__init__(_config)
        self.model_manager.last_compute_diagnostics = None

    def ensure_api_v1_runtime_ready(self):
        self.model_manager.last_compute_diagnostics = {
            "requested_mode": "gpu",
            "effective_mode": "gpu",
            "backend_available": "metal",
            "backend_selected": "metal",
            "backend_used": "metal",
            "n_gpu_layers": -1,
            "offloaded_layers": -1,
            "kv_cache_device": "metal",
            "fallback_reason": None,
        }
        return True


def test_platform_neutral_status_registered_only_after_fresh_ready_runtime(
    capsys, monkeypatch
):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=StaleFreshnessRuntime)
    stop_counter = {"count": 0}

    def fake_stop_requested():
        stop_counter["count"] += 1
        return stop_counter["count"] > 3

    monkeypatch.setattr(compute_node_bridge, "stop_requested", fake_stop_requested)
    args = SimpleNamespace(
        model="/tmp/model.gguf",
        mode="cpu",
        relay_url="https://token.place",
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0

    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert any(event.get("relay_runtime_state") == "ready" for event in events)
    assert not any(event.get("registered") is True for event in events)
    stale_status = next(
        event
        for event in events
        if event.get("type") == "status" and event.get("relay_runtime_state") == "ready"
    )
    assert stale_status["registered"] is False
    assert "unreachable, old, or incompatible" in stale_status["last_error"]


def test_platform_neutral_status_backend_fields_follow_relay_processing_runtime(
    capsys, monkeypatch
):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=MetalApiV1ParityRuntime)
    monkeypatch.setattr(
        compute_node_bridge,
        "ensure_desktop_llama_runtime",
        lambda _mode: {
            "selected_backend": "metal",
            "detected_device": "metal",
            "runtime_action": "already_supported",
            "interpreter": sys.executable,
            "llama_module_path": "/opt/site-packages/llama_cpp/__init__.py",
            "fallback_reason": "",
        },
    )
    stop_counter = {"count": 0}

    def fake_stop_requested():
        stop_counter["count"] += 1
        return stop_counter["count"] > 4

    monkeypatch.setattr(compute_node_bridge, "stop_requested", fake_stop_requested)
    args = SimpleNamespace(
        model="/tmp/model.gguf",
        mode="gpu",
        relay_url="https://token.place",
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0

    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    registered_events = [event for event in events if event.get("registered") is True]
    assert registered_events
    for event in registered_events:
        assert event["relay_runtime_state"] in {"ready", "processing"}
        assert event["backend_available"] == "metal"
        assert event["backend_selected"] == "metal"
        assert event["backend_used"] == "metal"
        assert event["last_error"] is None


def test_platform_neutral_runtime_setup_failure_last_error_is_actionable(
    capsys, monkeypatch
):
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cpu',
            'detected_device': 'none',
            'runtime_action': 'failed',
            'interpreter': '/opt/token.place/python',
            'llama_module_path': 'missing',
            'fallback_reason': 'No module named llama_cpp; install llama-cpp-python',
        },
    )
    monkeypatch.setattr(
        compute_node_bridge,
        'desktop_gpu_runtime_failure_message',
        lambda _mode, _setup: (
            'desktop model runtime setup failed '
            '(interpreter=/opt/token.place/python import_root=/runtime missing=llama_cpp): '
            'install llama-cpp-python before relay registration'
        ),
    )
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_python_dependencies',
        lambda: {'ok': 'true', 'missing': '', 'action': 'already_available'},
    )
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://token.place',
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    assert status == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    payload = events[-1]
    assert payload['type'] == 'error'
    assert payload['registered'] is False
    assert payload['relay_runtime_state'] == 'failed'
    assert payload['last_error'] == payload['message']
    assert 'desktop model runtime setup failed' in payload['last_error']
    assert 'interpreter=/opt/token.place/python' in payload['last_error']
    assert 'missing=llama_cpp' in payload['last_error']
    assert 'before relay registration' in payload['last_error']


def test_platform_neutral_dependency_failure_last_error_is_actionable(
    capsys, monkeypatch
):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        "ensure_desktop_python_dependencies",
        lambda **_: {
            "ok": "false",
            "action": "install_failed",
            "missing": "cryptography,requests",
            "interpreter": "/Applications/token.place.app/Contents/MacOS/python",
            "import_root": "/Applications/token.place.app/Contents/Resources",
            "detail": "pip install failed for desktop bridge dependencies",
        },
    )
    args = SimpleNamespace(
        model="/tmp/model.gguf",
        mode="auto",
        relay_url="https://token.place",
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 1

    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    payload = events[0]
    assert payload["type"] == "error"
    assert payload["registered"] is False
    assert payload["relay_runtime_state"] == "failed"
    assert payload["last_error"] == payload["message"]
    assert "desktop runtime dependency preflight failed" in payload["last_error"]
    assert (
        "interpreter=/Applications/token.place.app/Contents/MacOS/python"
        in payload["last_error"]
    )
    assert (
        "import_root=/Applications/token.place.app/Contents/Resources"
        in payload["last_error"]
    )
    assert "missing=cryptography,requests" in payload["last_error"]


class ImportTimeoutThenReadyRuntime(FakeRuntime):
    instances = []

    def __init__(self, _config):
        super().__init__(_config)
        self.ensure_calls = 0
        self.register_calls = 0
        ImportTimeoutThenReadyRuntime.instances.append(self)

    def ensure_api_v1_runtime_ready(self):
        self.ensure_calls += 1
        if len(ImportTimeoutThenReadyRuntime.instances) == 1:
            self.model_manager.last_runtime_init_error = 'llama_cpp_import_timeout after 0.01s'
            return False
        self.model_manager.last_runtime_init_error = None
        return True

    def register_and_poll_once(self):
        self.register_calls += 1
        return {'next_ping_in_x_seconds': 0, 'error': None}


def test_pre_registration_import_timeout_unregistered_and_start_after_failure_retries(
    capsys, monkeypatch
):
    _reset_cancel_queue()
    ImportTimeoutThenReadyRuntime.instances = []
    _install_fake_runtime_module(monkeypatch, runtime_cls=ImportTimeoutThenReadyRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '1')
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 1
    first_events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    first_error = next(event for event in first_events if event.get('type') == 'error')
    assert first_error['registered'] is False
    assert first_error['last_error'] == 'llama_cpp_import_timeout after 0.01s'
    assert not any(event.get('registered') is True for event in first_events)
    assert ImportTimeoutThenReadyRuntime.instances[0].register_calls == 0

    stop_counter = {'count': 0}

    def stop_after_first_poll():
        stop_counter['count'] += 1
        return stop_counter['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_first_poll)
    assert compute_node_bridge.run(args) == 0
    second_events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(ImportTimeoutThenReadyRuntime.instances) == 2
    assert ImportTimeoutThenReadyRuntime.instances[1].ensure_calls >= 1
    assert ImportTimeoutThenReadyRuntime.instances[1].register_calls >= 1
    assert any(event.get('registered') is True for event in second_events)


def test_pre_registration_warmup_failure_reports_runtime_stage_error(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = []

    class RuntimeDiscoveryFailureRuntime(FakeRuntime):
        def ensure_api_v1_runtime_ready(self):
            calls.append('warm')
            self.model_manager.last_runtime_init_error = 'llama_cpp_import_timeout after 0.01s'
            return False

        def register_and_poll_once(self):
            calls.append('poll')
            return {'next_ping_in_x_seconds': 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=RuntimeDiscoveryFailureRuntime)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        lambda _mode: {
            'selected_backend': 'cuda',
            'detected_device': 'cuda',
            'runtime_action': 'already_supported',
            'interpreter': sys.executable,
            'llama_module_path': '/opt/site-packages/llama_cpp/__init__.py',
            'fallback_reason': '',
        },
    )
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '1')
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='gpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 1

    assert calls == ['warm']
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get('type') == 'error')
    assert error_event['registered'] is False
    assert error_event['warm_load_state'] == 'failed'
    assert error_event['last_error'] == 'llama_cpp_import_timeout after 0.01s'
    assert error_event['message'] == 'llama_cpp_import_timeout after 0.01s'


class CurrentStageTimeoutWarmupRuntime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        super().__init__(_config)
        CurrentStageTimeoutWarmupRuntime.last_instance = self
        self.ready_started = threading.Event()

    def ensure_api_v1_runtime_ready(self):
        self.model_manager.last_runtime_init_error = 'llama_cpp_gpu_probe_timeout after 0.01s'
        self.ready_started.set()
        time.sleep(0.2)
        return True


def test_pre_registration_warmup_timeout_reports_current_runtime_stage(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=CurrentStageTimeoutWarmupRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '1')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS', '0.01')
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 1

    runtime = CurrentStageTimeoutWarmupRuntime.last_instance
    assert runtime is not None
    assert runtime.ready_started.is_set()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get('type') == 'error')
    assert error_event['registered'] is False
    assert error_event['warm_load_state'] == 'failed'
    assert error_event['last_error'] == 'llama_cpp_gpu_probe_timeout after 0.01s'
    assert error_event['message'] == 'llama_cpp_gpu_probe_timeout after 0.01s'


class NeverRegisteredRelayClient(FakeRelayClient):
    def __init__(self):
        self.stop_calls = 0
        self.unregister_calls = 0

    def api_v1_registration_fresh(self, _relay_url=None):
        return False

    def stop(self):
        self.stop_calls += 1

    def unregister_from_relay(self):
        self.unregister_calls += 1
        return True


class NeverRegisteredCancelRuntime(FakeRuntime):
    last_instance = None

    def __init__(self, _config):
        NeverRegisteredCancelRuntime.last_instance = self
        self.model_manager = FakeModelManager()
        self.relay_client = NeverRegisteredRelayClient()
        self.stop_calls = 0
        self._processed = []

    def register_and_poll_once(self):
        return {'next_ping_in_x_seconds': 60}

    def stop(self):
        self.stop_calls += 1


def test_run_skips_unregister_when_poll_cancel_happens_before_fresh_registration(
    capsys, monkeypatch
):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=NeverRegisteredCancelRuntime)
    stop_calls = {'count': 0}

    def fake_stop_requested():
        stop_calls['count'] += 1
        return stop_calls['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0

    runtime = NeverRegisteredCancelRuntime.last_instance
    assert runtime.relay_client.stop_calls == 1
    assert runtime.relay_client.unregister_calls == 0
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.unregister.skipped' in output.err
    assert 'reason=not_registered' in output.err
