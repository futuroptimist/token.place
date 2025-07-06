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
