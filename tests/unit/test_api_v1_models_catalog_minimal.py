"""Regression tests ensuring API v1 exposes only the canonical Llama 3.1 8B model."""

import importlib
import os
from unittest import mock


CANONICAL_ID = "llama-3.1-8b-instruct"
LEGACY_ID = "llama-3-8b-instruct"
ALIGNMENT_ID = "llama-3-8b-instruct:alignment"


@mock.patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_api_v1_catalog_is_restricted_to_canonical_llama_3_1_8b():
    import api.v1.models as models

    importlib.reload(models)

    entries = models.get_models_info()
    model_ids = [entry["id"] for entry in entries]

    assert model_ids == [CANONICAL_ID]
    assert LEGACY_ID not in model_ids
    assert ALIGNMENT_ID not in model_ids

    base_entry = entries[0]
    assert base_entry["base_model_id"] == CANONICAL_ID
    assert base_entry["owned_by"] == "Meta"
    assert base_entry["provider"] == "meta"
    assert base_entry["source"] == "meta-llama/Llama-3.1-8B-Instruct"


@mock.patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_llama_catalog_targets_latest_4090_ready_release():
    import api.v1.models as models

    importlib.reload(models)

    base_entry = models.get_models_info()[0]

    assert base_entry["id"] == CANONICAL_ID
    assert base_entry["name"] == "Meta Llama 3.1 8B Instruct"
    assert base_entry["file_name"] == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert base_entry["file_name"] in base_entry["url"]
