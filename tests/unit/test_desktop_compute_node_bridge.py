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

@pytest.fixture(autouse=True)
def _default_desktop_runtime_arch(monkeypatch):
    """Keep win32 platform simulations independent from the host CPU architecture."""

    runtime_setup = sys.modules.get('desktop_runtime_setup')
    if runtime_setup is not None:
        monkeypatch.setattr(runtime_setup.platform_module, 'machine', lambda: 'AMD64')


def test_api_v1_recovery_attempts_negative_value_uses_default(monkeypatch):
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '-1')

    assert compute_node_bridge._api_v1_recovery_attempts(default=3) == 3


@pytest.mark.parametrize(
    ('raw_value', 'expected'),
    [
        (None, 4),
        ('0', 0),
        ('5', 5),
        ('not-an-int', 4),
    ],
)
def test_api_v1_recovery_attempts_parses_valid_values_and_defaults(
    monkeypatch,
    raw_value,
    expected,
):
    if raw_value is None:
        monkeypatch.delenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', raising=False)
    else:
        monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', raw_value)

    assert compute_node_bridge._api_v1_recovery_attempts(default=4) == expected


@pytest.mark.parametrize(
    ('raw_value', 'expected'),
    [
        (None, 1.5),
        ('0', 0.0),
        ('2.25', 2.25),
        ('not-a-float', 1.5),
        ('nan', 1.5),
        ('-0.01', 1.5),
    ],
)
def test_api_v1_recovery_backoff_seconds_parses_valid_values_and_defaults(
    monkeypatch,
    raw_value,
    expected,
):
    if raw_value is None:
        monkeypatch.delenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', raising=False)
    else:
        monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', raw_value)

    assert compute_node_bridge._api_v1_recovery_backoff_seconds(default=1.5) == expected


class FakeModelManager:
    def __init__(self):
        self.model_path = ''
        self.default_n_gpu_layers = -1
        self.requested_compute_mode = 'auto'
        self.last_compute_diagnostics = None

    def worker_lifecycle_status(self):
        return {
            "worker_state": "ready",
            "worker_generation": 2,
            "worker_restart_count": 1,
            "worker_alive": True,
            "last_worker_error_code": None,
            "last_worker_exit_code": None,
            "last_worker_restart_at_ms": None,
        }


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
        'worker_state',
        'worker_generation',
        'worker_restart_count',
        'worker_alive',
        'last_worker_error_code',
        'last_worker_exit_code',
        'last_worker_restart_at_ms',
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
    assert stopped['last_worker_error_code'] is None
    assert stopped['last_worker_exit_code'] is None


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


def test_run_supplies_stop_requested_as_runtime_cancellation_predicate(monkeypatch):
    _reset_cancel_queue()
    captured = {}

    class CapturingRuntime(FakeRuntime):
        def __init__(self, config, cancellation_predicate=None):
            super().__init__(config)
            captured['cancellation_predicate'] = cancellation_predicate
            self._responses = [{'next_ping_in_x_seconds': 0}]

    _install_fake_runtime_module(monkeypatch, CapturingRuntime)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0
    assert captured['cancellation_predicate'] is compute_node_bridge.stop_requested
    assert captured['cancellation_predicate']() is True


def test_run_passes_desktop_relay_list_to_runtime(monkeypatch):
    _reset_cancel_queue()
    captured = {'configs': []}

    class CapturingRuntime:
        def __init__(self, config):
            captured['configs'].append(config)
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
    module.resolve_relay_url = lambda relay_url, **_kwargs: relay_url.strip()
    module.normalize_compute_mode = lambda mode: mode
    module.apply_compute_mode = lambda _model_manager, mode: mode
    module.SUPPORTED_COMPUTE_MODES = {'auto', 'cpu', 'cuda', 'metal'}
    module.compute_mode_diagnostics = lambda _model_manager: {}
    monkeypatch.setitem(sys.modules, 'utils.compute_node_runtime', module)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url=[
            ' http://127.0.0.1:5010 ',
            'https://staging.token.place',
            'https://staging.token.place',
        ],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    assert status == 0
    assert [config.relay_url for config in captured['configs']] == [
        'http://127.0.0.1:5010',
        'https://staging.token.place',
    ]
    assert all(config.use_configured_relay_fallbacks is False for config in captured['configs'])
    assert [config.relay_urls for config in captured['configs']] == [
        ('http://127.0.0.1:5010',),
        ('https://staging.token.place',),
    ]



def test_normalize_relay_urls_accepts_repeated_json_and_comma_values():
    assert compute_node_bridge._normalize_relay_urls(
        [' https://token.place ', 'https://staging.token.place,https://token.place'],
        '["https://dev.token.place", "https://staging.token.place"]',
    ) == [
        'https://token.place',
        'https://staging.token.place',
        'https://dev.token.place',
    ]


def test_run_multi_relay_status_reports_partial_success(capsys, monkeypatch):
    _reset_cancel_queue()

    class PartialRelayClient(FakeRelayClient):
        def __init__(self, relay_url):
            self.relay_url = relay_url
            self._api_v1_registered_relays = set()

        def api_v1_registration_fresh(self, relay_url=None):
            return (relay_url or self.relay_url) in self._api_v1_registered_relays

        def stop(self):
            return None

        def unregister_from_relay(self):
            self._api_v1_registered_relays.clear()
            return True

    class PartialMultiRelayRuntime(FakeRuntime):
        instances = []
        poll_started = []

        def __init__(self, config, **kwargs):
            self.config = config
            self.model_manager = kwargs.get('model_manager') or FakeModelManager()
            self.crypto_manager = kwargs.get('crypto_manager') or object()
            self.relay_client = PartialRelayClient(config.relay_url)
            self._processed = []
            PartialMultiRelayRuntime.instances.append(self)

        def ensure_api_v1_runtime_ready(self):
            return True

        def register_and_poll_once(self):
            PartialMultiRelayRuntime.poll_started.append(self.config.relay_url)
            if 'staging' in self.config.relay_url:
                return {'next_ping_in_x_seconds': 0, 'error': 'staging outage'}
            self.relay_client._api_v1_registered_relays.add(self.config.relay_url)
            return {'next_ping_in_x_seconds': 0, 'error': None}

        def stop(self):
            self.relay_client.stop()
            self.relay_client.unregister_from_relay()

    PartialMultiRelayRuntime.instances = []
    PartialMultiRelayRuntime.poll_started = []
    _install_fake_runtime_module(monkeypatch, runtime_cls=PartialMultiRelayRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')

    stop_state = {'both_polled': False, 'count': 0}

    def stop_after_partial_status_can_emit():
        if set(PartialMultiRelayRuntime.poll_started) >= {
            'https://token.place',
            'https://staging.token.place',
        }:
            stop_state['both_polled'] = True
        if not stop_state['both_polled']:
            return False
        stop_state['count'] += 1
        return stop_state['count'] > 4

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_partial_status_can_emit)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url=['https://token.place', 'https://staging.token.place'],
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    partial = next(
        event for event in events
        if event.get('type') == 'status'
        and event.get('registered_relay_count') == 1
        and any(
            status['relay_url'] == 'https://staging.token.place' and status['last_error'] == 'staging outage'
            for status in event.get('relay_statuses', [])
        )
    )

    assert partial['configured_relay_count'] == 2
    assert partial['registered'] is True
    assert partial['registered_relay_urls'] == ['https://token.place']
    assert any(
        status['relay_url'] == 'https://staging.token.place' and status['last_error'] == 'staging outage'
        for status in partial['relay_statuses']
    )
    assert len(PartialMultiRelayRuntime.instances) == 2
    assert PartialMultiRelayRuntime.instances[0].model_manager is PartialMultiRelayRuntime.instances[1].model_manager
    assert PartialMultiRelayRuntime.instances[0].crypto_manager is PartialMultiRelayRuntime.instances[1].crypto_manager


