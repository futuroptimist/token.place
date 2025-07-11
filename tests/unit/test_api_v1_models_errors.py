import importlib
import os
from unittest.mock import patch

import pytest

@patch.dict(os.environ, {"USE_MOCK_LLM": "0"})
def test_invalid_response_structure(monkeypatch):
    import api.v1.models as models
    importlib.reload(models)
    class Dummy:
        def create_chat_completion(self, messages):
            return {"bad": "data"}
    monkeypatch.setattr(models, "get_model_instance", lambda mid: Dummy())
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(models.ModelError) as exc:
        models.generate_response("llama-3-8b-instruct", messages)
    assert exc.value.error_type == "model_response_error"

@patch.dict(os.environ, {"USE_MOCK_LLM": "0"})
def test_model_exception(monkeypatch):
    import api.v1.models as models
    importlib.reload(models)
    class Dummy:
        def create_chat_completion(self, messages):
            raise RuntimeError("fail")
    monkeypatch.setattr(models, "get_model_instance", lambda mid: Dummy())
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(models.ModelError) as exc:
        models.generate_response("llama-3-8b-instruct", messages)
    assert exc.value.error_type == "model_inference_error"

