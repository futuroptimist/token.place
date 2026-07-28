import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'scripts' / 'build_local.py'
spec = importlib.util.spec_from_file_location('build_local', SCRIPT)
assert spec is not None
assert spec.loader is not None
build_local = importlib.util.module_from_spec(spec)
# Register before exec: dataclasses with `from __future__ import annotations` resolve
# field types via sys.modules[cls.__module__], which is unset for a spec-loaded module
# that was never registered, breaking on Python < 3.10 (e.g. system Python 3.9).
sys.modules[spec.name] = build_local
spec.loader.exec_module(build_local)


@pytest.fixture
def desktop_tauri_dir(tmp_path: Path) -> Path:
    d = tmp_path / 'desktop-tauri'
    d.mkdir()
    (d / 'package.json').write_text(json.dumps({'name': 'token-place-desktop-tauri', 'version': '0.1.6'}))
    return d


def test_read_package_version(desktop_tauri_dir: Path):
    assert build_local.read_package_version(desktop_tauri_dir) == '0.1.6'


def test_plan_macos_steps_includes_build_and_stage(desktop_tauri_dir: Path, monkeypatch):
    monkeypatch.setattr(build_local, 'git_short_sha', lambda cwd=None: 'abc1234')
    steps = build_local.plan_macos_steps(desktop_tauri_dir)
    descriptions = [s.description for s in steps]
    assert any('npm ci' in d for d in descriptions)
    assert any('Prepare embedded macOS Python runtime' in d for d in descriptions)
    assert any('Build Tauri bundles' in d for d in descriptions)
    assert any('Stage .app and build local .dmg' in d for d in descriptions)

    build_step = next(s for s in steps if 'Build Tauri bundles' in s.description)
    assert build_step.argv[:2] == [build_local._exe('bash'), '-c']
    assert '--target aarch64-apple-darwin --bundles app' in build_step.argv[2]

    stage_step = next(s for s in steps if 'Stage .app' in s.description)
    assert 'token.place-desktop-local-0.1.6-abc1234-apple-silicon.dmg' in stage_step.argv[2]


def test_plan_macos_steps_skip_install(desktop_tauri_dir: Path, monkeypatch):
    monkeypatch.setattr(build_local, 'git_short_sha', lambda cwd=None: 'abc1234')
    steps = build_local.plan_macos_steps(desktop_tauri_dir, skip_install=True)
    assert not any('npm ci' in s.description for s in steps)


def test_plan_macos_steps_skip_validate_omits_validator_call(desktop_tauri_dir: Path, monkeypatch):
    monkeypatch.setattr(build_local, 'git_short_sha', lambda cwd=None: 'abc1234')
    steps = build_local.plan_macos_steps(desktop_tauri_dir, skip_validate=True)
    stage_step = next(s for s in steps if 'Stage .app' in s.description)
    assert 'validate_desktop_tauri_release_artifacts.py' not in stage_step.argv[2]


def test_plan_windows_steps_runs_prep_when_runtime_missing(desktop_tauri_dir: Path):
    steps = build_local.plan_windows_steps(desktop_tauri_dir)
    descriptions = [s.description for s in steps]
    assert any('Prepare embedded Windows CUDA Python runtime' in d for d in descriptions)
    assert not any('Skip embedded Windows runtime prep' in d for d in descriptions)


def test_plan_windows_steps_skips_prep_when_runtime_present(desktop_tauri_dir: Path):
    runtime_dir = desktop_tauri_dir / 'src-tauri' / 'python-runtime'
    runtime_dir.mkdir(parents=True)
    (runtime_dir / 'python.exe').write_text('stub')

    steps = build_local.plan_windows_steps(desktop_tauri_dir)
    descriptions = [s.description for s in steps]
    assert any('Skip embedded Windows runtime prep' in d for d in descriptions)
    assert not any('Prepare embedded Windows CUDA Python runtime' in d for d in descriptions)

    skip_step = next(s for s in steps if 'Skip embedded Windows runtime prep' in s.description)
    assert skip_step.argv == []


def test_plan_windows_steps_fresh_runtime_forces_prep(desktop_tauri_dir: Path):
    runtime_dir = desktop_tauri_dir / 'src-tauri' / 'python-runtime'
    runtime_dir.mkdir(parents=True)
    (runtime_dir / 'python.exe').write_text('stub')

    steps = build_local.plan_windows_steps(desktop_tauri_dir, fresh_runtime=True)
    assert any('Prepare embedded Windows CUDA Python runtime' in s.description for s in steps)


def test_plan_windows_steps_build_command(desktop_tauri_dir: Path):
    steps = build_local.plan_windows_steps(desktop_tauri_dir)
    build_step = next(s for s in steps if 'Build Tauri bundles' in s.description)
    assert build_step.argv[-4:] == ['build', '--', '--target', 'x86_64-pc-windows-msvc']


def test_run_steps_dry_run_does_not_execute(monkeypatch, desktop_tauri_dir: Path):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append((a, k)))
    steps = [build_local.Step('do a thing', ['echo', 'hi'], desktop_tauri_dir)]
    build_local.run_steps(steps, dry_run=True)
    assert calls == []


def test_run_steps_raises_on_nonzero_exit(monkeypatch, desktop_tauri_dir: Path):
    class FakeResult:
        returncode = 1

    monkeypatch.setattr(build_local.subprocess, 'run', lambda *a, **k: FakeResult())
    steps = [build_local.Step('fails', ['false'], desktop_tauri_dir)]
    with pytest.raises(build_local.BuildLocalError):
        build_local.run_steps(steps, dry_run=False)


