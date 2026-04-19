"""Unit tests for the desktop NDJSON inference sidecar."""

import importlib.util
import json
import queue
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'src-tauri'
    / 'python'
    / 'inference_sidecar.py'
)
SPEC = importlib.util.spec_from_file_location('desktop_inference_sidecar', MODULE_PATH)
inference_sidecar = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(inference_sidecar)


class FakeConfig:
    def get(self, _key, default=None):
        return default


class FakeLlm:
    def create_chat_completion(self, **kwargs):
        if kwargs.get('stream') is False:
            return {'choices': [{'message': {'content': 'fallback response'}}]}

        return iter(
            [
                {'choices': [{'delta': {'content': 'Hello'}, 'finish_reason': None}]},
                {'choices': [{'delta': {'content': ' world'}, 'finish_reason': None}]},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            ]
        )


class FakeStreamNoContentLlm:
    def create_chat_completion(self, **kwargs):
        if kwargs.get('stream') is False:
            return {'choices': [{'message': {'content': 'fallback response'}}]}

        return iter(
            [
                {'choices': [{'delta': {}, 'finish_reason': None}]},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            ]
        )


class FakeManager:
    def __init__(self, llm=None):
        self.config = FakeConfig()
        self.model_path = ''
        self.use_mock_llm = False
        self.default_n_gpu_layers = -1
        self._llm = llm or FakeLlm()

    def get_llm_instance(self):
        return self._llm


class FakeDictCompletionLlm:
    def create_chat_completion(self, **_kwargs):
        return {'choices': [{'message': {'content': 'dict response'}}]}


class FakeNoLlmManager(FakeManager):
    def get_llm_instance(self):
        return None


class FakeLongStreamLlm:
    def create_chat_completion(self, **_kwargs):
        return iter(
            [
                {'choices': [{'delta': {'content': 'first'}, 'finish_reason': None}]},
                {'choices': [{'delta': {'content': ' second'}, 'finish_reason': None}]},
            ]
        )


@pytest.fixture(autouse=True)
def restore_model_manager_module():
    original = sys.modules.get('utils.llm.model_manager')
    original_compute_runtime = sys.modules.get('utils.compute_node_runtime')
    yield
    if original is None:
        sys.modules.pop('utils.llm.model_manager', None)
    else:
        sys.modules['utils.llm.model_manager'] = original
    if original_compute_runtime is None:
        sys.modules.pop('utils.compute_node_runtime', None)
    else:
        sys.modules['utils.compute_node_runtime'] = original_compute_runtime


def _install_fake_manager_module(manager):
    module = ModuleType('utils.llm.model_manager')
    module.get_model_manager = lambda: manager

    class _ModelManager:
        @staticmethod
        def _normalize_stream_chunk(chunk):
            return chunk

    module.ModelManager = _ModelManager
    sys.modules['utils.llm.model_manager'] = module

    compute_module = ModuleType('utils.compute_node_runtime')
    supported_modes = {'auto', 'cpu', 'gpu', 'hybrid'}
    aliases = {'cuda': 'gpu', 'metal': 'gpu'}

    def _normalize(mode):
        normalized = aliases.get(str(mode).lower(), str(mode).lower())
        return normalized if normalized in supported_modes else 'auto'

    def _apply_compute_mode(model_manager, mode):
        normalized = _normalize(mode)
        if normalized == 'cpu':
            model_manager.default_n_gpu_layers = 0
        elif normalized == 'hybrid':
            model_manager.default_n_gpu_layers = 24
        else:
            model_manager.default_n_gpu_layers = -1
        model_manager.requested_compute_mode = normalized
        return normalized

    def _compute_mode_diagnostics(model_manager):
        requested_mode = getattr(model_manager, 'requested_compute_mode', 'auto')
        effective_mode = 'cpu' if requested_mode == 'cpu' else 'gpu'
        return {
            'requested_mode': requested_mode,
            'effective_mode': effective_mode,
            'backend_used': effective_mode,
            'n_gpu_layers': model_manager.default_n_gpu_layers,
            'fallback_reason': None,
        }

    compute_module.normalize_compute_mode = _normalize
    compute_module.apply_compute_mode = _apply_compute_mode
    compute_module.compute_mode_diagnostics = _compute_mode_diagnostics
    sys.modules['utils.compute_node_runtime'] = compute_module


def _reset_cancel_queue():
    inference_sidecar._stdin_lines = queue.Queue()
    inference_sidecar._stdin_reader_started = True


def test_run_emits_bad_model_error_for_missing_path(capsys):
    _reset_cancel_queue()
    args = SimpleNamespace(model='/does/not/exist.gguf', mode='cpu', prompt='hello')

    status = inference_sidecar.run(args)

    assert status == 1
    event = json.loads(capsys.readouterr().out.strip())
    assert event['type'] == 'error'
    assert event['code'] == 'bad_model'


def test_run_streams_started_token_done_with_shared_runtime(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager()
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode='cpu', prompt='Say hello')
    status = inference_sidecar.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['type'] for event in events] == ['started', 'token', 'token', 'done']
    assert manager.default_n_gpu_layers == 0


