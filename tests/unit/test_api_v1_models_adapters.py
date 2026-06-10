"""Guardrails ensuring the removed API v1 alignment adapter stays unavailable."""

import importlib
from typing import Dict

ADAPTER_ID = "llama-3-8b-instruct:alignment"
CANONICAL_ADAPTER_ID = "llama-3.1-8b-instruct:alignment"
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

    ids = [item["id"] for item in models.get_models_info()]

    assert ids == [BASE_ID]
    assert ADAPTER_ID not in ids
    assert CANONICAL_ADAPTER_ID not in ids


def test_generate_response_rejects_removed_alignment_adapter(monkeypatch):
    models = _reload_models(monkeypatch, {"USE_MOCK_LLM": "1"})

    with monkeypatch.context() as _context:
        messages = [{"role": "user", "content": "hello"}]
        for removed_id in (ADAPTER_ID, CANONICAL_ADAPTER_ID):
            try:
                models.generate_response(removed_id, list(messages))
            except models.ModelError as exc:
                assert exc.error_type == "model_not_found"
            else:  # pragma: no cover - assertion branch
                raise AssertionError(f"{removed_id} should not be accepted")