def test_run_multi_relay_status_uses_canonical_relay_client_urls(capsys, monkeypatch):
    _reset_cancel_queue()

    class CanonicalRelayClient(FakeRelayClient):
        def __init__(self, relay_url):
            self.relay_url = 'https://token.place' if relay_url == 'token.place' else relay_url.rstrip('/')
            self._api_v1_registered_relays = set()

        def api_v1_registration_fresh(self, relay_url=None):
            return (relay_url or self.relay_url) in self._api_v1_registered_relays

        def stop(self):
            return None

        def unregister_from_relay(self):
            self._api_v1_registered_relays.clear()
            return True

    class CanonicalRuntime(FakeRuntime):
        poll_started = False

        def __init__(self, config, **kwargs):
            self.config = config
            self.model_manager = kwargs.get('model_manager') or FakeModelManager()
            self.crypto_manager = kwargs.get('crypto_manager') or object()
            self.relay_client = CanonicalRelayClient(config.relay_url)
            self._processed = []

        def ensure_api_v1_runtime_ready(self):
            return True

        def register_and_poll_once(self):
            CanonicalRuntime.poll_started = True
            self.relay_client._api_v1_registered_relays.add(self.relay_client.relay_url)
            return {'next_ping_in_x_seconds': 0, 'error': None}

        def stop(self):
            self.relay_client.stop()
            self.relay_client.unregister_from_relay()

    _install_fake_runtime_module(monkeypatch, runtime_cls=CanonicalRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    stop_counter = {'count': 0}

    def stop_after_status_can_emit():
        stop_counter['count'] += 1
        return CanonicalRuntime.poll_started and stop_counter['count'] > 3

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_status_can_emit)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url=['token.place'],
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    registered_event = next(
        event for event in events
        if event.get('type') == 'status' and event.get('registered_relay_count') == 1
    )

    assert registered_event['registered'] is True
    assert registered_event['configured_relay_urls'] == ['https://token.place']
    assert registered_event['registered_relay_urls'] == ['https://token.place']
    assert registered_event['relay_statuses'][0]['relay_url'] == 'https://token.place'


def test_run_reports_relay_poll_exception_as_structured_error(capsys, monkeypatch):
    _reset_cancel_queue()

    class FailingPollRuntime(FakeRuntime):
        def __init__(self, config, **kwargs):
            self.config = config
            self.model_manager = kwargs.get('model_manager') or FakeModelManager()
            self.crypto_manager = kwargs.get('crypto_manager') or object()
            self.relay_client = FakeRelayClient()
            self.relay_client.relay_url = config.relay_url
            self._processed = []

        def ensure_api_v1_runtime_ready(self):
            return True

        def register_and_poll_once(self):
            raise RuntimeError('network unavailable')

        def stop(self):
            return None

    _install_fake_runtime_module(monkeypatch, runtime_cls=FailingPollRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: False)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 1
    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get('type') == 'error')

    assert 'desktop.compute_node_bridge.poll.exception' in output.err
    assert error_event['relay_runtime_state'] == 'failed'
    assert error_event['registered'] is False
    assert error_event['last_error'] == 'relay poll failed: RuntimeError: network unavailable'
    assert error_event['message'] == error_event['last_error']
    assert error_event['relay_statuses'][0]['last_error'] == error_event['last_error']


