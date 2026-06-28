"""Tests covering alias resolution behaviour for API v1 models."""

import importlib
import importlib.util
import os
from pathlib import Path
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
            module_path = Path(__file__).resolve().parents[2] / "api" / "v1" / "models.py"
            spec = importlib.util.spec_from_file_location("api_v1_models_alias_test", module_path)
            models = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(models)
    return models


def test_resolve_model_alias_returns_canonical_id_for_compat_aliases():
    models = _reload_models()
    assert models.resolve_model_alias("llama-3-8b-instruct") == "llama-3.1-8b-instruct"
    assert models.resolve_model_alias("gpt-5-chat-latest") == "llama-3.1-8b-instruct"
    assert models.resolve_model_alias("gpt-3.5-turbo") == "llama-3.1-8b-instruct"


def test_resolve_model_alias_rejects_unsupported_gpt_id():
    models = _reload_models()
    assert models.resolve_model_alias("gpt-4") is None


def test_resolve_model_alias_missing_target_logs_and_returns_none():
    models = _reload_models()
    with patch.dict(models.MODEL_ALIASES, {"local-alias": "missing-model"}, clear=True):
        with patch.object(models, "_get_model_metadata", return_value=None):
            with patch.object(models, "log_warning") as mock_log_warning:
                result = models.resolve_model_alias("local-alias")
    assert result is None
    mock_log_warning.assert_called_once()


def test_qwen_is_not_aliased_to_llama_or_from_llama():
    models = _reload_models()
    assert models.resolve_model_alias("qwen3-8b-instruct") is None
    assert "qwen3-8b-instruct" not in models.MODEL_ALIASES.values()
    assert models.MODEL_ALIASES["gpt-5-chat-latest"] == "llama-3.1-8b-instruct"