def test_run_emits_canceled_when_cancel_signal_arrives(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager()
    _install_fake_manager_module(manager)
    inference_sidecar._stdin_lines.put('{"type":"cancel"}')

    args = SimpleNamespace(model=str(model_path), mode='cpu', prompt='Cancel me')
    status = inference_sidecar.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['type'] for event in events] == ['started', 'canceled']


def test_run_falls_back_to_non_streaming_when_stream_is_empty(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager(llm=FakeStreamNoContentLlm())
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode='auto', prompt='Say hello')
    status = inference_sidecar.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['type'] for event in events] == ['started', 'token', 'done']
    assert events[1]['text'] == 'fallback response'


@pytest.mark.parametrize(
    ('mode', 'expected'),
    [('cpu', 0), ('gpu', -1), ('hybrid', 24), ('auto', -1)],
)
def test_run_applies_compute_mode_to_manager(tmp_path, capsys, mode, expected):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager()
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode=mode, prompt='Mode test')
    status = inference_sidecar.run(args)

    assert status == 0
    _ = capsys.readouterr()
    assert manager.default_n_gpu_layers == expected


def test_run_handles_dict_completion_payload(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager(llm=FakeDictCompletionLlm())
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode='auto', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['type'] for event in events] == ['started', 'token', 'done']
    assert events[1]['text'] == 'dict response'


def test_run_normalizes_unknown_mode_to_auto_gpu_default(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager()
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode='UNSUPPORTED', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 0
    _ = capsys.readouterr()
    assert manager.default_n_gpu_layers == -1


def test_normalize_chunk_fallback_handles_object_shapes():
    class WithToDict:
        def to_dict(self):
            return {'choices': []}

    class WithModelDump:
        def model_dump(self):
            return {'choices': [{'delta': {'content': 'x'}}]}

    class WithDictMethod:
        def dict(self):
            return {'choices': [{'delta': {'content': 'y'}}]}

    class WithDunderDict:
        def __init__(self):
            self.choices = [{'delta': {'content': 'z'}}]

    assert inference_sidecar._fallback_normalize_chunk({'choices': []}) == {'choices': []}
    assert inference_sidecar._fallback_normalize_chunk(WithToDict()) == {'choices': []}
    assert inference_sidecar._fallback_normalize_chunk(WithModelDump()) == {
        'choices': [{'delta': {'content': 'x'}}]
    }
    assert inference_sidecar._fallback_normalize_chunk(WithDictMethod()) == {
        'choices': [{'delta': {'content': 'y'}}]
    }
    assert inference_sidecar._fallback_normalize_chunk(WithDunderDict()) == {
        'choices': [{'delta': {'content': 'z'}}]
    }


def test_run_emits_runtime_unavailable_when_model_manager_missing(tmp_path, capsys, monkeypatch):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'utils'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    sys.modules.pop('utils.llm.model_manager', None)

    args = SimpleNamespace(model=str(model_path), mode='cpu', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 1
    event = json.loads(capsys.readouterr().out.strip())
    assert event['code'] == 'runtime_unavailable'


def test_run_emits_bad_model_when_runtime_returns_no_llm(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeNoLlmManager()
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode='cpu', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 1
    event = json.loads(capsys.readouterr().out.strip())
    assert event['code'] == 'bad_model'


def test_run_cancels_during_streaming_after_started(tmp_path, capsys):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager(llm=FakeLongStreamLlm())
    _install_fake_manager_module(manager)
    inference_sidecar._stdin_lines.put('not-json')
    inference_sidecar._stdin_lines.put('{"type":"noop"}')
    inference_sidecar._stdin_lines.put('{"type":"cancel"}')

    args = SimpleNamespace(model=str(model_path), mode='cpu', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['type'] for event in events] == ['started', 'canceled']


def test_main_emits_inference_failed_when_compute_runtime_missing(capsys, monkeypatch):
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
            'inference_sidecar.py',
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'auto',
            '--prompt',
            'hello',
        ],
    )

    status = inference_sidecar.main()

    assert status == 1
    event = json.loads(capsys.readouterr().out.strip())
    assert event['type'] == 'error'
    assert event['code'] == 'inference_failed'
    assert 'bridge failure:' in event['message']


def test_main_normalizes_mode_before_run(monkeypatch):
    captured = {}
    _install_fake_manager_module(FakeManager())

    def fake_run(args):
        captured['mode'] = args.mode
        return 0

    monkeypatch.setattr(inference_sidecar, 'run', fake_run)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'inference_sidecar.py',
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'CUDA',
            '--prompt',
            'hello',
        ],
    )

    status = inference_sidecar.main()
    assert status == 0
    assert captured['mode'] == 'gpu'

    monkeypatch.setattr(
        sys,
        'argv',
        [
            'inference_sidecar.py',
            '--model',
            '/tmp/model.gguf',
            '--mode',
            'unsupported',
            '--prompt',
            'hello',
        ],
    )

    status = inference_sidecar.main()
    assert status == 0
    assert captured['mode'] == 'auto'