def test_run_multi_relay_processes_each_relay_and_serializes_inference(capsys, monkeypatch):
    _reset_cancel_queue()

    class MultiApiRelayClient(FakeRelayClientRouting):
        def __init__(self, relay_url):
            super().__init__()
            self.relay_url = relay_url
            self._api_v1_registered_relays = {relay_url}

        def stop(self):
            return None

        def unregister_from_relay(self):
            self._api_v1_registered_relays.clear()
            return True

        def api_v1_registration_fresh(self, relay_url=None):
            return (relay_url or self.relay_url) in self._api_v1_registered_relays

    class MultiWorkRuntime(FakeRuntime):
        instances = []
        active_processing = 0
        max_active_processing = 0
        processed_relays = []
        processed_lock = threading.Lock()

        def __init__(self, config, **kwargs):
            self.config = config
            self.model_manager = kwargs.get('model_manager') or FakeModelManager()
            self.crypto_manager = kwargs.get('crypto_manager') or object()
            self.relay_client = MultiApiRelayClient(config.relay_url)
            self._processed = []
            self._sent = False
            MultiWorkRuntime.instances.append(self)

        def ensure_api_v1_runtime_ready(self):
            return True

        def register_and_poll_once(self):
            if self._sent:
                return {'next_ping_in_x_seconds': 0, 'error': None}
            self._sent = True
            suffix = 'a' if self.config.relay_url == 'https://token.place' else 'b'
            return {
                'protocol': 'tokenplace_api_v1_relay_e2ee',
                'version': 1,
                'request_id': f'req-{suffix}',
                'client_public_key': 'client-key',
                'chat_history': 'ciphertext',
                'cipherkey': 'key',
                'iv': 'iv',
                'next_ping_in_x_seconds': 0,
            }

        def process_relay_request(self, payload):
            with MultiWorkRuntime.processed_lock:
                MultiWorkRuntime.active_processing += 1
                MultiWorkRuntime.max_active_processing = max(
                    MultiWorkRuntime.max_active_processing,
                    MultiWorkRuntime.active_processing,
                )
            try:
                time.sleep(0.02)
                self._processed.append(payload)
                MultiWorkRuntime.processed_relays.append((self.config.relay_url, payload['request_id']))
                return self.relay_client.process_api_v1_chat_request(payload)
            finally:
                with MultiWorkRuntime.processed_lock:
                    MultiWorkRuntime.active_processing -= 1

        def stop(self):
            self.relay_client.stop()
            self.relay_client.unregister_from_relay()

    _install_fake_runtime_module(monkeypatch, runtime_cls=MultiWorkRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setattr(
        compute_node_bridge,
        'stop_requested',
        lambda: len(MultiWorkRuntime.processed_relays) >= 2,
    )
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url=['https://token.place', 'https://staging.token.place'],
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    processing_event = next(
        event for event in events
        if event.get('type') == 'status'
        and event.get('relay_runtime_state') == 'processing'
        and event.get('registered_relay_count', 0) > 0
    )

    assert processing_event['registered'] is True
    assert any(
        status['relay_runtime_state'] == 'processing' and status['request_count'] == 1
        for status in processing_event['relay_statuses']
    )
    assert sorted(MultiWorkRuntime.processed_relays) == [
        ('https://staging.token.place', 'req-b'),
        ('https://token.place', 'req-a'),
    ]
    assert MultiWorkRuntime.max_active_processing == 1
    for instance in MultiWorkRuntime.instances:
        assert instance.relay_client.endpoint_calls == [
            ('/api/v1/relay/responses', instance._processed[0])
        ]
    assert MultiWorkRuntime.instances[0].model_manager is MultiWorkRuntime.instances[1].model_manager
    assert MultiWorkRuntime.instances[0].crypto_manager is MultiWorkRuntime.instances[1].crypto_manager

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
    assert "model_path" not in payload
    assert payload["error_code"] == "desktop_compute_node_startup_failed"
    assert payload["context_tier"] == "8k-fast"
    assert payload["interpreter"] == sys.executable
    assert payload["import_root"] == "unknown"
    assert payload["log_file_path"] == "unknown"
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
    repo_utils = Path(__file__).resolve().parents[2] / 'utils'
    (utils_dir / '__init__.py').write_text(
        (repo_utils / '__init__.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (utils_dir / 'path_handling.py').write_text(
        (repo_utils / 'path_handling.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
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
    (utils_dir / 'context_profiles.py').write_text(
        (Path(__file__).resolve().parents[2] / 'utils' / 'context_profiles.py').read_text(encoding='utf-8'),
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
            '--context-tier',
            'unknown',
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
    started = next(event for event in events if event.get('type') == 'started')
    assert started.get('context_tier') == '8k-fast'
    assert any(event.get('type') == 'stopped' for event in events)
    assert "No module named 'utils'" not in stdout


def test_main_subprocess_emits_structured_error_when_context_profiles_missing(tmp_path):
    python_dir = tmp_path / 'bin' / 'resources' / 'python'
    import_root = tmp_path / 'bin' / 'resources' / '_up_' / '_up_'
    utils_dir = import_root / 'utils'
    python_dir.mkdir(parents=True)
    utils_dir.mkdir(parents=True)

    (python_dir / 'compute_node_bridge.py').write_text(
        MODULE_PATH.read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (python_dir / 'path_bootstrap.py').write_text(
        (MODULE_PATH.parent / 'path_bootstrap.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (python_dir / 'desktop_runtime_setup.py').write_text(
        'def desktop_gpu_runtime_failure_message(_mode, _runtime_setup):\n    return None\n'
        'def ensure_desktop_llama_runtime(_mode):\n'
        '    print("unexpected runtime setup", file=__import__("sys").stderr)\n'
        '    return {"selected_backend": "cpu"}\n'
        'def ensure_desktop_python_dependencies(*, repo_root=None):\n    return {"ok": "true"}\n'
        'def maybe_reexec_for_runtime_refresh(_runtime_setup, *, allow_reexec=True):\n    return None\n',
        encoding='utf-8',
    )
    (utils_dir / '__init__.py').write_text('', encoding='utf-8')

    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env['TOKEN_PLACE_PYTHON_IMPORT_ROOT'] = str(import_root)
    env['TOKENPLACE_OPERATOR_LOG_FILE'] = str(tmp_path / 'operator.log')
    proc = subprocess.run(
        [
            sys.executable,
            str(python_dir / 'compute_node_bridge.py'),
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'auto',
            '--context-tier',
            '64k-full',
            '--relay-url',
            'https://token.place',
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 1
    assert "unexpected runtime setup" in proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    payload = next(event for event in events if event.get('type') == 'error')
    assert payload['error_code'] == 'context_profiles_unavailable'
    assert payload['registered'] is False
    assert payload['context_tier'] == '64k-full'
    assert payload['interpreter'] == sys.executable
    assert payload['import_root'] == str(import_root)
    assert payload['log_file_path'] == str(tmp_path / 'operator.log')
    assert 'context profiles unavailable' in payload['message']
    assert 'model_path' not in payload


def test_ensure_runtime_import_paths_prefers_bundled_context_profiles_over_site_packages(tmp_path):
    python_dir = tmp_path / 'bin' / 'resources' / 'python'
    bundled_root = tmp_path / 'bin' / 'resources' / '_up_' / '_up_'
    bundled_utils = bundled_root / 'utils'
    site_packages = tmp_path / 'venv' / 'lib' / 'python3.11' / 'site-packages'
    site_utils = site_packages / 'utils'
    python_dir.mkdir(parents=True)
    bundled_utils.mkdir(parents=True)
    site_utils.mkdir(parents=True)

    (python_dir / 'path_bootstrap.py').write_text(
        (MODULE_PATH.parent / 'path_bootstrap.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (bundled_utils / '__init__.py').write_text('', encoding='utf-8')
    (bundled_utils / 'context_profiles.py').write_text(
        'SOURCE = "bundled"\n',
        encoding='utf-8',
    )
    (site_utils / '__init__.py').write_text('', encoding='utf-8')
    (site_utils / 'context_profiles.py').write_text(
        'SOURCE = "site-packages"\n',
        encoding='utf-8',
    )
    probe = python_dir / 'probe_import.py'
    probe.write_text(
        'import json, sys\n'
        'from path_bootstrap import ensure_runtime_import_paths\n'
        'ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)\n'
        'import utils.context_profiles as profiles\n'
        'print(json.dumps({"source": profiles.SOURCE, "origin": profiles.__file__}))\n',
        encoding='utf-8',
    )

    env = os.environ.copy()
    env['PYTHONPATH'] = str(site_packages)
    proc = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['source'] == 'bundled'
    assert Path(payload['origin']).resolve() == (bundled_utils / 'context_profiles.py').resolve()


def test_context_profile_error_after_dependency_preflight_fails_closed(monkeypatch, capsys):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    events = []

    def fake_dependency_preflight():
        events.append('dependency_preflight')
        return {'ok': 'true', 'import_root': '/runtime'}

    def fail_context_profiles():
        events.append('context_profiles')
        raise ModuleNotFoundError("No module named 'utils.context_profiles'")

    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_python_dependencies',
        fake_dependency_preflight,
    )
    monkeypatch.setattr(
        compute_node_bridge,
        '_load_context_profile_helpers',
        fail_context_profiles,
    )

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://relay.example',
        relay_urls=None,
        relay_port=None,
        context_tier='64k-full',
    )

    assert compute_node_bridge.run(args) == 1
    emitted = json.loads(capsys.readouterr().out.strip())
    assert events == ['dependency_preflight', 'context_profiles']
    assert emitted['error_code'] == 'context_profiles_unavailable'
    assert emitted['context_tier'] == '64k-full'
    assert emitted['registered'] is False
    assert 'model_path' not in emitted
    assert "No module named 'utils.context_profiles'" in emitted['last_error']


def test_context_profile_import_error_details_are_sanitized(monkeypatch, capsys):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)

    sensitive_detail = (
        "context load failed\n"
        "model_path=/secret/model.gguf prompt=plaintext decrypted=ciphertext key=abc "
        + "x" * 400
    )

    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_python_dependencies',
        lambda: {'ok': 'true', 'import_root': '/runtime'},
    )
    monkeypatch.setattr(
        compute_node_bridge,
        '_load_context_profile_helpers',
        lambda: (_ for _ in ()).throw(RuntimeError(sensitive_detail)),
    )

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://relay.example',
        relay_urls=None,
        relay_port=None,
        context_tier='64k-full',
    )

    assert compute_node_bridge.run(args) == 1
    emitted = json.loads(capsys.readouterr().out.strip())
    assert emitted['error_code'] == 'context_profiles_unavailable'
    assert emitted['registered'] is False
    assert '\n' not in emitted['last_error']
    assert len(emitted['last_error']) <= len('context profiles unavailable: ') + 240
    assert 'model_path' not in emitted['last_error']
    assert 'model.gguf' not in emitted['last_error']
    assert 'prompt' not in emitted['last_error']
    assert 'plaintext' not in emitted['last_error']
    assert 'decrypted' not in emitted['last_error']
    assert 'ciphertext' not in emitted['last_error']
    assert 'key=abc' not in emitted['last_error']


def test_dependency_preflight_failure_does_not_import_context_profiles(monkeypatch, capsys):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    calls = []

    def fake_dependency_preflight():
        calls.append('dependency_preflight')
        return {
            'ok': 'false',
            'action': 'requirements_missing',
            'missing': 'cryptography',
            'interpreter': sys.executable,
            'import_root': '/runtime',
            'detail': 'cryptography unavailable',
        }

    def fail_context_profiles():
        calls.append('context_profiles')
        raise AssertionError('context profiles unavailable')

    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_python_dependencies',
        fake_dependency_preflight,
    )
    monkeypatch.setattr(
        compute_node_bridge,
        '_load_context_profile_helpers',
        fail_context_profiles,
    )

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://relay.example',
        relay_urls=None,
        relay_port=None,
        context_tier='64k-full',
    )

    assert compute_node_bridge.run(args) == 1
    emitted = json.loads(capsys.readouterr().out.strip())
    assert calls == ['dependency_preflight']
    assert 'desktop runtime dependency preflight failed' in emitted['last_error']
    assert 'missing=cryptography' in emitted['last_error']
    assert 'context profiles unavailable' not in emitted['last_error']
    assert emitted['registered'] is False


def test_clean_first_launch_imports_context_profiles_after_dependency_preflight(monkeypatch):
    _reset_cancel_queue()
    observed = []
    real_import = __import__
    cryptography_available = {'value': False}

    def fake_dependency_preflight():
        observed.append('dependency_preflight')
        cryptography_available['value'] = True
        return {'ok': 'true', 'missing': '', 'action': 'already_satisfied'}

    def fake_import(name, *args, **kwargs):
        if name == 'cryptography' and not cryptography_available['value']:
            raise ModuleNotFoundError("No module named 'cryptography'")
        if name == 'utils.context_profiles':
            observed.append('context_profiles_import')
            assert cryptography_available['value'] is True
        return real_import(name, *args, **kwargs)

    _install_fake_runtime_module(monkeypatch)
    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_python_dependencies',
        fake_dependency_preflight,
    )
    monkeypatch.setattr('builtins.__import__', fake_import)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: True)

    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='auto',
        relay_url='https://relay.example',
        relay_urls=None,
        relay_port=None,
        context_tier='unknown',
    )

    assert compute_node_bridge.run(args) == 0
    assert observed[:2] == ['dependency_preflight', 'context_profiles_import']
    assert args.context_tier == '8k-fast'


def test_module_import_does_not_load_context_profiles_before_preflight(monkeypatch):
    real_import = __import__
    imported_context_profiles = False

    def fake_import(name, *args, **kwargs):
        nonlocal imported_context_profiles
        if name == 'utils.context_profiles':
            imported_context_profiles = True
            raise AssertionError('context profiles imported at module scope')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)

    module = ModuleType('compute_node_bridge_no_context_profiles')
    module.__file__ = str(MODULE_PATH)
    code = compile(MODULE_PATH.read_text(encoding='utf-8'), str(MODULE_PATH), 'exec')
    exec(code, module.__dict__)

    assert imported_context_profiles is False

    args = SimpleNamespace(
        mode='auto',
        relay_url='https://relay.example',
        relay_urls=None,
        context_tier='64k-full',
    )
    payload = module._structured_startup_error_payload(args, 'context profiles unavailable')

    assert payload['context_tier'] == '64k-full'
    assert payload['error_code'] == 'desktop_compute_node_startup_failed'
    assert 'model_path' not in payload


