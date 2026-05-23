from __future__ import annotations

from pathlib import Path


WORKFLOW_PATH = Path('.github/workflows/desktop-release.yml')
TAURI_CONF_PATH = Path('desktop-tauri/src-tauri/tauri.conf.json')


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_workflow_stages_macos_from_tauri_bundle_only() -> None:
    content = _read(WORKFLOW_PATH)
    assert 'src-tauri/target/${{ matrix.tauri_target }}/release/bundle' in content
    assert 'python ../scripts/validate_desktop_tauri_release_artifacts.py' in content
    assert 'desktop/electron-builder.json' not in content


def test_workflow_uses_obvious_apple_silicon_name_and_rejects_stale_branding() -> None:
    content = _read(WORKFLOW_PATH)
    assert 'token.place-desktop-${version}-apple-silicon.dmg' in content
    assert 'tokenplace Desktop|tokenplace Desktop Setup|desktop/electron-builder' in content


def test_workflow_validates_macos_artifacts_with_tauri_guardrail_script() -> None:
    content = _read(WORKFLOW_PATH)
    assert 'python ../scripts/validate_desktop_tauri_release_artifacts.py' in content
    assert '--source-icon "src-tauri/icons/icon.icns"' in content


def test_tauri_bundle_config_references_expected_icon_set() -> None:
    content = _read(TAURI_CONF_PATH)
    assert '"icons/128x128@2x.png"' in content
    assert '"icons/icon.icns"' in content
    assert '"icons/icon.ico"' in content
