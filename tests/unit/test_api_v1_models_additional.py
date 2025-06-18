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
