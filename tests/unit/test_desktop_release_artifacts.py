from __future__ import annotations

import json
from pathlib import Path

WORKFLOW = Path('.github/workflows/desktop-release.yml')
TAURI_CONFIG = Path('desktop-tauri/src-tauri/tauri.conf.json')


def test_workflow_stages_tauri_bundle_and_blocks_legacy_markers() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'src-tauri/target/${{ matrix.tauri_target }}/release/bundle' in text
    assert 'desktop/electron-builder' in text
    assert 'desktop/electron-builder.json' not in text


def test_workflow_uses_obvious_apple_silicon_dmg_name() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'token.place-desktop-${version}-apple-silicon.dmg' in text
    assert 'tokenplace Desktop-0.1.0-arm64.dmg' not in text


def test_workflow_rejects_stale_electron_branding() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'tokenplace Desktop' in text
    assert 'tokenplace Desktop Setup' in text
    assert 'stale Electron branding detected' in text


def test_tauri_icon_set_references_expected_files() -> None:
    config = json.loads(TAURI_CONFIG.read_text(encoding='utf-8'))
    icons = set(config['bundle']['icon'])
    expected = {
        'icons/icon.icns',
        'icons/icon.ico',
        'icons/128x128@2x.png',
        'icons/128x128.png',
        'icons/32x32.png',
    }
    assert expected.issubset(icons)


def test_workflow_sets_explicit_dmg_volume_name() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'hdiutil create -volname "token.place desktop"' in text


def test_workflow_requires_exactly_one_staged_macos_dmg() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'Expected exactly one staged macOS .dmg in release-artifacts' in text


def test_validator_checks_display_name_and_executable_and_dmg_pattern() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert 'CFBundleDisplayName' in text
    assert 'CFBundleExecutable' in text
    assert 'token.place-desktop-<version>-apple-silicon.dmg' in text
