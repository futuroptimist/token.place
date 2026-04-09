from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python' / 'inference_sidecar.py'
SPEC = importlib.util.spec_from_file_location('desktop_inference_sidecar', MODULE_PATH)
inference_sidecar = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(inference_sidecar)


class _FakeLlm:
    def __init__(self, chunks: Iterable[Dict[str, Any]]):
        self._chunks = list(chunks)

    def create_chat_completion(self, **kwargs: Any) -> Iterable[Dict[str, Any]]:
        return iter(self._chunks)


class _FakeManager:
    def __init__(self, chunks: Iterable[Dict[str, Any]]):
        self.config = {
            'model.max_tokens': 16,
            'model.temperature': 0.7,
            'model.top_p': 0.9,
            'model.stop_tokens': [],
        }
        self.default_n_gpu_layers = -1
        self._llm = _FakeLlm(chunks)
        self.model_path = ''

    def get_llm_instance(self) -> _FakeLlm:
        return self._llm


def _collect_events() -> tuple[List[Dict[str, Any]], Any]:
    events: List[Dict[str, Any]] = []

    def emit_fn(payload: Dict[str, Any]) -> None:
        events.append(payload)

    return events, emit_fn


def test_run_inference_emits_started_tokens_and_done(tmp_path: Path):
    model_path = tmp_path / 'fake.gguf'
    model_path.write_bytes(b'gguf')

    manager = _FakeManager(
        [
            {'choices': [{'delta': {'content': 'Hello'}}]},
            {'choices': [{'delta': {'content': ' world'}, 'finish_reason': 'stop'}]},
        ]
    )

    events, emit_fn = _collect_events()
    with patch.dict('sys.modules', {'utils.llm.model_manager': SimpleNamespace(get_model_manager=lambda: manager)}):
        status = inference_sidecar.run_inference(
            str(model_path),
            'say hi',
            'cpu',
            emit_fn=emit_fn,
            canceled_fn=lambda: False,
        )

    assert status == 0
    assert events[0] == {'type': 'started'}
    assert events[1] == {'type': 'token', 'text': 'Hello'}
    assert events[2] == {'type': 'token', 'text': ' world'}
    assert events[3] == {'type': 'done'}
    assert manager.default_n_gpu_layers == 0


def test_run_inference_emits_canceled_when_requested(tmp_path: Path):
    model_path = tmp_path / 'fake.gguf'
    model_path.write_bytes(b'gguf')

    manager = _FakeManager([{'choices': [{'delta': {'content': 'Hello'}}]}])
    events, emit_fn = _collect_events()

    calls = {'count': 0}

    def canceled_fn() -> bool:
        calls['count'] += 1
        return calls['count'] >= 2

    with patch.dict('sys.modules', {'utils.llm.model_manager': SimpleNamespace(get_model_manager=lambda: manager)}):
        status = inference_sidecar.run_inference(
            str(model_path),
            'cancel me',
            'auto',
            emit_fn=emit_fn,
            canceled_fn=canceled_fn,
        )

    assert status == 0
    assert events[0] == {'type': 'started'}
    assert events[1] == {'type': 'token', 'text': 'Hello'}
    assert events[2] == {'type': 'canceled'}


def test_run_inference_emits_bad_model_error_when_path_missing():
    events, emit_fn = _collect_events()
    status = inference_sidecar.run_inference(
        '/path/does/not/exist.gguf',
        'hello',
        'auto',
        emit_fn=emit_fn,
        canceled_fn=lambda: False,
    )

    assert status == 2
    assert events == [{'type': 'error', 'code': 'bad_model', 'message': 'model path not found'}]
