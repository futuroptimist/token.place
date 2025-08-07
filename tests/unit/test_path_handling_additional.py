import pathlib
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


def test_get_relative_path_not_relative(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = ph.get_relative_path(a, b)
    assert result == a.resolve()


def test_get_relative_path_default_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    result = ph.get_relative_path(sub)
    assert result == pathlib.Path("sub")