def test_utils_package_keeps_lazy_convenience_exports():
    import utils

    assert utils.get_temp_dir
    assert utils.get_model_manager
    assert utils.get_crypto_manager
    assert utils.RelayClient
    assert {
        'get_model_manager',
        'get_crypto_manager',
        'get_temp_dir',
        'RelayClient',
    } <= set(dir(utils))


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
        assert "stop" in events
        assert "warm-done" not in events[: events.index("stop") + 1]
        # Coarse guard: the behavioral assertions above prove shutdown did not
        # wait for warmup completion. This only catches regressions that block
        # on the full 1s warmup, without making the test depend on CI host speed.
        assert elapsed < 0.9
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
    assert "runtime_wait.exception" in output.err or "model_init.exception" in output.err
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


class PreRegistrationUnregisterRelayClient(FakeRelayClient):
    def __init__(self):
        self.stop_calls = 0
        self.unregister_calls = 0

    def stop(self):
        self.stop_calls += 1

    def unregister_from_relay(self):
        self.unregister_calls += 1
        return True


class PreRegistrationCancelRuntime(WarmingThenApiV1Runtime):
    last_instance = None

    def __init__(self, config):
        super().__init__(config)
        PreRegistrationCancelRuntime.last_instance = self
        self.relay_client = PreRegistrationUnregisterRelayClient()


def test_run_logs_unregister_skip_when_cancelled_before_registration(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch, runtime_cls=PreRegistrationCancelRuntime)

    def fake_stop_requested():
        instance = PreRegistrationCancelRuntime.last_instance
        return bool(instance is not None and instance.ready_started.is_set())

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '1')
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    assert compute_node_bridge.run(args) == 0

    runtime = PreRegistrationCancelRuntime.last_instance
    assert runtime.relay_client.unregister_calls == 0
    assert 'desktop.compute_node_bridge.unregister.skipped' in capsys.readouterr().err


def test_runtime_setup_diagnostics_are_logged_and_in_status_without_noisy_last_error(capsys, monkeypatch):
    _reset_cancel_queue()
    _install_fake_runtime_module(monkeypatch)
    runtime_setup = {
        'selected_backend': 'cpu',
        'detected_device': 'cpu',
        'runtime_action': 'metal_cpu_fallback',
        'interpreter': '/Applications/TokenPlace.app/Contents/Resources/python/bin/python',
        'python_version': '3.12.4',
        'prefix': '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework',
        'base_prefix': '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework',
        'dependency_target': '/Users/alice/Library/Application Support/token.place/.token_place_desktop_site',
        'pip_version': 'pip 24.0',
        'install_command_summary': 'python -m pip install --target /deps llama-cpp-python',
        'install_backend': 'metal',
        'cmake_args': '-DGGML_METAL=on -DGGML_NATIVE=off',
        'pip_stdout_tail': 'building wheel',
        'pip_stderr_tail': 'Metal headers missing',
        'llama_module_path': '/deps/llama_cpp/__init__.py',
        'fallback_reason': 'Metal failed; using CPU runtime',
    }
    monkeypatch.setattr(compute_node_bridge, 'ensure_desktop_llama_runtime', lambda _mode: runtime_setup)
    monkeypatch.setattr(compute_node_bridge, 'desktop_gpu_runtime_failure_message', lambda _mode, _setup: None)
    stop_counter = {'count': 0}

    def fake_stop_requested():
        stop_counter['count'] += 1
        return stop_counter['count'] > 2

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    args = SimpleNamespace(model='/tmp/model.gguf', mode='auto', relay_url='https://token.place', relay_port=None)

    assert compute_node_bridge.run(args) == 0

    output = capsys.readouterr()
    stderr = output.err
    assert 'desktop.runtime_setup ' in stderr
    for marker in (
        'interpreter=/Applications/TokenPlace.app/Contents/Resources/python/bin/python',
        'python_version=3.12.4',
        'prefix=/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework',
        'base_prefix=/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework',
        'dependency_target=/Users/alice/Library/Application Support/token.place/.token_place_desktop_site',
        'pip=pip 24.0',
        'install_command=python -m pip install --target /deps llama-cpp-python',
        'install_backend=metal',
        'cmake_args=-DGGML_METAL=on -DGGML_NATIVE=off',
        'pip_stdout_tail=building wheel',
        'pip_stderr_tail=Metal headers missing',
        'llama_module_path=/deps/llama_cpp/__init__.py',
        'fallback_reason=Metal failed; using CPU runtime',
    ):
        assert marker in stderr
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    started = events[0]
    assert started['last_error'] is None
    assert started['runtime_action'] == 'metal_cpu_fallback'
    assert started['base_prefix'].startswith('/Library/Developer')
    assert started['pip_version'] == 'pip 24.0'
    assert started['install_command_summary'].startswith('python -m pip install')
    assert started['cmake_args'] == '-DGGML_METAL=on -DGGML_NATIVE=off'
    assert started['pip_stderr_tail'] == 'Metal headers missing'

