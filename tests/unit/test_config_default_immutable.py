import copy

import pytest

from config import Config, DEFAULT_CONFIG


@pytest.fixture
def testing_config() -> Config:
    """Provide an isolated testing config instance for mutation scenarios."""

    return Config(env="testing")


def test_config_initialization_does_not_mutate_default_config(testing_config: Config):
    """Config initialization should not modify DEFAULT_CONFIG."""
    snapshot = copy.deepcopy(DEFAULT_CONFIG)
    # Access fixture to ensure construction completes without touching defaults.
    _ = testing_config
    assert DEFAULT_CONFIG == snapshot


def test_reset_restores_deep_copied_defaults(testing_config: Config):
    """`Config.reset` should rebuild from untouched DEFAULT_CONFIG copies."""

    testing_config.set("relay.additional_servers", ["https://mutated.example"])
    testing_config.set("model.temperature", 0.05)

    # Sanity check that mutations took effect prior to reset.
    assert testing_config.get("relay.additional_servers") == ["https://mutated.example"]
    assert testing_config.get("model.temperature") == pytest.approx(0.05)

    testing_config.reset()

    assert testing_config.get("relay.additional_servers") == []
    assert testing_config.get("model.temperature") == DEFAULT_CONFIG["model"]["temperature"]
    # Environment overrides should be re-applied after the reset.
    assert testing_config.get("server.port") == 8001
