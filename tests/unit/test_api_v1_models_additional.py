import importlib
import os
import random
import pytest
from unittest.mock import MagicMock, patch


@patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_get_model_instance_mock():
    import api.v1.models as models
    importlib.reload(models)
    inst = models.get_model_instance('llama-3-8b-instruct')
    assert inst == "MOCK_MODEL"


@patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_get_model_instance_invalid():
    import api.v1.models as models
    importlib.reload(models)
    with pytest.raises(models.ModelError):
        models.get_model_instance('bad-id')


@patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_generate_response_mock(monkeypatch):
    import api.v1.models as models
    importlib.reload(models)
    # Force deterministic choice
    monkeypatch.setattr(random, 'choice', lambda seq: seq[0])
    messages = [{'role': 'user', 'content': 'hi'}]
    resp = models.generate_response('llama-3-8b-instruct', messages)
    assert resp[-1]['role'] == 'assistant'
    assert 'Mock response:' in resp[-1]['content']


@patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_generate_response_validation_errors():
    import api.v1.models as models
    importlib.reload(models)
    with pytest.raises(models.ModelError):
        models.generate_response('llama-3-8b-instruct', [])
    bad_messages = [{'role': 'user'}]
    with pytest.raises(models.ModelError):
        models.generate_response('llama-3-8b-instruct', bad_messages)


@patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_get_model_instance_empty_id():
    import api.v1.models as models
    import importlib
    importlib.reload(models)
    with pytest.raises(models.ModelError) as exc:
        models.get_model_instance("")
    assert "Model ID cannot be empty" in str(exc.value)


@patch.dict(os.environ, {"USE_MOCK_LLM": "0"})
def test_get_model_instance_load_error(monkeypatch):
    import api.v1.models as models
    import importlib
    importlib.reload(models)

    def boom(*args, **kwargs):
        raise RuntimeError("load fail")

    monkeypatch.setattr(models, "Llama", lambda *a, **k: boom())
    with pytest.raises(models.ModelError) as exc:
        models.get_model_instance("llama-3-8b-instruct")
    assert "Failed to load model" in str(exc.value)


@patch.dict(os.environ, {"ENVIRONMENT": "prod"})
def test_prod_logging_suppressed(monkeypatch):
    import importlib
    import logging
    fake_logger = MagicMock()
    monkeypatch.setattr(logging, "getLogger", lambda *a, **k: fake_logger)
    import api.v1.models as models
    importlib.reload(models)

    models.log_info("info")
    models.log_warning("warn")
    models.log_error("err")

    fake_logger.info.assert_not_called()
    fake_logger.warning.assert_not_called()
    fake_logger.error.assert_not_called()


def test_stream_chat_completion_requires_messages():
    import api.v1.models as models
    import importlib
    importlib.reload(models)

    with pytest.raises(models.ModelError):
        generator = models.stream_chat_completion('llama-3-8b-instruct', [])
        next(generator)


def test_stream_chat_completion_mock_mode(monkeypatch):
    import api.v1.models as models
    import importlib
    importlib.reload(models)

    monkeypatch.setattr(models, '_get_model_and_mode', lambda mid: ("MOCK_MODEL", True))
    monkeypatch.setattr(models.random, 'choice', lambda seq: seq[0])

    messages = [{'role': 'user', 'content': 'hi'}]
    chunks = list(models.stream_chat_completion('llama-3-8b-instruct', messages))

    # Expect assistant role delta, content tokens, and final stop chunk
    assert chunks[0]['choices'][0]['delta']['role'] == 'assistant'
    assert any('content' in chunk['choices'][0]['delta'] for chunk in chunks[1:-1])
    assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'


def test_stream_chat_completion_real_model(monkeypatch):
    import api.v1.models as models
    import importlib
    importlib.reload(models)

    class DummyModel:
        def create_chat_completion(self, messages, stream=True):
            assert stream is True

            def _gen():
                yield {
                    'choices': [{
                        'index': 0,
                        'delta': {'role': 'assistant'},
                        'finish_reason': None,
                    }]
                }
                yield "ignore"
                yield {
                    'choices': [{
                        'index': 0,
                        'delta': {'content': 'Hello'},
                        'finish_reason': None,
                    }]
                }

            return _gen()

    monkeypatch.setattr(models, '_get_model_and_mode', lambda mid: (DummyModel(), False))

    messages = [{'role': 'user', 'content': 'hi'}]
    chunks = list(models.stream_chat_completion('llama-3-8b-instruct', messages))

    assert len(chunks) == 2
    assert chunks[0]['choices'][0]['delta']['role'] == 'assistant'
    assert chunks[1]['choices'][0]['delta']['content'] == 'Hello'


def test_stream_chat_completion_model_attribute_error(monkeypatch):
    import api.v1.models as models
    import importlib
    importlib.reload(models)

    class NoStreamModel:
        pass

    monkeypatch.setattr(models, '_get_model_and_mode', lambda mid: (NoStreamModel(), False))

    with pytest.raises(models.ModelError) as exc:
        generator = models.stream_chat_completion(
            'llama-3-8b-instruct',
            [{'role': 'user', 'content': 'hi'}],
        )
        next(generator)

    assert "does not support streaming" in str(exc.value)
