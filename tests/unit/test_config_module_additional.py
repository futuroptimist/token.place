import json
from unittest.mock import patch
from pathlib import Path
import config


def test_save_user_config_error(tmp_path, monkeypatch, caplog):
    cfg = config.Config()
    bad_path = tmp_path / 'cfg.json'
    monkeypatch.setattr('builtins.open', lambda *a, **k: (_ for _ in ()).throw(IOError('boom')))
    with caplog.at_level('ERROR'):
        cfg.save_user_config(str(bad_path))
    assert any('Error saving configuration' in r.message for r in caplog.records)


def test_load_user_config_missing_file(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')
    config_path = tmp_path / 'nope.json'
    monkeypatch.setattr('utils.path_handling.get_config_dir', lambda: tmp_path / 'config')
    monkeypatch.setattr('utils.path_handling.get_app_data_dir', lambda: tmp_path / 'data')
    monkeypatch.setattr('utils.path_handling.get_models_dir', lambda: tmp_path / 'models')
    monkeypatch.setattr('utils.path_handling.get_logs_dir', lambda: tmp_path / 'logs')
    monkeypatch.setattr('utils.path_handling.get_cache_dir', lambda: tmp_path / 'cache')
    monkeypatch.setattr('utils.path_handling.ensure_dir_exists', lambda p: Path(p).mkdir(parents=True, exist_ok=True))
    with caplog.at_level('WARNING'):
        cfg = config.Config(config_path=str(config_path))
    assert any('User configuration file not found' in r.message for r in caplog.records)
    assert cfg.get('server.port') == 8001


def patched_paths_no_config(tmp_path, monkeypatch):
    """Helper to patch path functions without TOKEN_PLACE_CONFIG."""
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')
    monkeypatch.setenv('PLATFORM', 'linux')
    monkeypatch.delenv('TOKEN_PLACE_CONFIG', raising=False)

    monkeypatch.setattr('utils.path_handling.get_config_dir', lambda: tmp_path / 'config')
    monkeypatch.setattr('utils.path_handling.get_app_data_dir', lambda: tmp_path / 'data')
    monkeypatch.setattr('utils.path_handling.get_models_dir', lambda: tmp_path / 'models')
    monkeypatch.setattr('utils.path_handling.get_logs_dir', lambda: tmp_path / 'logs')
    monkeypatch.setattr('utils.path_handling.get_cache_dir', lambda: tmp_path / 'cache')
    monkeypatch.setattr('utils.path_handling.ensure_dir_exists', lambda p: Path(p).mkdir(parents=True, exist_ok=True))
    return tmp_path


def test_save_user_config_default_path(tmp_path, monkeypatch):
    base = patched_paths_no_config(tmp_path, monkeypatch)
    cfg = config.Config()
    cfg.save_user_config()
    assert (base / 'config' / 'user_config.json').exists()


def test_load_user_config_generic_error(tmp_path, monkeypatch, caplog):
    base = patched_paths_no_config(tmp_path, monkeypatch)
    bad = base / 'bad.json'
    monkeypatch.setattr('builtins.open', lambda *a, **k: (_ for _ in ()).throw(ValueError('boom')))
    with caplog.at_level('ERROR'):
        config.Config(config_path=str(bad))
    assert any('Error loading user configuration' in r.message for r in caplog.records)


def test_env_properties_and_global_get(tmp_path, monkeypatch):
    patched_paths_no_config(tmp_path, monkeypatch)
    cfg_dev = config.Config(env='development')
    cfg_prod = config.Config(env='production')
    assert cfg_dev.is_development and not cfg_dev.is_production
    assert cfg_prod.is_production and not cfg_prod.is_development
    assert isinstance(config.get_config(), config.Config)
