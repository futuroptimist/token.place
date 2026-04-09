"""Unit tests for the desktop Python inference bridge."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python' / 'inference_bridge.py'
SPEC = importlib.util.spec_from_file_location('desktop_inference_bridge', MODULE_PATH)
inference_bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(inference_bridge)


def test_iter_text_deltas_emits_stream_content_only():
    stream = [
        {'choices': [{'delta': {'role': 'assistant'}}]},
        {'choices': [{'delta': {'content': 'Hello'}}]},
        {'choices': [{'delta': {'content': ' world'}}]},
        {'choices': [{'delta': {}}]},
    ]

    assert list(inference_bridge._iter_text_deltas(stream)) == ['Hello', ' world']


def test_main_streams_tokens_and_done_for_mock_runtime(tmp_path, monkeypatch, capsys):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake')

    mock_completion = [
        {'choices': [{'delta': {'content': 'Hi'}}]},
        {'choices': [{'delta': {'content': ' there'}}]},
    ]
    llm = MagicMock()
    llm.create_chat_completion.return_value = mock_completion

    manager = MagicMock()
    manager.config = {
        'model.max_tokens': 512,
        'model.temperature': 0.7,
        'model.top_p': 0.9,
        'model.stop_tokens': [],
    }
    manager.get_llm_instance.return_value = llm

    monkeypatch.setattr('sys.argv', ['bridge', '--model', str(model_path), '--prompt', 'hello'])
    with patch('utils.llm.model_manager.get_model_manager', return_value=manager):
        status = inference_bridge.main()

    assert status == 0
    emitted = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert emitted[0] == {'type': 'started'}
    assert emitted[1] == {'type': 'token', 'text': 'Hi'}
    assert emitted[2] == {'type': 'token', 'text': ' there'}
    assert emitted[3] == {'type': 'done'}


def test_main_emits_canceled_event_when_cancel_requested(tmp_path, monkeypatch, capsys):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('fake')

    llm = MagicMock()
    llm.create_chat_completion.return_value = [
        {'choices': [{'delta': {'content': 'A'}}]},
        {'choices': [{'delta': {'content': 'B'}}]},
    ]

    manager = MagicMock()
    manager.config = {
        'model.max_tokens': 512,
        'model.temperature': 0.7,
        'model.top_p': 0.9,
        'model.stop_tokens': [],
    }
    manager.get_llm_instance.return_value = llm

    monkeypatch.setattr('sys.argv', ['bridge', '--model', str(model_path), '--prompt', 'hello'])
    with patch('utils.llm.model_manager.get_model_manager', return_value=manager):
        with patch.object(inference_bridge, 'canceled_requested', side_effect=[False, True]):
            status = inference_bridge.main()

    assert status == 0
    emitted = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert emitted[0] == {'type': 'started'}
    assert emitted[1] == {'type': 'token', 'text': 'A'}
    assert emitted[2] == {'type': 'canceled'}
