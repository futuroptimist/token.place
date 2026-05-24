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


def test_workflow_uses_ad_hoc_signing_fallback_without_paid_secrets() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert "export TAURI_BUNDLE_MACOS_SIGNING_IDENTITY='-'" in text
    assert "export APPLE_SIGNING_IDENTITY='-'" in text
    assert 'using ad-hoc signing for preview/dev-only macOS artifacts' in text


def test_workflow_does_not_gate_release_on_notary_profile() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'APPLE_NOTARYTOOL_KEYCHAIN_PROFILE is set, but notarization/stapling is not performed' in text
    assert 'skipping strict Gatekeeper notarization enforcement' in text
    assert 'signing_flag="--expect-signing"' in text


def test_workflow_emits_preview_warning_asset_for_macos_downloads() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'README-macos-apple-silicon-preview.txt' in text
    assert 'README BEFORE OPENING.txt' in text
    assert 'This preview build is ad-hoc signed and not notarized.' in text
    assert 'Apple could not verify \\"token.place desktop\\" is free of malware.' in text
    assert 'Done.' in text
    assert 'System Settings -> Privacy & Security' in text
    assert 'Open Anyway / Allow / Open' in text
    assert 'Developer ID signing + notarization' in text
    assert 'This is expected for unpaid/non-notarized GitHub preview releases.' in text
    assert 'System Settings -> Privacy & Security' in text
    assert 'Only install if you trust this GitHub release and checksum files.' in text


def test_workflow_stages_macos_dmg_with_app_readme_and_applications_symlink() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'dmg_stage_dir="$RUNNER_TEMP/token-place-desktop-dmg-stage"' in text
    assert 'cp "${preview_notice}" "${dmg_stage_dir}/README BEFORE OPENING.txt"' in text
    assert 'cp -R "${app_path}" "${dmg_stage_dir}/$(basename "${app_path}")"' in text
    assert 'ln -s /Applications "${dmg_stage_dir}/Applications"' in text
    assert 'hdiutil create -volname "token.place desktop" -srcfolder "${dmg_stage_dir}"' in text
    assert 'hdiutil create -volname "token.place desktop" -srcfolder "${app_path}"' not in text


def test_validator_checks_display_name_and_executable_and_dmg_pattern() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert 'CFBundleDisplayName' in text
    assert 'CFBundleExecutable' in text
    assert 'token.place-desktop-<version>-apple-silicon.dmg' in text


def test_workflow_writes_preview_notice_via_printf() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert "printf '%s\\n' \\" in text
    assert '> "${preview_notice}"' in text


def test_validator_checks_dmg_root_preview_readme_contents() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert 'README BEFORE OPENING.txt' in text
    assert 'README-macos-apple-silicon-preview.txt' in text
    assert 'Expected exactly one .app at DMG root' in text
    assert 'ad-hoc signed' in text
    assert 'not notarized' in text
    assert 'Apple could not verify' in text
    assert 'Privacy & Security' in text
    assert 'Developer ID' in text
    assert 'notarization' in text


def test_preview_notice_uses_full_signing_decision_in_stage_step() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'APPLE_SIGNING_IDENTITY: ${{ secrets.APPLE_SIGNING_IDENTITY }}' in text
    assert 'APPLE_CERTIFICATE_P12_BASE64: ${{ secrets.APPLE_CERTIFICATE_P12_BASE64 }}' in text
    assert 'APPLE_CERTIFICATE_PASSWORD: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}' in text
    assert 'if [ -n "${APPLE_SIGNING_IDENTITY:-}" ] && [ -n "${APPLE_CERTIFICATE_P12_BASE64:-}" ] && [ -n "${APPLE_CERTIFICATE_PASSWORD:-}" ]; then' in text
