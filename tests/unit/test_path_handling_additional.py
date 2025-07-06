from utils import path_handling as ph


def test_ensure_dir_exists_existing(tmp_path):
    p = tmp_path / 'sub'
    p.mkdir()
    result = ph.ensure_dir_exists(p)
    assert result == p
