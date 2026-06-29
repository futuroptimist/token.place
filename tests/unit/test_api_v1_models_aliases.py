"""Tests covering alias resolution behaviour for API v1 models."""

import importlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _reload_models(env=None):
    """Reload the api.v1.models module with a controlled environment."""

    env_vars = {"ENVIRONMENT": "test", "USE_MOCK_LLM": "1"}
    if env:
        env_vars.update(env)

    # Ensure the module is reloaded under the patched environment and dependencies.
    fake_llama_module = MagicMock()
    fake_routes_module = SimpleNamespace(bp=MagicMock())
    with patch.dict("sys.modules", {
        "llama_cpp": fake_llama_module,
        "api.v1.routes": fake_routes_module,
        "api.v2.routes": fake_routes_module,
    }):
        with patch.dict(os.environ, env_vars, clear=True):
            import api.v1.models as models
            importlib.reload(models)
    return models


def test_resolve_model_alias_returns_canonical_id_for_compat_aliases():
    models = _reload_models()
    assert models.resolve_model_alias("llama-3-8b-instruct") == "qwen3-8b-instruct"
    assert models.resolve_model_alias("gpt-5-chat-latest") == "qwen3-8b-instruct"
    assert models.resolve_model_alias("gpt-3.5-turbo") == "qwen3-8b-instruct"


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


def test_api_v1_alias_sources_agree_across_relay_and_client():
    models = _reload_models()
    import relay
    from utils.llm.model_profiles import build_model_aliases
    from utils.networking.relay_client import RelayClient

    expected = build_model_aliases()

    assert models.MODEL_ALIASES == expected
    assert relay.MODEL_ALIASES == expected
    assert RelayClient._API_V1_LOCAL_MODEL_ALIASES == expected
