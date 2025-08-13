import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import scripts.validate_dependencies as vd

# Helper to set up fake project structure
def _setup(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    req = project_root / "requirements.txt"
    req.write_text("flask==3.0.0\n")
    fake_script = project_root / "scripts" / "validate_dependencies.py"
    fake_script.parent.mkdir()
    fake_script.write_text("")
    return project_root, fake_script


def test_run_command_success():
    success, out, err = vd.run_command("echo hello")
    assert success and "hello" in out and err == ""


def test_run_command_failure():
    success, out, err = vd.run_command("sh -c 'exit 1'")
    assert not success


def test_run_command_exception(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('fail')
    monkeypatch.setattr(vd.subprocess, 'run', boom)
    success, _, err = vd.run_command('echo')
    assert not success and err


def test_run_command_split_error():
    success, _, err = vd.run_command("echo '")
    assert not success and err


def test_validate_dependencies_success(tmp_path, monkeypatch):
    project_root, fake_script = _setup(tmp_path)
    monkeypatch.setattr(vd, "__file__", str(fake_script))
    outputs = [
        (True, "", ""),
        (True, "Successfully installed pip", ""),
        (True, "", ""),
        (True, "Name: pytest\nVersion: 8.2.0\n", ""),
        (True, "Name: pytest-playwright\nVersion: 0.5\n", ""),
        (True, "Name: playwright\nVersion: 1.0\n", ""),
    ]
    def fake_run_command(cmd, cwd=None):
        return outputs.pop(0)
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


def test_validate_dependencies_venv_fail(tmp_path, monkeypatch):
    project_root, fake_script = _setup(tmp_path)
    monkeypatch.setattr(vd, "__file__", str(fake_script))
    def fake_run_command(cmd, cwd=None):
        return False, "", "err"
    monkeypatch.setattr(vd, "run_command", fake_run_command)
    assert vd.validate_dependencies() is False


def test_validate_dependencies_pip_upgrade_fail(tmp_path, monkeypatch):
    project_root, fake_script = _setup(tmp_path)
    monkeypatch.setattr(vd, "__file__", str(fake_script))
    outputs = [
        (True, "", ""),
        (False, "", "pip error"),
    ]
    def fake_run_command(cmd, cwd=None):
        return outputs.pop(0)
    monkeypatch.setattr(vd, "run_command", fake_run_command)
    assert vd.validate_dependencies() is False


def test_validate_dependencies_install_fail_conflict(tmp_path, monkeypatch):
    project_root, fake_script = _setup(tmp_path)
    monkeypatch.setattr(vd, "__file__", str(fake_script))
    outputs = [
        (True, "", ""),
        (True, "", ""),
        (False, "", "ResolutionImpossible"),
    ]
    def fake_run_command(cmd, cwd=None):
        return outputs.pop(0)
    monkeypatch.setattr(vd, "run_command", fake_run_command)
    assert vd.validate_dependencies() is False


def test_validate_dependencies_old_pytest(tmp_path, monkeypatch):
    project_root, fake_script = _setup(tmp_path)
    monkeypatch.setattr(vd, "__file__", str(fake_script))
    outputs = [
        (True, "", ""),
        (True, "", ""),
        (True, "", ""),
        (True, "Name: pytest\nVersion: 7.0.0\n", ""),
    ]
    def fake_run_command(cmd, cwd=None):
        return outputs.pop(0)
    monkeypatch.setattr(vd, "run_command", fake_run_command)
    assert vd.validate_dependencies() is False


def test_validate_dependencies_windows_paths(tmp_path, monkeypatch):
    project_root, fake_script = _setup(tmp_path)
    monkeypatch.setattr(vd, "__file__", str(fake_script))
    monkeypatch.setattr(vd, 'os', type('osmod', (), {'name': 'nt'}))
    outputs = [
        (True, '', ''),
        (True, 'Successfully installed pip', ''),
        (True, '', ''),
        (True, 'Name: pytest\nVersion: 8.2.0\n', ''),
        (True, '', ''),
        (True, '', ''),
    ]
    def fake_run_command(cmd, cwd=None):
        return outputs.pop(0)
    monkeypatch.setattr(vd, 'run_command', fake_run_command)
    assert vd.validate_dependencies() is True


def test_main_success(monkeypatch):
    monkeypatch.setattr(vd, "validate_dependencies", lambda: True)
    exits = []
    monkeypatch.setattr(sys, "exit", lambda c: exits.append(c))
    vd.main()
    assert exits == [0]


def test_main_failure(monkeypatch):
    monkeypatch.setattr(vd, "validate_dependencies", lambda: False)
    exits = []
    monkeypatch.setattr(sys, "exit", lambda c: exits.append(c))
    vd.main()
    assert exits == [1]


def test_main_called_via_exec(monkeypatch):
    code = "\n" * 133 + "main()"
    called = []
    monkeypatch.setattr(vd, "main", lambda: called.append(True))
    compiled = compile(code, vd.__file__, "exec")
    exec(compiled, {"main": vd.main})
    assert called == [True]