def test_run_steps_no_argv_step_is_a_noop(monkeypatch, desktop_tauri_dir: Path):
    calls = []
    monkeypatch.setattr(build_local.subprocess, 'run', lambda *a, **k: calls.append((a, k)))
    steps = [build_local.Step('skip', [], desktop_tauri_dir)]
    build_local.run_steps(steps, dry_run=False)
    assert calls == []


def test_stage_windows_artifacts_dry_run_returns_empty(desktop_tauri_dir: Path):
    assert build_local.stage_windows_artifacts(desktop_tauri_dir, dry_run=True) == []


def test_stage_windows_artifacts_missing_nsis_raises(desktop_tauri_dir: Path):
    bundle_root = desktop_tauri_dir / 'src-tauri' / 'target' / 'x86_64-pc-windows-msvc' / 'release' / 'bundle'
    (bundle_root / 'msi').mkdir(parents=True)
    (bundle_root / 'msi' / 'token.place.msi').write_text('stub')
    (bundle_root / 'nsis').mkdir(parents=True)
    with pytest.raises(build_local.BuildLocalError, match='No setup EXE'):
        build_local.stage_windows_artifacts(desktop_tauri_dir, skip_validate=True)


def test_stage_windows_artifacts_copies_and_skips_validate(desktop_tauri_dir: Path):
    bundle_root = desktop_tauri_dir / 'src-tauri' / 'target' / 'x86_64-pc-windows-msvc' / 'release' / 'bundle'
    (bundle_root / 'nsis').mkdir(parents=True)
    (bundle_root / 'nsis' / 'token.place-desktop_0.1.6_x64-setup.exe').write_text('stub')
    (bundle_root / 'msi').mkdir(parents=True)
    (bundle_root / 'msi' / 'token.place-desktop_0.1.6_x64_en-US.msi').write_text('stub')

    staged = build_local.stage_windows_artifacts(desktop_tauri_dir, skip_validate=True)
    names = sorted(p.name for p in staged)
    assert names == ['token.place-desktop_0.1.6_x64-setup.exe', 'token.place-desktop_0.1.6_x64_en-US.msi']
    for p in staged:
        assert p.parent == desktop_tauri_dir / 'release-artifacts'
        assert p.exists()


def test_cli_dry_run_exits_zero_without_subprocess(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(build_local, 'DESKTOP_TAURI_DIR', tmp_path)
    monkeypatch.setattr(build_local.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(build_local, 'plan_macos_steps', lambda **kw: [build_local.Step('noop', ['true'], tmp_path)])
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append((a, k)))

    rc = build_local.main(['--dry-run'])

    assert rc == 0
    assert calls == []


def test_cli_unsupported_platform_returns_1(monkeypatch):
    monkeypatch.setattr(build_local.platform, 'system', lambda: 'Linux')
    assert build_local.main(['--dry-run']) == 1


def test_run_steps_missing_executable_raises_clean_error(monkeypatch, desktop_tauri_dir: Path):
    def fake_run(*a, **k):
        raise FileNotFoundError(2, 'No such file or directory', 'rustup')

    monkeypatch.setattr(build_local.subprocess, 'run', fake_run)
    steps = [build_local.Step('Add Rust target', ['rustup', 'target', 'add', 'aarch64-apple-darwin'], desktop_tauri_dir)]
    with pytest.raises(build_local.BuildLocalError, match="'rustup' not found on PATH"):
        build_local.run_steps(steps, dry_run=False)


def test_check_prerequisites_reports_missing_tools(monkeypatch):
    monkeypatch.setattr(build_local.shutil, 'which', lambda name: None)
    assert build_local.check_prerequisites('Darwin') == ['rustup', 'npm', 'python3']
    assert build_local.check_prerequisites('Windows') == ['rustup', 'npm', 'python']


def test_check_prerequisites_empty_when_all_present(monkeypatch):
    monkeypatch.setattr(build_local.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(build_local, '_python_version', lambda exe: (3, 12))
    assert build_local.check_prerequisites('Darwin') == []


def test_find_python_skips_versions_below_minimum(monkeypatch):
    monkeypatch.setattr(build_local.shutil, 'which', lambda name: f'/usr/bin/{name}' if name in ('python3.11', 'python3') else None)

    def fake_version(exe):
        return (3, 9) if exe.endswith('python3') else (3, 11)

    monkeypatch.setattr(build_local, '_python_version', fake_version)
    assert build_local.find_python('Darwin') == '/usr/bin/python3.11'


def test_find_python_returns_none_when_only_stale_stub_present(monkeypatch):
    monkeypatch.setattr(build_local.shutil, 'which', lambda name: '/usr/bin/python3' if name == 'python3' else None)
    monkeypatch.setattr(build_local, '_python_version', lambda exe: (3, 9))
    assert build_local.find_python('Darwin') is None


def test_cli_reports_missing_prerequisites_without_running_steps(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(build_local, 'DESKTOP_TAURI_DIR', tmp_path)
    monkeypatch.setattr(build_local.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(build_local, 'check_prerequisites', lambda system: ['rustup'])
    calls = []
    monkeypatch.setattr(build_local, 'plan_macos_steps', lambda **kw: calls.append('planned') or [])
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append(('run', a, k)))

    rc = build_local.main([])

    assert rc == 1
    assert ('run', ) not in [c[:1] for c in calls if isinstance(c, tuple)]
