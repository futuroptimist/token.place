import importlib
import os
import pathlib
import platform
from unittest import mock

import pytest
from utils import path_handling as ph


def test_ensure_dir_exists_existing(tmp_path):
    p = tmp_path / 'sub'
    p.mkdir()
    result = ph.ensure_dir_exists(p)
    assert result == p


def test_ensure_dir_exists_file(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("data")
    with pytest.raises(NotADirectoryError):
        ph.ensure_dir_exists(file_path)


def test_ensure_dir_exists_expands_user(tmp_path, monkeypatch):
    """ensure_dir_exists should expand '~' to the user's home directory"""
    monkeypatch.setenv("HOME", str(tmp_path))
    path_with_tilde = "~/nested"
    result = ph.ensure_dir_exists(path_with_tilde)
    assert result == tmp_path / "nested"
    assert result.exists()


def test_ensure_dir_exists_expands_env_vars(tmp_path, monkeypatch):
    """ensure_dir_exists should expand environment variables"""
    monkeypatch.setenv("TEST_BASE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    if ph.IS_WINDOWS:
        path_with_var = "%TEST_BASE%\\envdir"
    else:
        path_with_var = "$TEST_BASE/envdir"
    result = ph.ensure_dir_exists(path_with_var)
    assert result == tmp_path / "envdir"
    assert result.exists()


def test_ensure_dir_exists_strips_whitespace(tmp_path):
    """ensure_dir_exists should strip surrounding whitespace from the path"""
    path_with_spaces = f"  {tmp_path / 'spaced'}  "
    result = ph.ensure_dir_exists(path_with_spaces)
    assert result == tmp_path / "spaced"
    assert result.exists()


def test_normalize_path_expands_env_vars(tmp_path, monkeypatch):
    """normalize_path should expand environment variables"""
    monkeypatch.setenv("TEST_BASE", str(tmp_path))
    target = tmp_path / "nested"
    target.mkdir()
    if ph.IS_WINDOWS:
        path_with_var = "%TEST_BASE%\\nested"
    else:
        path_with_var = "$TEST_BASE/nested"
    result = ph.normalize_path(path_with_var)
    assert result == target


def test_normalize_path_strips_whitespace(tmp_path):
    """normalize_path should strip leading/trailing whitespace"""
    target = tmp_path / "clean"
    target.mkdir()
    path_with_spaces = f"  {target}  "
    result = ph.normalize_path(path_with_spaces)
    assert result == target


def test_get_relative_path_not_relative(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = ph.get_relative_path(a, b)
    assert result == pathlib.Path("..") / "a"


def test_get_relative_path_default_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    result = ph.get_relative_path(sub)
    assert result == pathlib.Path("sub")


def test_get_app_data_dir_creates_directory(tmp_path, monkeypatch):
    """get_app_data_dir should ensure the directory exists"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    importlib.reload(ph)
    app_dir = ph.get_app_data_dir()
    expected = tmp_path / ".local" / "share" / "token.place"
    assert app_dir == expected
    assert app_dir.exists()


def test_linux_uses_xdg_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg" / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg" / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg" / "cache"))
    with mock.patch('platform.system', return_value='Linux'):
        importlib.reload(ph)
        assert ph.get_app_data_dir() == tmp_path / "xdg" / "data" / "token.place"
        assert ph.get_config_dir() == tmp_path / "xdg" / "config" / "token.place"
        assert ph.get_cache_dir() == tmp_path / "xdg" / "cache" / "token.place"


def test_get_relative_path_relpath_error(monkeypatch, tmp_path):
    """Return absolute path when os.path.relpath raises ValueError."""

    def _raise(*_args, **_kwargs):
        raise ValueError("different drives")

    monkeypatch.setattr(os.path, "relpath", _raise)
    base = tmp_path / "base"
    target = tmp_path / "target"
    base.mkdir()
    target.mkdir()
    result = ph.get_relative_path(target, base)
    assert result == target


def test_normalize_path_none():
    """normalize_path should reject None values"""
    with pytest.raises(TypeError):
        ph.normalize_path(None)


def test_ensure_dir_exists_none():
    """ensure_dir_exists should reject None values"""
    with pytest.raises(TypeError):
        ph.ensure_dir_exists(None)
