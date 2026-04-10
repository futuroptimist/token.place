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
    yield
    if original is None:
        sys.modules.pop('utils.llm.model_manager', None)
    else:
        sys.modules['utils.llm.model_manager'] = original


def _install_fake_manager_module(manager):
    module = ModuleType('utils.llm.model_manager')
    module.get_model_manager = lambda: manager

    class _ModelManager:
        @staticmethod
        def _normalize_stream_chunk(chunk):
            return chunk

    module.ModelManager = _ModelManager
    sys.modules['utils.llm.model_manager'] = module


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
    [('cpu', 0), ('metal', -1), ('cuda', -1), ('auto', -1)],
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


def test_extract_text_from_completion_handles_non_dict_message():
    assert inference_sidecar._extract_text_from_completion({'choices': [{'message': 'x'}]}) == ''


def test_extract_text_from_completion_handles_empty_choices():
    assert inference_sidecar._extract_text_from_completion({}) == ''


def test_normalize_chunk_fallback_handles_typeerror_and_unknown_shape():
    class WithBadDict:
        def dict(self, required):  # pragma: no cover - signature intentionally incompatible
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
