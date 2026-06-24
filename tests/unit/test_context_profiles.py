import pytest

from utils.context_profiles import apply_context_profile, get_context_profile, normalize_context_tier


class _Config:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


class _Manager:
    def __init__(self):
        self.config = _Config()


def test_normalize_unknown_context_profile_defaults_to_8k_fast():
    assert normalize_context_tier("unknown") == "8k-fast"


def test_get_context_profile_rejects_unknown_profile():
    with pytest.raises(ValueError):
        get_context_profile("unknown")


def test_apply_context_profile_sets_model_context_before_warm_load():
    manager = _Manager()

    profile = apply_context_profile(manager, "64k-full")

    assert profile.profile_id == "64k-full"
    assert manager.config.values["model.context_size"] == 65536
    assert manager.context_tier == "64k-full"
    assert manager.context_window_tokens == 65536
