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
