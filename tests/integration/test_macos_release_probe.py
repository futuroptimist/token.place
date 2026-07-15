from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _validator():
    script_path = Path('scripts/validate_desktop_tauri_release_artifacts.py')
    spec = importlib.util.spec_from_file_location('validate_desktop_tauri_release_artifacts', script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(platform.system() != 'Darwin', reason='requires real macOS codesign and hdiutil')
def test_real_macos_sanitized_probe_preserves_ad_hoc_signature_and_dmg_mount(tmp_path) -> None:
    validator = _validator()
    app = tmp_path / 'Probe.app'
    resources = app / 'Contents' / 'Resources' / 'python'
    macos = app / 'Contents' / 'MacOS'
    resources.mkdir(parents=True)
    macos.mkdir(parents=True)
    (app / 'Contents' / 'Info.plist').write_text(
        '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict>'
        '<key>CFBundleExecutable</key><string>probe</string><key>CFBundleIdentifier</key><string>place.token.probe</string>'
        '<key>CFBundleName</key><string>Probe</string></dict></plist>',
        encoding='utf-8',
    )
    exe = macos / 'probe'
    exe.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    exe.chmod(0o755)
    (resources / 'probe_module.py').write_text('VALUE = 42\n', encoding='utf-8')

    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)], check=True)
    before = validator._app_tree_fingerprint(app)
    validator._run_python_sanitized(
        Path(sys.executable),
        "import probe_module, subprocess, sys; assert probe_module.VALUE == 42; subprocess.check_call([sys.executable, '-B', '-c', 'import probe_module; assert probe_module.VALUE == 42'])",
        app,
    )
    assert not list(app.rglob('__pycache__'))
    assert not list(app.rglob('*.pyc'))
    assert validator._describe_app_tree_changes(before, validator._app_tree_fingerprint(app)) == []
    subprocess.run(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)], check=True)

    def mutating_failure() -> None:
        pycache = resources / '__pycache__'
        pycache.mkdir()
        (pycache / 'probe_module.cpython-311.pyc').write_bytes(b'unsealed')
        raise RuntimeError('probe failed after mutation')

    with pytest.raises(SystemExit) as excinfo:
        validator._run_with_app_mutation_guard(app, 'intentional mutating probe', mutating_failure)
    assert 'intentional mutating probe mutated app bundle' in str(excinfo.value)
    assert 'added: Contents/Resources/python/__pycache__/probe_module.cpython-311.pyc (unsealed Python bytecode)' in str(excinfo.value)
    shutil.rmtree(resources / '__pycache__')

    stage = tmp_path / 'stage'
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / 'README BEFORE OPENING.txt').write_text(
        'This preview build is ad-hoc signed and not notarized. Apple could not verify. Privacy & Security. Developer ID notarization.',
        encoding='utf-8',
    )
    dmg = tmp_path / 'token.place-desktop-probe-apple-silicon.dmg'
    subprocess.run(['hdiutil', 'create', '-volname', 'Probe', '-srcfolder', str(stage), '-ov', '-format', 'UDZO', str(dmg)], check=True)

    mount = validator._attach_dmg_with_retries(dmg)
    try:
        root = Path(mount.name)
        mounted_apps = sorted(p for p in root.iterdir() if p.is_dir() and p.suffix == '.app')
        assert len(mounted_apps) == 1
        mounted_app = mounted_apps[0]
        mounted_before = validator._app_tree_fingerprint(mounted_app)
        validator._run_python_sanitized(
            Path(sys.executable),
            "import probe_module, subprocess, sys; assert probe_module.VALUE == 42; subprocess.check_call([sys.executable, '-B', '-c', 'import probe_module; assert probe_module.VALUE == 42'])",
            mounted_app,
        )
        assert not list(mounted_app.rglob('__pycache__'))
        assert not list(mounted_app.rglob('*.pyc'))
        assert validator._describe_app_tree_changes(mounted_before, validator._app_tree_fingerprint(mounted_app)) == []
        subprocess.run(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(mounted_app)], check=True)
    finally:
        validator._cleanup_dmg_attach_state(dmg, Path(mount.name))
        mount.cleanup()

    validator._validate_dmg_contents(dmg, expect_signing=False, require_embedded_python_runtime=False)
