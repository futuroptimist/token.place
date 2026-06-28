"""Regression tests ensuring API v1 exposes only the canonical Llama 3.1 8B model."""

import importlib
import os
from unittest import mock


@mock.patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_api_v1_catalog_is_restricted_to_single_llama_3_1_8b_model():
    import api.v1.models as models

    importlib.reload(models)

    entries = models.get_models_info()
    assert [entry["id"] for entry in entries] == ["llama-3.1-8b-instruct"]

    base_entry = entries[0]
    assert base_entry["base_model_id"] == "llama-3.1-8b-instruct"
    assert base_entry["owned_by"] == "Meta"
    assert base_entry["owner"] == "Meta"
    assert base_entry["provider"] == "meta"
    assert base_entry["source_model"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert "adapter" not in base_entry


@mock.patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_llama_catalog_targets_latest_4090_ready_release():
    import api.v1.models as models

    importlib.reload(models)

    base_entry = next(
        entry for entry in models.get_models_info() if entry["id"] == "llama-3.1-8b-instruct"
    )

    assert base_entry["name"] == "Meta Llama 3.1 8B Instruct"
    assert base_entry["file_name"] == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert base_entry["file_name"] in base_entry["url"]


def test_qwen3_profile_exists_but_is_not_public_catalog_default():
    from utils.llm.model_profiles import get_model_profile, iter_model_profiles

    qwen = get_model_profile("qwen3-8b-q4-k-m")

    assert qwen is not None
    assert qwen["api_model_id"] == "qwen3-8b-instruct"
    assert qwen["display_name"] == "Qwen3 8B Instruct"
    assert qwen["source_model"] == "Qwen/Qwen3-8B"
    assert qwen["gguf_repo"] == "Qwen/Qwen3-8B-GGUF"
    assert qwen["filename"] == "Qwen3-8B-Q4_K_M.gguf"
    assert qwen["quantization"] == "Q4_K_M"
    assert qwen["license"] == "apache-2.0"
    assert qwen["parameters"] == "8.2B"
    assert qwen["native_context_tokens"] == 32768
    assert qwen["maximum_validated_context_tokens"] == 131072
    assert qwen["supported_context_tiers"] == ["8k-fast", "64k-full"]
    assert qwen["thinking_mode"] == "disabled"
    assert qwen["chat_template_policy"] == "gguf-jinja"
    assert qwen["rope_scaling_policy"] == {
        "type": "yarn",
        "required_for_tier": "64k-full",
        "factor": 2.0,
        "original_context_tokens": 32768,
    }
    assert qwen["public_catalog"] is False
    assert qwen["runnable"] is False
    assert [profile["api_model_id"] for profile in iter_model_profiles(public_only=True)] == [
        "llama-3.1-8b-instruct"
    ]