def test_run_keeps_registration_false_after_runtime_health_failure(capsys, monkeypatch):
    from utils.processing_result import RelayProcessingResult

    _reset_cancel_queue()

    class ReRegisterAfterFailureRuntime(ApiV1Runtime):
        last_instance = None

        def __init__(self, config):
            super().__init__(config)
            ReRegisterAfterFailureRuntime.last_instance = self
            self._responses.append({'next_ping_in_x_seconds': 0})
            self.poll_count = 0

        def register_and_poll_once(self):
            self.poll_count += 1
            return super().register_and_poll_once()

        def process_relay_request_result(self, payload):
            self._processed.append(payload)
            return RelayProcessingResult(
                inference_succeeded=False,
                submitted=True,
                safe_error_code="compute_node_internal_error",
                runtime_healthy=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=ReRegisterAfterFailureRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    monkeypatch.setattr(
        compute_node_bridge,
        'stop_requested',
        lambda: (
            ReRegisterAfterFailureRuntime.last_instance is not None
            and ReRegisterAfterFailureRuntime.last_instance.poll_count >= 2
        ),
    )
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0
    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    status_events = [event for event in events if event.get('type') == 'status']
    failed_statuses = [
        event for event in status_events if event.get('relay_runtime_state') == 'failed'
    ]
    assert ReRegisterAfterFailureRuntime.last_instance.poll_count == 1
    assert failed_statuses
    assert all(event['registered'] is False for event in failed_statuses)
    assert not any(
        event['registered'] is True and event.get('relay_runtime_state') == 'failed'
        for event in status_events
    )


def test_run_error_envelope_submission_is_not_success_marker(capsys, monkeypatch):
    from utils.processing_result import RelayProcessingResult

    _reset_cancel_queue()

    class ErrorEnvelopeRuntime(ApiV1Runtime):
        def process_relay_request_result(self, payload):
            self._processed.append(payload)
            return RelayProcessingResult(
                inference_succeeded=False,
                submitted=True,
                safe_error_code="compute_node_internal_error",
                runtime_healthy=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=ErrorEnvelopeRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "0")
    monkeypatch.setattr(
        compute_node_bridge,
        'stop_requested',
        lambda: bool(ErrorEnvelopeRuntime.last_instance._processed),
    )
    args = SimpleNamespace(model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None)

    assert compute_node_bridge.run(args) == 0
    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.api_v1_e2ee.response_submitted' not in output.err
    assert 'desktop.compute_node_bridge.api_v1_e2ee.error_envelope_submitted' in output.err
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    status_events = [event for event in events if event.get('type') == 'status']
    assert status_events[-1]['registered'] is False
    assert status_events[-1]['relay_runtime_state'] == 'failed'
    assert status_events[-1]['last_error'] == 'relay request failed: compute_node_internal_error'


def test_multi_relay_shared_dead_worker_uses_one_recovery_and_restores_registrations(capsys, monkeypatch):
    _reset_cancel_queue()

    class SharedManager(FakeModelManager):
        def __init__(self):
            super().__init__()
            self.dead = True
            self.recovery_calls = 0
            self.process_calls = 0

    shared_manager = SharedManager()

    class RelayClient(FakeRelayClient):
        def __init__(self, relay_url):
            self.relay_url = relay_url
            self.unregister_calls = 0
            self.start_calls = 0

        def unregister_from_relay(self):
            self.unregister_calls += 1
            return True

        def start(self):
            self.start_calls += 1

    class MultiRelayRecoveryRuntime(FakeRuntime):
        instances = []

        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            self.model_manager = model_manager or shared_manager
            self.crypto_manager = crypto_manager or object()
            self.relay_client = RelayClient(config.relay_url)
            self._sent_payload = False
            self._processed = []
            MultiRelayRecoveryRuntime.instances.append(self)

        def start_relay_session(self):
            self.relay_client.start()

        def ensure_api_v1_runtime_ready(self):
            self.model_manager.recovery_calls += 1
            self.model_manager.dead = False
            return True

        def register_and_poll_once(self):
            if not self._sent_payload:
                self._sent_payload = True
                return {
                    'protocol': 'tokenplace_api_v1_relay_e2ee',
                    'version': 1,
                    'request_id': f"req-{self.relay_client.relay_url.rsplit('/', 1)[-1]}",
                    'client_public_key': 'client-key',
                    'chat_history': 'ciphertext',
                    'cipherkey': 'key',
                    'iv': 'iv',
                    'next_ping_in_x_seconds': 0,
                }
            return {'next_ping_in_x_seconds': 0}

        def process_relay_request_result(self, payload):
            self._processed.append(payload)
            self.model_manager.process_calls += 1
            if self.model_manager.dead:
                return SimpleNamespace(
                    inference_succeeded=False,
                    submitted=True,
                    safe_error_code='compute_node_internal_error',
                    runtime_healthy=False,
                    recovery_attempted=True,
                    recovery_succeeded=False,
                )
            return SimpleNamespace(
                inference_succeeded=True,
                submitted=True,
                safe_error_code=None,
                runtime_healthy=True,
                recovery_attempted=False,
                recovery_succeeded=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=MultiRelayRecoveryRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '2')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '0.01')
    stop_calls = {'count': 0}

    def fake_stop_requested():
        stop_calls['count'] += 1
        return shared_manager.dead is False and stop_calls['count'] > 20

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', fake_stop_requested)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=['https://relay-b.example'],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert shared_manager.recovery_calls == 1
    assert output.err.count('desktop.compute_node_bridge.recovery.start') == 1
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    recovering_events = [event for event in events if event.get('relay_runtime_state') == 'recovering']
    assert recovering_events
    assert all(event['registered'] is False for event in recovering_events)
    ready_registered = [
        event for event in events
        if event.get('type') == 'status' and event.get('registered_relay_count') == 2
    ]
    assert ready_registered
    assert all(instance.relay_client.unregister_calls >= 1 for instance in MultiRelayRecoveryRuntime.instances)
    assert 'ciphertext' not in output.err


def test_multi_relay_recovery_exhaustion_reports_none_registered_or_ready(capsys, monkeypatch):
    _reset_cancel_queue()

    class FailingRecoveryRuntime(ApiV1Runtime):
        instances = []

        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client = FakeRelayClientRouting()
            self.relay_client.relay_url = config.relay_url
            self.relay_client.unregister_from_relay = lambda: True
            FailingRecoveryRuntime.instances.append(self)

        def ensure_api_v1_runtime_ready(self):
            return False

        def process_relay_request_result(self, _payload):
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=True,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=True,
                recovery_succeeded=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=FailingRecoveryRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '1')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '0')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: False)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=['https://relay-b.example'],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 1
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    error_event = next(event for event in events if event.get('type') == 'error')
    assert error_event['relay_runtime_state'] == 'failed'
    assert error_event['registered'] is False
    assert error_event['registered_relay_count'] == 0
    assert all(not status['registered'] for status in error_event['relay_statuses'])
    assert 'shared model runtime recovery exhausted' in error_event['message']


