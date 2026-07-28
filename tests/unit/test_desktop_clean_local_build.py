import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'scripts' / 'clean_local_build.py'
spec = importlib.util.spec_from_file_location('clean_local_build', SCRIPT)
assert spec is not None
assert spec.loader is not None
clean_local_build = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = clean_local_build
spec.loader.exec_module(clean_local_build)


@pytest.fixture
def desktop_tauri_dir(tmp_path: Path) -> Path:
    d = tmp_path / 'desktop-tauri'
    (d / 'src-tauri' / 'target' / 'release').mkdir(parents=True)
    (d / 'src-tauri' / 'gen' / 'schemas').mkdir(parents=True)
    (d / 'release-artifacts').mkdir(parents=True)
    (d / 'dist').mkdir(parents=True)
    (d / 'node_modules' / 'somepkg').mkdir(parents=True)
    runtime = d / 'src-tauri' / 'python-runtime'
    runtime.mkdir(parents=True)
    (runtime / '.gitkeep').write_text('')
    (runtime / 'bin').mkdir()
    (runtime / 'bin' / 'python3').write_text('stub')
    return d


def test_clean_default_removes_project_artifacts_only(desktop_tauri_dir: Path):
    clean_local_build.clean(desktop_tauri_dir)

    assert not (desktop_tauri_dir / 'src-tauri' / 'target').exists()
    assert not (desktop_tauri_dir / 'src-tauri' / 'gen').exists()
    assert not (desktop_tauri_dir / 'release-artifacts').exists()
    assert not (desktop_tauri_dir / 'dist').exists()
    # opt-in only:
    assert (desktop_tauri_dir / 'node_modules').exists()
    assert (desktop_tauri_dir / 'src-tauri' / 'python-runtime' / 'bin').exists()


def test_clean_node_modules_flag(desktop_tauri_dir: Path):
    clean_local_build.clean(desktop_tauri_dir, node_modules=True)
    assert not (desktop_tauri_dir / 'node_modules').exists()


def test_clean_runtime_flag_preserves_gitkeep(desktop_tauri_dir: Path):
    clean_local_build.clean(desktop_tauri_dir, runtime=True)
    runtime = desktop_tauri_dir / 'src-tauri' / 'python-runtime'
    assert runtime.is_dir()
    assert (runtime / '.gitkeep').exists()
    assert not (runtime / 'bin').exists()


def test_clean_cargo_registry_flag_targets_home_cargo(desktop_tauri_dir: Path, monkeypatch, tmp_path: Path):
    fake_home = tmp_path / 'home'
    (fake_home / '.cargo' / 'registry' / 'src').mkdir(parents=True)
    monkeypatch.setattr(clean_local_build.Path, 'home', lambda: fake_home)

    clean_local_build.clean(desktop_tauri_dir, cargo_registry=True)

    assert not (fake_home / '.cargo' / 'registry').exists()
    assert (fake_home / '.cargo').exists()


def test_clean_dry_run_removes_nothing(desktop_tauri_dir: Path):
    clean_local_build.clean(desktop_tauri_dir, node_modules=True, runtime=True, dry_run=True)

    assert (desktop_tauri_dir / 'src-tauri' / 'target').exists()
    assert (desktop_tauri_dir / 'node_modules').exists()
    assert (desktop_tauri_dir / 'src-tauri' / 'python-runtime' / 'bin').exists()


def test_cli_all_flag_enables_everything(monkeypatch, desktop_tauri_dir: Path):
    calls = {}

    def fake_clean(desktop_tauri_dir=None, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(clean_local_build, 'clean', fake_clean)
    rc = clean_local_build.main(['--all'])

    assert rc == 0
    assert calls == {'node_modules': True, 'runtime': True, 'cargo_registry': True, 'dry_run': False}


def test_cli_no_flags_disables_optional_cleanup(monkeypatch):
    calls = {}

    def fake_clean(desktop_tauri_dir=None, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(clean_local_build, 'clean', fake_clean)
    rc = clean_local_build.main([])

    assert rc == 0
    assert calls == {'node_modules': False, 'runtime': False, 'cargo_registry': False, 'dry_run': False}


def test_clean_missing_dirs_are_noop(tmp_path: Path):
    empty = tmp_path / 'nonexistent-desktop-tauri'
    clean_local_build.clean(empty, node_modules=True, runtime=True, cargo_registry=False)
