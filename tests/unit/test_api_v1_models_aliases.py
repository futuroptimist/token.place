"""Tests covering alias resolution behaviour for API v1 models."""

import importlib
import os
from unittest.mock import MagicMock, patch


def _reload_models(env=None):
    """Reload the api.v1.models module with a controlled environment."""

    env_vars = {"ENVIRONMENT": "test", "USE_MOCK_LLM": "1"}
    if env:
        env_vars.update(env)

    # Ensure the module is reloaded under the patched environment and dependencies.
    fake_llama_module = MagicMock()
    with patch.dict("sys.modules", {"llama_cpp": fake_llama_module}):
        with patch.dict(os.environ, env_vars, clear=True):
            import api.v1.models as models
            importlib.reload(models)
    return models


def test_resolve_model_alias_returns_canonical_id():
    models = _reload_models()
    assert models.resolve_model_alias("gpt-5-chat-latest") == "llama-3-8b-instruct"


def test_resolve_model_alias_missing_target_logs_and_returns_none():
    models = _reload_models()
    with patch.object(models, "_get_model_metadata", return_value=None):
        with patch.object(models, "log_warning") as mock_log_warning:
            result = models.resolve_model_alias("gpt-5-chat-latest")
    assert result is None
    mock_log_warning.assert_called_once()
