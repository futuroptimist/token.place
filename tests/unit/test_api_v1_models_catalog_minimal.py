"""Regression tests ensuring API v1 exposes only the canonical Llama 3 8B model."""

import importlib
import os
from unittest import mock


@mock.patch.dict(os.environ, {"USE_MOCK_LLM": "1"})
def test_api_v1_catalog_is_restricted_to_llama_3_8b():
    import api.v1.models as models

    importlib.reload(models)

    entries = models.get_models_info()
    model_ids = {entry["id"] for entry in entries}

    assert model_ids == {
        "llama-3-8b-instruct",
        "llama-3-8b-instruct:alignment",
    }

    base_entry = next(entry for entry in entries if entry["id"] == "llama-3-8b-instruct")
    assert base_entry["base_model_id"] == "llama-3-8b-instruct"

    adapter_entry = next(entry for entry in entries if entry["id"] == "llama-3-8b-instruct:alignment")
    assert adapter_entry["adapter"]["share_base"] is True
    assert adapter_entry["base_model_id"] == "llama-3-8b-instruct"
