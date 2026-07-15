from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(platform.system() != 'Darwin', reason='requires real macOS codesign and hdiutil')


def _validator():
    script = Path('scripts/validate_desktop_tauri_release_artifacts.py')
    spec = importlib.util.spec_from_file_location('validate_desktop_tauri_release_artifacts', script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert result.returncode == 0, f"{cmd} failed\nstdout={result.stdout}\nstderr={result.stderr}"
    return f"{result.stdout}\n{result.stderr}"


def _minimal_app(tmp_path: Path) -> Path:
    app = tmp_path / 'Probe.app'
    macos = app / 'Contents' / 'MacOS'
    resources_python = app / 'Contents' / 'Resources' / 'python'
    macos.mkdir(parents=True)
    resources_python.mkdir(parents=True)
    executable = macos / 'Probe'
    executable.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    executable.chmod(0o755)
    (app / 'Contents' / 'Info.plist').write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Probe</string>
<key>CFBundleIdentifier</key><string>place.token.probe</string>
<key>CFBundleName</key><string>Probe</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
''',
        encoding='utf-8',
    )
    (resources_python / 'probe_module.py').write_text('VALUE = 123\n', encoding='utf-8')
    return app


def test_real_macos_ad_hoc_signature_survives_sanitized_probe_and_dmg_mount(tmp_path) -> None:
    validator = _validator()
    app = _minimal_app(tmp_path)
    py = Path(shutil.which('python3') or '')
    assert py.exists(), 'python3 is required on macOS runner'

    _run(['codesign', '--force', '--deep', '--sign', '-', str(app)])
    _run(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)])
    before = validator._app_tree_fingerprint(app)

    validator._run_python_sanitized(py, 'import probe_module; print(probe_module.VALUE)', app)

    assert validator._describe_app_tree_mutations(before, validator._app_tree_fingerprint(app)) == []
    assert not list(app.rglob('__pycache__'))
    assert not list(app.rglob('*.pyc'))
    _run(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)])

    pycache = app / 'Contents' / 'Resources' / 'python' / '__pycache__'
    pycache.mkdir()
    (pycache / 'probe_module.cpython-311.pyc').write_bytes(b'unsealed')
    changes = validator._describe_app_tree_mutations(before, validator._app_tree_fingerprint(app))
    assert any('unsealed Python bytecode' in change for change in changes)

    shutil.rmtree(pycache)
    _run(['codesign', '--force', '--deep', '--sign', '-', str(app)])

    stage = tmp_path / 'stage'
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / 'README BEFORE OPENING.txt').write_text(
        'This preview build is ad-hoc signed and not notarized. Apple could not verify. Privacy & Security. Developer ID notarization.',
        encoding='utf-8',
    )
    dmg = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    _run(['hdiutil', 'create', '-volname', 'Probe', '-srcfolder', str(stage), '-ov', '-format', 'UDZO', str(dmg)])

    handle = validator._attach_dmg_with_retries(dmg)
    try:
        mounted_app = next(Path(handle.name).glob('*.app'))
        assert mounted_app.name == app.name
        validator._verify_existing_macos_signature(mounted_app)
        validator._run_python_sanitized(py, 'import probe_module; print(probe_module.VALUE)', mounted_app)
    finally:
        validator._cleanup_dmg_attach_state(dmg, Path(handle.name))
        handle.cleanup()
