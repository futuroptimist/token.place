import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from config import Config


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    monkeypatch.setenv("PLATFORM", "linux")
    monkeypatch.setenv("TOKEN_PLACE_CONFIG", str(tmp_path / "user.json"))

    monkeypatch.setattr(
        "utils.path_handling.get_config_dir", lambda: tmp_path / "config"
    )
    monkeypatch.setattr(
        "utils.path_handling.get_app_data_dir", lambda: tmp_path / "data"
    )
    monkeypatch.setattr(
        "utils.path_handling.get_models_dir", lambda: tmp_path / "models"
    )
    monkeypatch.setattr(
        "utils.path_handling.get_logs_dir", lambda: tmp_path / "logs"
    )
    monkeypatch.setattr(
        "utils.path_handling.get_cache_dir", lambda: tmp_path / "cache"
    )
    monkeypatch.setattr(
        "utils.path_handling.ensure_dir_exists",
        lambda p: Path(p).mkdir(parents=True, exist_ok=True),
    )

    return tmp_path


def test_config_overrides_and_save(patched_paths, tmp_path):
    cfg = Config()
    # verify environment overrides
    assert cfg.is_testing
    assert cfg.get("server.port") == 8001
    # set and retrieve value
    cfg.set("server.port", 9000)
    assert cfg.get("server.port") == 9000
    # save config to provided path
    cfg.save_user_config()
    saved = Path(os.environ["TOKEN_PLACE_CONFIG"])
    assert saved.exists()
    data = json.loads(saved.read_text())
    assert data["server"]["port"] == 9000
    # ensure directories were created
    assert (patched_paths / "data").exists()
    assert (patched_paths / "models").exists()
