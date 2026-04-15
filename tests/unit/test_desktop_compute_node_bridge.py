"""Unit tests for the desktop compute-node bridge."""

import importlib.util
import json
import os
import queue
import subprocess
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
        self.requested_compute_mode = 'auto'
        self.last_compute_diagnostics = None


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


class IncompatibleRelayRuntime(FakeRuntime):
    def __init__(self, _config):
        self.model_manager = FakeModelManager()
        self.relay_client = FakeRelayClient()
        self._responses = [{'relay_version': 'outdated'}]
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
    module.is_legacy_relay_payload = (
        lambda payload: {"client_public_key", "chat_history", "cipherkey", "iv"}.issubset(payload)
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
    started = events[0]
    assert started['offloaded_layers'] == 0
    assert started['kv_cache_device'] == 'cpu'
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


def test_run_disables_runtime_reexec_to_avoid_pre_startup_exit(capsys, monkeypatch):
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
    assert reexec_flags == [False]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'stopped'


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


def test_run_reports_error_when_legacy_relay_request_processing_fails(capsys, monkeypatch):
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
    assert status_events[0]['registered'] is True
    assert status_events[0]['last_error'] == 'failed to process relay request'


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
    module.is_legacy_relay_payload = lambda _payload: False
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
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload['type'] == 'error'
    assert payload['message'].startswith(
        'compute-node bridge exited before emitting a startup event:'
    )


def test_main_normalizes_mode_before_run(monkeypatch):
    captured = {}

    def fake_run(args):
        captured['mode'] = args.mode
        return 0

    module = ModuleType('utils.compute_node_runtime')
    module.normalize_compute_mode = lambda mode: {'cuda': 'gpu'}.get(str(mode).lower(), 'auto')
    monkeypatch.setitem(sys.modules, 'utils.compute_node_runtime', module)
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
        ['compute_node_bridge.py', '--model', '/tmp/model.gguf', '--mode', 'unsupported'],
    )

    status = compute_node_bridge.main()
    assert status == 0
    assert captured['mode'] == 'auto'


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


def is_legacy_relay_payload(_payload):
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


def test_module_level_fallback_when_desktop_runtime_setup_is_missing(monkeypatch, tmp_path):
    module_path = tmp_path / 'compute_node_bridge.py'
    module_path.write_text(MODULE_PATH.read_text(encoding='utf-8'), encoding='utf-8')

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'desktop_runtime_setup':
            raise ModuleNotFoundError("No module named 'desktop_runtime_setup'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)

    spec = importlib.util.spec_from_file_location('compute_node_bridge_no_runtime_setup', module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    setup = module.ensure_desktop_llama_runtime('auto')
    assert setup['runtime_action'] == 'unavailable'
    assert 'module missing' in setup['fallback_reason']
