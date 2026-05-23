from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-release.yml"
TAURI_CONFIG = ROOT / "desktop-tauri" / "src-tauri" / "tauri.conf.json"


def test_desktop_release_workflow_uses_tauri_bundle_paths_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "src-tauri/target/${{ matrix.tauri_target }}/release/bundle" in text
    assert "src-tauri/target/release/bundle/nsis" in text
    assert "src-tauri/target/release/bundle/msi" in text
    assert "banned=(\"tokenplace Desktop\" \"tokenplace Desktop Setup\" \"desktop/electron-builder\")" in text


def test_workflow_has_obvious_apple_silicon_dmg_name_and_stale_branding_guard() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "token.place-desktop-${version}-apple-silicon.dmg" in text
    assert "tokenplace Desktop" in text
    assert "tokenplace Desktop Setup" in text
    assert "validate_desktop_tauri_release_artifacts.py" in text


def test_tauri_icons_include_expected_set() -> None:
    text = TAURI_CONFIG.read_text(encoding="utf-8")
    assert '"icons/icon.icns"' in text
    assert '"icons/icon.ico"' in text
    assert '"icons/128x128@2x.png"' in text
    assert '"icons/128x128.png"' in text
