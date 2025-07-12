import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import scripts.validate_dependencies as vd


def test_validate_dependencies_success(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    req = project_root / "requirements.txt"
    req.write_text("flask==3.0.0\n")
    fake_script = project_root / "scripts" / "validate_dependencies.py"
    fake_script.parent.mkdir()
    fake_script.write_text("")

    monkeypatch.setattr(vd, "__file__", str(fake_script))

    def fake_run_command(cmd, cwd=None):
        return True, "ok", ""

    monkeypatch.setattr(vd, "run_command", fake_run_command)

    assert vd.validate_dependencies() is True


def test_validate_dependencies_missing_file(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    fake_script = project_root / "scripts" / "validate_dependencies.py"
    fake_script.parent.mkdir()
    fake_script.write_text("")

    monkeypatch.setattr(vd, "__file__", str(fake_script))

    with patch.object(vd, "run_command") as mock_run:
        result = vd.validate_dependencies()
        mock_run.assert_not_called()
    assert result is False
