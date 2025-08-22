import importlib
import os
from pathlib import Path
from unittest import mock

import pytest

import utils.path_handling as ph


def test_paths_linux(tmp_path):
    with mock.patch('platform.system', return_value='Linux'):
        importlib.reload(ph)
        home = Path.home()
        assert ph.get_app_data_dir() == home / '.local' / 'share' / 'token.place'
        assert ph.get_config_dir() == home / '.config' / 'token.place' / 'config'
        assert ph.get_cache_dir() == home / '.cache' / 'token.place'
        assert ph.get_models_dir() == home / '.local' / 'share' / 'token.place' / 'models'
        assert ph.get_logs_dir() == home / '.local' / 'state' / 'token.place' / 'logs'

        test_dir = tmp_path / 'newdir'
        created = ph.ensure_dir_exists(test_dir)
        assert created.is_dir()

        assert ph.get_executable_extension() == ''
        assert ph.normalize_path('~/test').is_absolute()
        rel = ph.get_relative_path(created, tmp_path)
        assert rel == Path('newdir')


def test_paths_linux_with_xdg_state_home(tmp_path):
    env = {'XDG_STATE_HOME': str(tmp_path / 'xdg' / 'state')}
    with mock.patch('platform.system', return_value='Linux'):
        with mock.patch.dict(os.environ, env, clear=False):
            importlib.reload(ph)
            base = tmp_path / 'xdg' / 'state'
            assert ph.get_logs_dir() == base / 'token.place' / 'logs'


def test_paths_linux_with_xdg_config_home(tmp_path):
    env = {'XDG_CONFIG_HOME': str(tmp_path / 'xdg' / 'config')}
    with mock.patch('platform.system', return_value='Linux'):
        with mock.patch.dict(os.environ, env, clear=False):
            importlib.reload(ph)
            base = tmp_path / 'xdg' / 'config'
            assert ph.get_config_dir() == base / 'token.place' / 'config'


def test_env_vars_with_whitespace(tmp_path):
    env = {
        'XDG_DATA_HOME': str(tmp_path / 'xdg' / 'data') + '  ',
        'XDG_CONFIG_HOME': str(tmp_path / 'xdg' / 'config') + '\n',
        'XDG_CACHE_HOME': str(tmp_path / 'xdg' / 'cache') + '\t',
        'XDG_STATE_HOME': str(tmp_path / 'xdg' / 'state') + ' ',
    }
    with mock.patch('platform.system', return_value='Linux'):
        with mock.patch.dict(os.environ, env, clear=True):
            importlib.reload(ph)
            base = tmp_path / 'xdg'
            assert ph.get_app_data_dir() == base / 'data' / 'token.place'
            assert ph.get_config_dir() == base / 'config' / 'token.place' / 'config'
            assert ph.get_cache_dir() == base / 'cache' / 'token.place'
            assert ph.get_logs_dir() == base / 'state' / 'token.place' / 'logs'


def test_paths_windows(tmp_path):
    env = {
        'APPDATA': str(tmp_path / 'AppData' / 'Roaming'),
        'LOCALAPPDATA': str(tmp_path / 'AppData' / 'Local')
    }
    with mock.patch('platform.system', return_value='Windows'):
        with mock.patch.dict(os.environ, env, clear=False):
            importlib.reload(ph)
            base = Path(env['APPDATA'])
            assert ph.get_app_data_dir() == base / 'token.place'
            assert ph.get_config_dir() == base / 'token.place' / 'config'
            assert ph.get_cache_dir() == Path(env['LOCALAPPDATA']) / 'token.place' / 'cache'
            assert ph.get_logs_dir() == base / 'token.place' / 'logs'
            assert ph.get_executable_extension() == '.exe'


def test_paths_windows_missing_env():
    with mock.patch('platform.system', return_value='Windows'):
        with mock.patch.dict(os.environ, {'APPDATA': '', 'LOCALAPPDATA': ''}, clear=False):
            importlib.reload(ph)
            home = Path.home()
            assert ph.get_app_data_dir() == home / 'AppData' / 'Roaming' / 'token.place'
            assert ph.get_config_dir() == home / 'AppData' / 'Roaming' / 'token.place' / 'config'
            assert ph.get_cache_dir() == home / 'AppData' / 'Local' / 'token.place' / 'cache'


def test_paths_macos(tmp_path):
    with mock.patch('platform.system', return_value='Darwin'):
        importlib.reload(ph)
        home = Path.home()
        assert ph.get_app_data_dir() == home / 'Library' / 'Application Support' / 'token.place'
        assert ph.get_config_dir() == home / 'Library' / 'Application Support' / 'token.place' / 'config'
        assert ph.get_cache_dir() == home / 'Library' / 'Caches' / 'token.place'
        assert ph.get_logs_dir() == home / 'Library' / 'Logs' / 'token.place'


def test_unexpanded_env_var_raises():
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError):
            ph.normalize_path("$UNSET_VAR/path")
        with pytest.raises(ValueError):
            ph.ensure_dir_exists("$UNSET_VAR/path")
