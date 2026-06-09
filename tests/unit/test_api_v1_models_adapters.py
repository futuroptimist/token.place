"""Guardrails ensuring API v1 no longer exposes launch adapter variants."""

import importlib
from typing import Dict

ALIGNMENT_ID = "llama-3-8b-instruct:alignment"
CANONICAL_ID = "llama-3.1-8b-instruct"


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

    assert [item["id"] for item in info] == [CANONICAL_ID]
    assert all("adapter" not in item for item in info)
    assert all(item["id"] != ALIGNMENT_ID for item in info)


def test_alignment_adapter_is_not_accepted_by_api_v1_runtime(monkeypatch):
    models = _reload_models(monkeypatch, {"USE_MOCK_LLM": "1"})

    try:
        models.get_model_instance(ALIGNMENT_ID)
    except models.ModelError as exc:
        assert exc.error_type == "model_not_found"
    else:  # pragma: no cover - keeps the assertion failure readable
        raise AssertionError("alignment adapter should not be accepted by API v1")