def test_extract_text_from_completion_handles_non_dict_message():
    assert inference_sidecar._extract_text_from_completion({'choices': [{'message': 'x'}]}) == ''


def test_extract_text_from_completion_handles_empty_choices():
    assert inference_sidecar._extract_text_from_completion({}) == ''


def test_normalize_chunk_fallback_handles_typeerror_and_unknown_shape():
    class WithBadDict:
        def dict(self, _required):  # pragma: no cover - signature intentionally incompatible
            return {'choices': []}

    class UnknownShape:
        pass

    assert inference_sidecar._fallback_normalize_chunk(WithBadDict()) == {}
    assert inference_sidecar._fallback_normalize_chunk(UnknownShape()) == {}


def test_stream_content_ignores_invalid_chunks_and_stops_on_finish_reason(capsys):
    inference_sidecar._stdin_lines = queue.Queue()
    inference_sidecar._stdin_reader_started = True

    chunks = iter(
        [
            {'choices': []},
            {'choices': [{'delta': 'not-a-dict', 'finish_reason': None}]},
            {'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]},
            {'choices': [{'delta': {'content': 'skipped-after-stop'}, 'finish_reason': None}]},
        ]
    )
    text, canceled = inference_sidecar._stream_content(chunks, lambda chunk: chunk)

    assert canceled is False
    assert text == 'ok'
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{'type': 'token', 'text': 'ok'}]


def test_stream_content_records_token_count_when_requested(capsys):
    inference_sidecar._stdin_lines = queue.Queue()
    inference_sidecar._stdin_reader_started = True

    chunks = iter(
        [
            {'choices': [{'delta': {'content': 'one'}, 'finish_reason': None}]},
            {'choices': [{'delta': {'content': ' two'}, 'finish_reason': 'stop'}]},
        ]
    )
    token_counts = []
    text, canceled = inference_sidecar._stream_content(chunks, lambda chunk: chunk, token_counts)

    assert canceled is False
    assert text == 'one two'
    assert token_counts == [2]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{'type': 'token', 'text': 'one'}, {'type': 'token', 'text': ' two'}]


def test_stream_content_records_token_count_on_cancel(capsys):
    inference_sidecar._stdin_lines = queue.Queue()
    inference_sidecar._stdin_reader_started = True
    inference_sidecar._stdin_lines.put('{"type":"cancel"}')

    chunks = iter([{'choices': [{'delta': {'content': 'ignored'}, 'finish_reason': None}]}])
    token_counts = []
    text, canceled = inference_sidecar._stream_content(chunks, lambda chunk: chunk, token_counts)

    assert canceled is True
    assert text == ''
    assert token_counts == [0]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{'type': 'canceled'}]


def test_run_done_without_token_when_stream_and_fallback_are_empty(tmp_path, capsys):
    class EmptyLlm:
        def create_chat_completion(self, **kwargs):
            if kwargs.get('stream') is False:
                return {'choices': [{'message': {'content': ''}}]}
            return iter([{'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])

    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager(llm=EmptyLlm())
    _install_fake_manager_module(manager)

    args = SimpleNamespace(model=str(model_path), mode='auto', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['type'] for event in events] == ['started', 'done']


def test_run_probe_only_windows_startup_emits_started_without_bootstrap_install_work(
    tmp_path, capsys, monkeypatch
):
    _reset_cancel_queue()
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake-model')

    manager = FakeManager()
    _install_fake_manager_module(manager)

    runtime_setup_module = sys.modules['desktop_runtime_setup']

    class _WinSysStub:
        platform = 'win32'
        executable = sys.executable

    probe = runtime_setup_module.RuntimeProbe(
        backend='cpu',
        detected_device='cpu',
        gpu_offload_supported=False,
        error='missing cuda runtime',
        interpreter=sys.executable,
        llama_module_path='missing',
        prefix='',
    )
    repair_calls = {'source': 0, 'retry_gate': 0}

    monkeypatch.setattr(runtime_setup_module, 'sys', _WinSysStub)
    monkeypatch.delenv(runtime_setup_module.DISABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.delenv(runtime_setup_module.ENABLE_BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(runtime_setup_module, '_probe_llama_runtime', lambda **_: probe)
    monkeypatch.setattr(
        runtime_setup_module,
        '_windows_cuda_source_repair',
        lambda _requirements_path: (
            repair_calls.__setitem__('source', repair_calls['source'] + 1) or True,
            'ok',
        ),
    )
    monkeypatch.setattr(
        runtime_setup_module,
        '_should_attempt_source_repair',
        lambda: (
            repair_calls.__setitem__('retry_gate', repair_calls['retry_gate'] + 1) or True,
            '',
        ),
    )

    args = SimpleNamespace(model=str(model_path), mode='auto', prompt='hello')
    status = inference_sidecar.run(args)

    assert status == 0
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert events[0]['type'] == 'started'
    assert events[-1]['type'] == 'done'
    assert 'desktop.runtime_setup' in captured.err
    assert 'action=probe_only' in captured.err
    assert repair_calls == {'source': 0, 'retry_gate': 0}
