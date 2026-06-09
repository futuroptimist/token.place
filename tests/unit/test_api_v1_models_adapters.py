"""Regression tests ensuring hallucinated API v1 adapters stay removed."""

import importlib
from typing import Dict

import pytest

ADAPTER_ID = "llama-3-8b-instruct:alignment"
BASE_ID = "llama-3.1-8b-instruct"


def _reload_models(monkeypatch, env: Dict[str, str] | None = None):
    """Reload api.v1.models with an optional environment override."""
    if env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    import api.v1.models as models

    importlib.reload(models)
    return models


def test_get_models_info_does_not_expose_alignment_adapter(monkeypatch):
    models = _reload_models(monkeypatch, {"USE_MOCK_LLM": "1"})

    info = models.get_models_info()

    assert [item["id"] for item in info] == [BASE_ID]
    assert all(item.get("adapter") is None for item in info)


def test_generate_response_rejects_removed_alignment_adapter(monkeypatch):
    models = _reload_models(monkeypatch, {"USE_MOCK_LLM": "1"})

    with pytest.raises(models.ModelError) as exc:
        models.generate_response(ADAPTER_ID, [{"role": "user", "content": "hello"}])

    assert exc.value.error_type == "model_not_found"
