"""Adapter support tests for api.v1.models."""

import importlib
from typing import Dict

ADAPTER_ID = "llama-3-8b-instruct:alignment"
BASE_ID = "llama-3-8b-instruct"


def _reload_models(monkeypatch, env: Dict[str, str] | None = None):
    """Reload api.v1.models with an optional environment override."""
    if env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    import api.v1.models as models

    importlib.reload(models)
    return models


def test_get_models_info_exposes_adapter_metadata(monkeypatch):
    models = _reload_models(monkeypatch, {"USE_MOCK_LLM": "1"})

    info = models.get_models_info()
    adapter_entry = next((item for item in info if item["id"] == ADAPTER_ID), None)

    assert adapter_entry is not None, "adapter should be listed alongside base models"
    assert adapter_entry["base_model_id"] == BASE_ID
    assert adapter_entry.get("adapter", {}).get("instructions"), "instructions are required"


def test_generate_response_injects_adapter_prompt(monkeypatch):
    models = _reload_models(monkeypatch, {"USE_MOCK_LLM": "1"})
    monkeypatch.setattr(models.random, "choice", lambda seq: seq[0])

    messages = [{"role": "user", "content": "hello"}]
    response = models.generate_response(ADAPTER_ID, messages)

    assert response[0]["role"] == "system"
    assert response[0]["name"].startswith("adapter:")
    assert "alignment" in response[0]["content"].lower()
    assert response[-1]["role"] == "assistant"