def test_multi_relay_network_failure_does_not_trigger_shared_model_recovery(capsys, monkeypatch):
    _reset_cancel_queue()
    calls = {'warm': 0, 'poll': 0}

    class NetworkFailureRuntime(FakeRuntime):
        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client = FakeRelayClient()
            self.relay_client.relay_url = config.relay_url

        def ensure_api_v1_runtime_ready(self):
            calls['warm'] += 1
            return True

        def register_and_poll_once(self):
            calls['poll'] += 1
            if self.relay_client.relay_url.endswith('a.example'):
                return {'error': 'temporary relay outage', 'next_ping_in_x_seconds': 0}
            return {'next_ping_in_x_seconds': 0}

    _install_fake_runtime_module(monkeypatch, runtime_cls=NetworkFailureRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: calls['poll'] > 3)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=['https://relay-b.example'],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert calls['warm'] == 0
    assert 'desktop.compute_node_bridge.recovery.start' not in output.err
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    assert any(event.get('registered_relay_count', 0) > 0 for event in events)


def test_multi_relay_stop_during_recovery_backoff_exits_without_threads(capsys, monkeypatch):
    _reset_cancel_queue()
    attempts = {'count': 0}
    sleep_calls = {'count': 0}

    class BackoffRecoveryRuntime(ApiV1Runtime):
        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client = FakeRelayClientRouting()
            self.relay_client.relay_url = config.relay_url
            self.relay_client.unregister_from_relay = lambda: True

        def ensure_api_v1_runtime_ready(self):
            attempts['count'] += 1
            return False

        def process_relay_request_result(self, _payload):
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=True,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=True,
                recovery_succeeded=False,
            )

    def cancel_sleep(_seconds):
        sleep_calls['count'] += 1
        compute_node_bridge._stop_requested_latched.set()
        return True

    _install_fake_runtime_module(monkeypatch, runtime_cls=BackoffRecoveryRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '2')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '1')
    monkeypatch.setattr(compute_node_bridge, '_sleep_with_cancel', cancel_sleep)
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: compute_node_bridge._stop_requested_latched.is_set())
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=['https://relay-b.example'],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert attempts['count'] == 1
    assert sleep_calls['count'] >= 1
    assert 'desktop.compute_node_bridge.recovery.cancelled_during_backoff' in output.err
    assert not any(
        thread.name.startswith('tokenplace-relay-poller') and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_multi_relay_recovery_logs_unregister_failure_and_retries_after_exception(
    capsys,
    monkeypatch,
):
    _reset_cancel_queue()
    recovery_calls = {'count': 0}

    class RaisingUnregisterClient(FakeRelayClient):
        def __init__(self, relay_url):
            self.relay_url = relay_url
            self._api_v1_registered_relays = {relay_url}
            self.unregister_calls = 0
            self.start_calls = 0

        def unregister_from_relay(self):
            self.unregister_calls += 1
            raise TimeoutError('unregister timed out')

        def start(self):
            self.start_calls += 1

    class ExceptionThenSuccessRuntime(ApiV1Runtime):
        instances = []

        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client = RaisingUnregisterClient(config.relay_url)
            ExceptionThenSuccessRuntime.instances.append(self)

        def ensure_api_v1_runtime_ready(self):
            recovery_calls['count'] += 1
            if recovery_calls['count'] == 1:
                raise RuntimeError('first warm-load failed')
            return True

        def process_relay_request_result(self, _payload):
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=False,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=True,
                recovery_succeeded=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=ExceptionThenSuccessRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '2')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '0')
    stop_checks = {'count': 0}

    def stop_after_recovery():
        stop_checks['count'] += 1
        return recovery_calls['count'] >= 2 and stop_checks['count'] > 6

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_recovery)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=['https://relay-b.example'],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert recovery_calls['count'] == 2
    assert output.err.count('desktop.compute_node_bridge.recovery.attempt_exception') == 1
    assert output.err.count('desktop.compute_node_bridge.recovery.unregister.failed') >= 1
    assert all(instance.relay_client.start_calls >= 1 for instance in ExceptionThenSuccessRuntime.instances)


def test_recovery_logs_redact_payload_keys_and_paths(capsys, monkeypatch):
    _reset_cancel_queue()
    recovery_calls = {'count': 0}
    sentinels = {
        'PROMPT_SENTINEL',
        'OUTPUT_SENTINEL',
        'PRIVATE_KEY',
        'FULL_PUBLIC_KEY',
        '/tmp/sentinel-model-path.gguf',
        '/tmp/sentinel-runtime-path',
        'path=/tmp/sentinel-path',
    }

    class SensitiveModelManager(FakeModelManager):
        model_path = '/tmp/sentinel-model-path.gguf'

        def worker_lifecycle_status(self):
            return {
                "worker_state": "recovering",
                "worker_generation": 7,
                "worker_restart_count": 1,
                "worker_alive": False,
                "last_worker_error_code": "worker_dead",
                "last_worker_exit_code": None,
                "last_worker_restart_at_ms": None,
            }

    class SensitiveRecoveryRuntime(ApiV1Runtime):
        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or SensitiveModelManager()
            self.crypto_manager = SimpleNamespace(
                private_key='PRIVATE_KEY',
                full_public_key='FULL_PUBLIC_KEY',
            )
            self.relay_client = FakeRelayClientRouting()
            self.relay_client.relay_url = config.relay_url

        def ensure_api_v1_runtime_ready(self):
            recovery_calls['count'] += 1
            if recovery_calls['count'] == 1:
                raise RuntimeError(
                    'PROMPT_SENTINEL OUTPUT_SENTINEL decrypted generated output '
                    'model_path=/tmp/sentinel-model-path.gguf '
                    'runtime_path=/tmp/sentinel-runtime-path path=/tmp/sentinel-path'
                )
            return True

        def process_relay_request_result(self, _payload):
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=True,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=True,
                recovery_succeeded=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=SensitiveRecoveryRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '2')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '0')
    stop_checks = {'count': 0}

    def stop_after_recovery():
        stop_checks['count'] += 1
        return recovery_calls['count'] >= 2 and stop_checks['count'] > 6

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_recovery)
    args = SimpleNamespace(
        model='/tmp/sentinel-model-path.gguf',
        mode='cpu',
        relay_url='https://user:PRIVATE_KEY@relay-a.example/path?query=FULL_PUBLIC_KEY#fragment',
        relay_urls=[],
        relay_port=None,
    )

    assert compute_node_bridge.run(args) == 0

    output = capsys.readouterr()
    assert 'desktop.compute_node_bridge.recovery.start' in output.err
    assert 'desktop.compute_node_bridge.recovery.attempt_exception' in output.err
    assert 'desktop.compute_node_bridge.recovery.succeeded' in output.err
    lifecycle_log_lines = '\n'.join(
        line for line in output.err.splitlines()
        if 'desktop.compute_node_bridge.recovery.' in line
        or 'desktop.compute_node_bridge.worker.' in line
    )
    for sentinel in sentinels:
        assert sentinel not in lifecycle_log_lines
    assert 'decrypted' not in lifecycle_log_lines
    assert 'generated output' not in lifecycle_log_lines
    assert 'model_path' not in lifecycle_log_lines
    assert 'runtime_path' not in lifecycle_log_lines


def test_multi_relay_recovery_cancelled_before_first_attempt(capsys, monkeypatch):
    _reset_cancel_queue()
    cancel_recovery = {'value': False}
    recovery_calls = {'count': 0}

    class CancelledRecoveryRuntime(ApiV1Runtime):
        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client = FakeRelayClientRouting()
            self.relay_client.relay_url = config.relay_url
            self.relay_client._api_v1_registered_relays = {config.relay_url}
            self.relay_client.unregister_from_relay = lambda: True

        def ensure_api_v1_runtime_ready(self):
            recovery_calls['count'] += 1
            return True

        def process_relay_request_result(self, _payload):
            cancel_recovery['value'] = True
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=False,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=True,
                recovery_succeeded=False,
            )

    def stop_during_recovery():
        return cancel_recovery['value']

    _install_fake_runtime_module(monkeypatch, runtime_cls=CancelledRecoveryRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '2')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '0')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_during_recovery)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=[],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert recovery_calls['count'] == 0
    assert 'desktop.compute_node_bridge.recovery.cancelled' in output.err


