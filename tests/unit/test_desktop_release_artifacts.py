from __future__ import annotations

from pathlib import Path

WORKFLOW_PATH = Path('.github/workflows/desktop-release.yml')
TAURI_CONF_PATH = Path('desktop-tauri/src-tauri/tauri.conf.json')


def test_desktop_release_workflow_uses_tauri_bundle_path_only() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding='utf-8')
    assert 'src-tauri/target/${{ matrix.tauri_target }}/release/bundle' in workflow
    assert 'nsis_dir="src-tauri/target/release/bundle/nsis"' in workflow
    assert 'msi_dir="src-tauri/target/release/bundle/msi"' in workflow


def test_desktop_release_workflow_uses_clear_apple_silicon_dmg_name() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding='utf-8')
    assert 'token.place-desktop-${version}-apple-silicon.dmg' in workflow
    assert 'tokenplace Desktop-0.1.0-arm64.dmg' not in workflow


def test_desktop_release_workflow_rejects_stale_electron_branding() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding='utf-8')
    assert 'tokenplace Desktop|tokenplace Desktop Setup|desktop/electron-builder' in workflow


def test_tauri_config_uses_expected_icon_set() -> None:
    config = TAURI_CONF_PATH.read_text(encoding='utf-8')
    assert 'icons/icon.icns' in config
    assert 'icons/icon.ico' in config
    assert 'icons/128x128@2x.png' in config
    assert 'icons/128x128.png' in config
