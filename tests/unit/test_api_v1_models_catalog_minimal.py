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