def test_multi_relay_recovery_stops_unregistered_client_without_unregister_hook(capsys, monkeypatch):
    _reset_cancel_queue()
    recovery_calls = {'count': 0}

    class StopOnlyRelayClient(FakeRelayClient):
        def __init__(self, relay_url):
            self.relay_url = relay_url
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1
            raise RuntimeError('stop failed')

    class StopOnlyRecoveryRuntime(ApiV1Runtime):
        instances = []

        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client = StopOnlyRelayClient(config.relay_url)
            StopOnlyRecoveryRuntime.instances.append(self)

        def ensure_api_v1_runtime_ready(self):
            recovery_calls['count'] += 1
            return True

        def process_relay_request_result(self, _payload):
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=False,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=True,
                recovery_succeeded=False,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=StopOnlyRecoveryRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS', '1')
    monkeypatch.setenv('TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS', '0')
    stop_checks = {'count': 0}

    def stop_after_recovery():
        stop_checks['count'] += 1
        return recovery_calls['count'] >= 1 and stop_checks['count'] > 5

    monkeypatch.setattr(compute_node_bridge, 'stop_requested', stop_after_recovery)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://relay-a.example',
        relay_urls=[],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert recovery_calls['count'] == 1
    assert StopOnlyRecoveryRuntime.instances[0].relay_client.stop_calls >= 1
    assert 'desktop.compute_node_bridge.recovery.stop_failed' in output.err


@pytest.mark.parametrize(
    ('recovery_succeeded', 'expected_state'),
    [
        (True, 'ready'),
        (False, 'failed'),
    ],
)
def test_api_v1_unhealthy_result_without_recovery_attempt_sets_terminal_state(
    capsys,
    monkeypatch,
    recovery_succeeded,
    expected_state,
):
    _reset_cancel_queue()
    processed = {'count': 0}

    class UnhealthyTerminalRuntime(ApiV1Runtime):
        def __init__(self, config, *_, model_manager=None, crypto_manager=None):
            super().__init__(config)
            self.model_manager = model_manager or FakeModelManager()
            self.crypto_manager = crypto_manager or object()
            self.relay_client.relay_url = config.relay_url

        def process_relay_request_result(self, _payload):
            processed['count'] += 1
            return SimpleNamespace(
                inference_succeeded=False,
                submitted=False,
                safe_error_code='compute_node_internal_error',
                runtime_healthy=False,
                recovery_attempted=False,
                recovery_succeeded=recovery_succeeded,
            )

    _install_fake_runtime_module(monkeypatch, runtime_cls=UnhealthyTerminalRuntime)
    monkeypatch.setenv('TOKENPLACE_DESKTOP_WARM_LOAD', '0')
    monkeypatch.setattr(compute_node_bridge, 'stop_requested', lambda: processed['count'] >= 1)
    args = SimpleNamespace(
        model='/tmp/model.gguf',
        mode='cpu',
        relay_url='https://token.place',
        relay_urls=[],
        relay_port=None,
    )

    status = compute_node_bridge.run(args)

    output = capsys.readouterr()
    assert status == 0
    assert processed['count'] == 1
    assert 'desktop.compute_node_bridge.api_v1_e2ee.error_response.skipped' in output.err
    events = [json.loads(line) for line in output.out.splitlines() if line.strip()]
    status_events = [event for event in events if event.get('type') == 'status']
    assert status_events[-1]['relay_runtime_state'] == expected_state


def test_desktop_runtime_context_shim_does_not_swallow_internal_type_error(monkeypatch):
    def _runtime_with_internal_type_error(_mode, *, context_tier):
        raise TypeError('nested helper got unexpected keyword argument')

    monkeypatch.setattr(
        compute_node_bridge,
        'ensure_desktop_llama_runtime',
        _runtime_with_internal_type_error,
    )

    with pytest.raises(TypeError, match='nested helper'):
        compute_node_bridge._ensure_desktop_llama_runtime_for_context('auto', '64k-full')


def test_desktop_runtime_context_shim_uses_legacy_signature(monkeypatch):
    calls = []

    def _legacy_runtime(mode):
        calls.append(mode)
        return {'runtime_action': 'legacy'}

    monkeypatch.setattr(compute_node_bridge, 'ensure_desktop_llama_runtime', _legacy_runtime)

    assert compute_node_bridge._ensure_desktop_llama_runtime_for_context('auto', '64k-full') == {
        'runtime_action': 'legacy'
    }
    assert calls == ['auto']


def test_desktop_runtime_context_shim_uses_legacy_call_when_signature_uninspectable(monkeypatch):
    calls = []

    def _legacy_runtime(mode):
        calls.append(mode)
        return {'runtime_action': 'legacy_uninspectable'}

    monkeypatch.setattr(compute_node_bridge, 'ensure_desktop_llama_runtime', _legacy_runtime)
    monkeypatch.setattr(
        compute_node_bridge.inspect,
        'signature',
        lambda _target: (_ for _ in ()).throw(ValueError('signature unavailable')),
    )

    assert compute_node_bridge._ensure_desktop_llama_runtime_for_context('auto', '64k-full') == {
        'runtime_action': 'legacy_uninspectable'
    }
    assert calls == ['auto']


