from __future__ import annotations

import importlib.util
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(platform.system() != 'Darwin', reason='requires real macOS codesign and hdiutil')


def _validator():
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'validate_desktop_tauri_release_artifacts.py'
    spec = importlib.util.spec_from_file_location('validate_desktop_tauri_release_artifacts', script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _make_app(root: Path) -> Path:
    app = root / 'Minimal.app'
    macos = app / 'Contents' / 'MacOS'
    resources_python = app / 'Contents' / 'Resources' / 'python'
    macos.mkdir(parents=True)
    resources_python.mkdir(parents=True)
    exe = macos / 'Minimal'
    exe.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    exe.chmod(0o755)
    (resources_python / 'fixture_module.py').write_text('VALUE = 42\n', encoding='utf-8')
    (app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleExecutable': 'Minimal',
        'CFBundleIdentifier': 'place.token.MinimalRegression',
        'CFBundleName': 'Minimal',
        'CFBundlePackageType': 'APPL',
        'CFBundleVersion': '1',
    }))
    return app


def test_real_macos_ad_hoc_probe_dmg_and_mutation_guard(tmp_path: Path) -> None:
    validator = _validator()
    app = _make_app(tmp_path)
    _run(['codesign', '--force', '--deep', '--sign', '-', str(app)])
    validator._verify_existing_signature(app)
    before = validator._app_tree_fingerprint(app)

    code = 'import fixture_module, subprocess, sys, os; raise SystemExit(subprocess.run([sys.executable, "-B", "-c", "import fixture_module"], env=os.environ.copy()).returncode)'
    validator._run_python_sanitized(Path(sys.executable), code, app)

    assert not list(app.rglob('__pycache__'))
    assert not list(app.rglob('*.pyc'))
    assert validator._describe_app_tree_mutations(before, validator._app_tree_fingerprint(app)) == []
    validator._verify_existing_signature(app)

    pycache = app / 'Contents' / 'Resources' / 'python' / '__pycache__'
    pycache.mkdir()
    (pycache / 'fixture_module.cpython-311.pyc').write_bytes(b'unsealed')
    mutations = validator._describe_app_tree_mutations(before, validator._app_tree_fingerprint(app))
    assert any('unsealed Python bytecode' in m for m in mutations)

    shutil.rmtree(pycache)
    _run(['codesign', '--force', '--deep', '--sign', '-', str(app)])
    stage = tmp_path / 'dmg-stage'
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / 'README BEFORE OPENING.txt').write_text(
        'This preview build is ad-hoc signed and not notarized. Apple could not verify. '
        'Privacy & Security Developer ID notarization',
        encoding='utf-8',
    )
    dmg = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    _run(['hdiutil', 'create', '-volname', 'Minimal', '-srcfolder', str(stage), '-ov', '-format', 'UDZO', str(dmg)])
    seen = []
    original = validator._verify_existing_signature

    def record(path: Path) -> None:
        seen.append(path)
        original(path)

    validator._verify_existing_signature = record
    validator._validate_dmg_contents(dmg, expect_signing=False, require_embedded_python_runtime=False)
    assert seen and seen[-1].name == app.name
    assert any('/token-place-dmg-mount-' in str(path) for path in seen)
