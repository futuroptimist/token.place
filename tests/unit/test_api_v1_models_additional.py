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
def test_get_model_instance_v2_catalogue():
    import api.v1.models as models
    import api.v2.models as models_v2

    # Reload both catalogues so the environment flag is picked up and the
    # fallback to the v2 listings is available within the v1 loader.
    importlib.reload(models_v2)
    importlib.reload(models)

    inst = models.get_model_instance('mistral-7b-instruct')
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