def test_safe_readiness_diagnostics_allowlists_scalar_fields_and_drops_unsafe_fields():
    manager = SimpleNamespace(
        last_compute_diagnostics={
            'api_v1_readiness_result': 'failed',
            'api_v1_readiness_error_code': 'compute_node_internal_error',
            'api_v1_readiness_error_reason': 'runtime_completion_smoke_plain_completion_worker_exception',
            'api_v1_readiness_completion_smoke_method': 'create_completion_keyword_prompt',
            'api_v1_readiness_completion_smoke_attempted_generation_kwargs': 'max_tokens,prompt',
            'api_v1_readiness_completion_smoke_safe_summary': 'RuntimeError:worker_timeout',
            'api_v1_readiness_completion_smoke_plain_completion_create_completion_callable': True,
            'api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg': False,
            'api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs': True,
            'api_v1_readiness_completion_smoke_plain_completion_reset_after_failure_count': 2,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted': True,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_token_count': 3,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method': 'llama.tokenize',
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special': True,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category': 'prompt_tokenization_failure',
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count': 3,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids': 'tokenize_add_bos_false_special_false,tokenize_add_bos_false_no_special,tokenize_add_bos_false_special_true',
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts': '3,3,4',
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values': 'false,none,true',
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant': 'tokenize_add_bos_false_no_special',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_methods': 'create_completion_keyword_prompt,create_completion_keyword_token_ids,create_chat_completion_qwen_non_thinking',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_categories': 'prompt_eval_failure,prompt_eval_decode_failure,',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_exception_types': 'RuntimeError,RuntimeError,',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_safe_summaries': 'RuntimeError:prompt_eval_failure,RuntimeError:prompt_eval_decode_failure,',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_rejected_kwargs': ',,',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_result_shapes': ',,choices_message',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_tokenization_variants': ',tokenize_add_bos_false_no_special,',
            'api_v1_readiness_completion_smoke_plain_completion_attempt_count': 3,
            'api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_attempted': True,
            'api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_supported': True,
            'api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_succeeded': False,
            'api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_rejected_kwarg': '',
            'api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_category': 'thinking_leaked',
            'api_v1_readiness_completion_smoke_plain_completion_eval_return_code': 1,
            'api_v1_readiness_completion_smoke_exception_type': 'RuntimeError',
            'api_v1_readiness_completion_smoke_result_shape': 'dict_malformed',
            'api_v1_readiness_yarn_requested_context_tokens': 65536,
            'api_v1_readiness_yarn_original_context_tokens': 32768,
            'api_v1_readiness_yarn_context_multiplier': 2.0,
            'api_v1_readiness_yarn_rope_freq_scale': 0.5,
            'api_v1_readiness_yarn_ext_factor_overridden': False,
            'api_v1_readiness_yarn_rope_scaling_type_source': 'enum',
            'api_v1_readiness_yarn_configuration_valid': True,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count': 3,
            'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special': None,
            'prompt': 'SECRET_PROMPT',
            'rendered_prompt': '<|im_start|>SECRET_PROMPT',
            'assistant_output': 'SECRET_OUTPUT',
            'decrypted_payload': 'SECRET_PAYLOAD',
            'ciphertext': 'SECRET_CIPHERTEXT_INTERNALS',
            'token_ids': [1, 2, 3],
            'output': 'SECRET_OUTPUT',
            'key': 'SECRET_KEY',
            'tool_args': {'secret': True},
            'api_v1_readiness_completion_smoke_internal_reason': 'bad value with spaces and secret prompt',
        }
    )

    safe = compute_node_bridge._safe_readiness_diagnostics(manager)

    assert safe['api_v1_readiness_result'] == 'failed'
    assert safe['api_v1_readiness_completion_smoke_method'] == 'create_completion_keyword_prompt'
    assert safe['api_v1_readiness_completion_smoke_attempted_generation_kwargs'] == 'max_tokens,prompt'
    assert safe['api_v1_readiness_completion_smoke_safe_summary'] == 'RuntimeError:worker_timeout'
    assert safe['api_v1_readiness_completion_smoke_plain_completion_create_completion_callable'] is True
    assert safe['api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg'] is False
    assert safe['api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs'] is True
    assert safe['api_v1_readiness_completion_smoke_plain_completion_reset_after_failure_count'] == 2
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted'] is True
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_token_count'] == 3
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method'] == 'llama.tokenize'
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special'] is True
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category'] == 'prompt_tokenization_failure'
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count'] == 3
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values'] == 'false,none,true'
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant'] == 'tokenize_add_bos_false_no_special'
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count'] == 3
    assert safe['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special'] is None
    assert safe['api_v1_readiness_yarn_requested_context_tokens'] == 65536
    assert safe['api_v1_readiness_yarn_original_context_tokens'] == 32768
    assert safe['api_v1_readiness_yarn_context_multiplier'] == 2.0
    assert safe['api_v1_readiness_yarn_rope_freq_scale'] == 0.5
    assert safe['api_v1_readiness_yarn_ext_factor_overridden'] is False
    assert safe['api_v1_readiness_yarn_rope_scaling_type_source'] == 'enum'
    assert safe['api_v1_readiness_yarn_configuration_valid'] is True
    assert safe['api_v1_readiness_completion_smoke_plain_completion_attempt_methods'].endswith('create_chat_completion_qwen_non_thinking')
    assert safe['api_v1_readiness_completion_smoke_plain_completion_attempt_count'] == 3
    assert safe['api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_attempted'] is True
    assert safe['api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_supported'] is True
    assert safe['api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_succeeded'] is False
    assert safe['api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_category'] == 'thinking_leaked'
    assert safe['api_v1_readiness_completion_smoke_plain_completion_eval_return_code'] == 1
    dumped = json.dumps(safe)
    assert 'SECRET_' not in dumped
    assert 'prompt text' not in dumped
    assert 'api_v1_readiness_completion_smoke_internal_reason' not in safe


def test_warm_load_failure_stderr_includes_safe_readiness_diagnostics(capsys, monkeypatch):
    _reset_cancel_queue()

    class WarmLoadDiagnosticRuntime(ApiV1Runtime):
        def ensure_api_v1_runtime_ready(self):
            self.model_manager.last_compute_diagnostics = {
                'api_v1_readiness_completion_smoke_method': 'create_completion_keyword_prompt',
                'api_v1_readiness_completion_smoke_generation_exception_category': 'worker_exception',
                'api_v1_readiness_completion_smoke_exception_type': 'LlamaCppInferenceRequestError',
                'api_v1_readiness_completion_smoke_safe_summary': 'plain_completion_worker_exception',
                'api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg': True,
                'api_v1_readiness_yarn_requested_context_tokens': 65536,
                'api_v1_readiness_yarn_original_context_tokens': 32768,
                'api_v1_readiness_yarn_context_multiplier': 2.0,
                'api_v1_readiness_yarn_rope_freq_scale': 0.5,
                'api_v1_readiness_yarn_ext_factor_overridden': False,
                'api_v1_readiness_yarn_rope_scaling_type_source': 'enum',
                'api_v1_readiness_yarn_configuration_valid': True,
                'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count': 28,
                'api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special': True,
                'prompt': 'SECRET_PROMPT',
                'api_v1_readiness_completion_smoke_internal_reason': 'unsafe free text prompt',
            }
            return False

    _install_fake_runtime_module(monkeypatch, runtime_cls=WarmLoadDiagnosticRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    assert compute_node_bridge.run(args) == 1
    err = capsys.readouterr().err

    assert "desktop.compute_node_bridge.api_v1_readiness.safe_diagnostics" in err
    assert "api_v1_readiness_completion_smoke_method=create_completion_keyword_prompt" in err
    assert "api_v1_readiness_completion_smoke_generation_exception_category=worker_exception" in err
    assert "api_v1_readiness_completion_smoke_exception_type=LlamaCppInferenceRequestError" in err
    assert "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg=true" in err
    assert "api_v1_readiness_yarn_requested_context_tokens=65536" in err
    assert "api_v1_readiness_yarn_original_context_tokens=32768" in err
    assert "api_v1_readiness_yarn_context_multiplier=2.0" in err
    assert "api_v1_readiness_yarn_rope_freq_scale=0.5" in err
    assert "api_v1_readiness_yarn_ext_factor_overridden=false" in err
    assert "api_v1_readiness_yarn_configuration_valid=true" in err
    assert "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count=28" in err
    assert "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special=true" in err
    assert "SECRET_PROMPT" not in err
    assert "unsafe free text prompt" not in err


def test_safe_readiness_stderr_renders_null_diagnostic_values(capsys):
    manager = SimpleNamespace(
        last_compute_diagnostics={
            'api_v1_readiness_error_code': None,
            'api_v1_readiness_completion_smoke_exception_type': 'RuntimeError',
        }
    )

    compute_node_bridge._emit_safe_readiness_diagnostics_stderr(manager)

    err = capsys.readouterr().err
    assert "api_v1_readiness_error_code=null" in err
    assert "api_v1_readiness_completion_smoke_exception_type=RuntimeError" in err


def test_warm_load_failure_stderr_marks_safe_readiness_diagnostics_unavailable(capsys, monkeypatch):
    _reset_cancel_queue()

    class WarmLoadNoDiagnosticRuntime(ApiV1Runtime):
        def ensure_api_v1_runtime_ready(self):
            self.model_manager.last_compute_diagnostics = None
            return False

    _install_fake_runtime_module(monkeypatch, runtime_cls=WarmLoadNoDiagnosticRuntime)
    monkeypatch.setenv("TOKENPLACE_DESKTOP_WARM_LOAD", "1")
    args = SimpleNamespace(
        model='/tmp/model.gguf', mode='cpu', relay_url='https://token.place', relay_port=None
    )

    assert compute_node_bridge.run(args) == 1
    err = capsys.readouterr().err

    assert (
        "desktop.compute_node_bridge.api_v1_readiness.safe_diagnostics unavailable=true"
        in err
    )
