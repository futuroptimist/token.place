import copy

from config import Config, DEFAULT_CONFIG


def test_config_initialization_does_not_mutate_default_config():
    """Config initialization should not modify DEFAULT_CONFIG."""
    snapshot = copy.deepcopy(DEFAULT_CONFIG)
    Config(env="testing")
    assert DEFAULT_CONFIG == snapshot
