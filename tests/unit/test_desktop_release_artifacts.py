from __future__ import annotations

import json
import subprocess
from pathlib import Path

WORKFLOW = Path('.github/workflows/desktop-release.yml')
TAURI_CONFIG = Path('desktop-tauri/src-tauri/tauri.conf.json')


def _load_release_artifact_validator():
    import importlib.util

    script_path = Path('scripts/validate_desktop_tauri_release_artifacts.py')
    spec = importlib.util.spec_from_file_location('validate_desktop_tauri_release_artifacts', script_path)
    assert spec is not None
    assert spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator


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


def test_workflow_stages_dmg_directory_with_app_readme_and_applications_symlink() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'dmg_stage_dir="$RUNNER_TEMP/token-place-dmg-stage"' in text
    assert 'cp -R "${app_path}" "${dmg_stage_dir}/"' in text
    assert 'cp "${preview_notice}" "${dmg_stage_dir}/${preview_notice_name}"' in text
    assert 'ln -s /Applications "${dmg_stage_dir}/Applications"' in text
    assert '-srcfolder "${dmg_stage_dir}"' in text
    assert '-srcfolder "${app_path}"' not in text


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
    assert 'preview_notice_name="README BEFORE OPENING.txt"' in text
    assert 'README-macos-apple-silicon-preview.txt' in text
    assert 'This preview build is ad-hoc signed and not notarized.' in text
    assert 'This preview build is signed with the configured Apple signing identity, but it is not notarized.' in text
    assert 'This preview build is ad-hoc signed and not notarized for public Gatekeeper trust.' not in text
    assert 'Apple could not verify' in text
    assert 'token.place desktop' in text
    assert 'click Done' in text
    assert 'System Settings -> Privacy & Security' in text
    assert 'Open Anyway / Allow / Open for token.place desktop' in text
    assert 'Control-click (right-click) the app and choose Open when available.' in text
    assert 'System Settings -> Privacy & Security' in text
    assert 'paid Developer ID signing + notarization' in text


def test_validator_checks_display_name_and_executable_and_dmg_pattern() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert 'CFBundleDisplayName' in text
    assert 'CFBundleExecutable' in text
    assert 'token.place-desktop-<version>-apple-silicon.dmg' in text
    assert 'DMG_PREVIEW_README_NAMES' in text
    assert 'DMG_PREVIEW_REQUIRED_PHRASES' in text
    assert 'platform.system()' in text
    assert 'hdiutil' in text


def test_workflow_writes_preview_notice_via_printf() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert "printf '%s\\n' \\" in text
    assert '> "${preview_notice}"' in text


def test_preview_notice_uses_full_signing_decision_in_stage_step() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'APPLE_SIGNING_IDENTITY: ${{ secrets.APPLE_SIGNING_IDENTITY }}' in text
    assert 'APPLE_CERTIFICATE_P12_BASE64: ${{ secrets.APPLE_CERTIFICATE_P12_BASE64 }}' in text
    assert 'APPLE_CERTIFICATE_PASSWORD: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}' in text
    assert 'if [ -n "${APPLE_SIGNING_IDENTITY:-}" ] && [ -n "${APPLE_CERTIFICATE_P12_BASE64:-}" ] && [ -n "${APPLE_CERTIFICATE_PASSWORD:-}" ]; then' in text


def test_validator_retries_transient_hdiutil_attach_errors(monkeypatch) -> None:
    import subprocess

    validator = _load_release_artifact_validator()
    calls = []

    def fake_run(cmd, *, check, capture_output, text):
        calls.append(cmd)
        assert check is False
        assert capture_output is True
        assert text is True
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 1, '', 'hdiutil: attach failed - Resource temporarily unavailable')
        return subprocess.CompletedProcess(cmd, 0, '/dev/disk4', '')

    sleeps = []
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    monkeypatch.setattr(validator.time, 'sleep', sleeps.append)

    output = validator._run_with_retries(
        ['hdiutil', 'attach', 'release-artifacts/example.dmg'],
        attempts=4,
        retry_messages=('Resource temporarily unavailable', 'Resource busy'),
        delay_seconds=0.01,
    )

    assert output == '/dev/disk4'
    assert len(calls) == 2
    assert sleeps == [0.01]


def test_validator_does_not_retry_non_transient_hdiutil_errors(monkeypatch) -> None:
    import subprocess

    validator = _load_release_artifact_validator()
    calls = []

    def fake_run(cmd, *, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, 'mount failed', 'permission denied')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._run_with_retries(
            ['hdiutil', 'attach', 'release-artifacts/example.dmg'],
            attempts=4,
            retry_messages=('Resource temporarily unavailable', 'Resource busy'),
            delay_seconds=0.01,
        )
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError('expected non-transient hdiutil failure to exit')

    assert len(calls) == 1
    assert 'Command failed (hdiutil attach release-artifacts/example.dmg)' in message
    assert 'permission denied' in message


def test_validator_fails_after_transient_hdiutil_retry_budget(monkeypatch) -> None:
    import subprocess

    validator = _load_release_artifact_validator()
    calls = []
    sleeps = []

    def fake_run(cmd, *, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, '', 'hdiutil: attach failed - Resource busy')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    monkeypatch.setattr(validator.time, 'sleep', sleeps.append)

    try:
        validator._run_with_retries(
            ['hdiutil', 'attach', 'release-artifacts/example.dmg'],
            attempts=3,
            retry_messages=('Resource temporarily unavailable', 'Resource busy'),
            delay_seconds=0.01,
        )
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError('expected exhausted transient hdiutil failures to exit')

    assert len(calls) == 3
    assert sleeps == [0.01, 0.02]
    assert 'Resource busy' in message


def test_validator_rejects_zero_retry_attempts(monkeypatch) -> None:
    validator = _load_release_artifact_validator()

    def fake_run(*args, **kwargs):  # pragma: no cover - regression guard
        raise AssertionError('subprocess.run should not be called when attempts=0')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._run_with_retries(
            ['hdiutil', 'attach', 'release-artifacts/example.dmg'],
            attempts=0,
            retry_messages=('Resource temporarily unavailable', 'Resource busy'),
        )
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError('expected zero-attempt retry helper call to exit')

    assert 'no attempts were run' in message


def test_validator_mounts_macos_dmg_with_retry_helper_and_detaches(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    mount_dir = tmp_path / 'mounted-dmg'
    mount_dir.mkdir()
    (mount_dir / 'token.place desktop.app').mkdir()
    readme = mount_dir / 'README BEFORE OPENING.txt'
    readme.write_text(
        '\n'.join(
            [
                'This preview build is ad-hoc signed and not notarized.',
                'Apple could not verify token.place desktop is free of malware.',
                'Open System Settings -> Privacy & Security.',
                'Developer ID signing + notarization is required for public trust.',
            ]
        ),
        encoding='utf-8',
    )
    dmg_path = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    dmg_path.write_bytes(b'dmg')
    cleanup_state_calls = []
    cleaned = []

    class FakeMountTemporaryDirectory:
        name = str(mount_dir)

        def cleanup(self):
            cleaned.append(self.name)

    def fake_attach(dmg):
        assert dmg == dmg_path
        return FakeMountTemporaryDirectory()

    def fake_cleanup_state(path, cleanup_mount_dir):
        cleanup_state_calls.append((path, cleanup_mount_dir))
        return 'hdiutil info snapshot'

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_attach_dmg_with_retries', fake_attach)
    monkeypatch.setattr(validator, '_cleanup_dmg_attach_state', fake_cleanup_state)

    validator._validate_dmg_contents(dmg_path, expect_signing=False)

    assert cleanup_state_calls == [(dmg_path, mount_dir)]
    assert cleaned == [str(mount_dir)]


def test_validator_run_formats_command_failures(monkeypatch) -> None:
    import subprocess

    validator = _load_release_artifact_validator()

    def fake_run(cmd, *, check, capture_output, text):
        return subprocess.CompletedProcess(cmd, 2, 'stdout detail', 'stderr detail')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._run(['codesign', '--verify', 'example.app'])
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError('expected _run to fail for non-zero subprocess results')

    assert 'Command failed (codesign --verify example.app)' in message
    assert 'stdout detail' in message
    assert 'stderr detail' in message


def test_validator_attach_retries_with_fresh_mountpoints_and_cleanup(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    dmg_path.write_bytes(b'dmg')
    mount_roots = [tmp_path / 'mount-1', tmp_path / 'mount-2']
    cleanup_calls = []
    run_calls = []
    sleeps = []

    class FakeTemporaryDirectory:
        counter = 0

        def __init__(self, *, prefix):
            assert prefix == 'token-place-dmg-mount-'
            self.name = str(mount_roots[FakeTemporaryDirectory.counter])
            FakeTemporaryDirectory.counter += 1
            Path(self.name).mkdir()

        def cleanup(self):
            cleanup_calls.append(self.name)

    def fake_cleanup(path, mount_dir):
        cleanup_calls.append(f'cleanup-state:{mount_dir}')
        return f'image-path: {path}\n/dev/disk9s1 Apple_HFS'

    def fake_run(cmd, *, check, capture_output, text):
        run_calls.append(cmd)
        if len(run_calls) == 1:
            return subprocess.CompletedProcess(cmd, 1, '', 'hdiutil: attach failed - Resource temporarily unavailable')
        return subprocess.CompletedProcess(cmd, 0, '/dev/disk4', '')

    monkeypatch.setattr(validator.tempfile, 'TemporaryDirectory', FakeTemporaryDirectory)
    monkeypatch.setattr(validator, '_cleanup_dmg_attach_state', fake_cleanup)
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    monkeypatch.setattr(validator.time, 'sleep', sleeps.append)

    temp_dir = validator._attach_dmg_with_retries(dmg_path, attempts=3, delay_seconds=0.01)

    assert temp_dir.name == str(mount_roots[1])
    assert [call[5] for call in run_calls] == [str(mount_roots[0]), str(mount_roots[1])]
    assert f'cleanup-state:{mount_roots[0]}' in cleanup_calls
    assert str(mount_roots[0]) in cleanup_calls
    assert sleeps == [0.01]
    temp_dir.cleanup()



def test_validator_cleanup_only_detaches_referenced_mountpoint_and_matched_device(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = tmp_path / 'target.dmg'
    dmg_path.write_bytes(b'dmg')
    other_dmg_path = tmp_path / 'other.dmg'
    other_dmg_path.write_bytes(b'dmg')
    mount_dir = tmp_path / 'mount'
    mount_dir.mkdir()
    calls = []
    plist_calls = []
    snapshots = []

    first_plist = {
        'images': [
            {
                'image-path': str(dmg_path),
                'system-entities': [
                    {'dev-entry': '/dev/disk4', 'mount-point': str(mount_dir)},
                    {'dev-entry': '/dev/disk4s1', 'mount-point': str(mount_dir)},
                ],
            },
            {
                'image-path': str(other_dmg_path),
                'system-entities': [{'dev-entry': '/dev/disk9', 'mount-point': '/Volumes/Other'}],
            },
        ]
    }
    second_plist = {
        'images': [
            {
                'image-path': str(dmg_path),
                'system-entities': [{'dev-entry': '/dev/disk4s1'}],
            },
            {
                'image-path': str(other_dmg_path),
                'system-entities': [{'dev-entry': '/dev/disk9', 'mount-point': '/Volumes/Other'}],
            },
        ]
    }

    def fake_info_plist():
        plist_calls.append('plist')
        return first_plist if len(plist_calls) == 1 else second_plist

    def fake_run_best_effort(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(validator, '_hdiutil_info_plist', fake_info_plist)
    monkeypatch.setattr(validator, '_hdiutil_info_snapshot', lambda: snapshots.append('snapshot') or 'snapshot')
    monkeypatch.setattr(validator, '_run_best_effort', fake_run_best_effort)

    assert validator._cleanup_dmg_attach_state(dmg_path, mount_dir) == 'snapshot'

    assert calls == [['hdiutil', 'detach', str(mount_dir)], ['hdiutil', 'detach', '/dev/disk4s1']]
    assert len(plist_calls) == 2
    assert snapshots == ['snapshot']
    assert mount_dir.exists()


def test_validator_cleanup_skips_unreferenced_mountpoint(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = tmp_path / 'target.dmg'
    dmg_path.write_bytes(b'dmg')
    mount_dir = tmp_path / 'mount'
    mount_dir.mkdir()
    calls = []

    monkeypatch.setattr(
        validator,
        '_hdiutil_info_plist',
        lambda: {
            'images': [
                {
                    'image-path': str(dmg_path),
                    'system-entities': [{'dev-entry': '/dev/disk5', 'mount-point': '/Volumes/Target'}],
                }
            ]
        },
    )
    monkeypatch.setattr(validator, '_hdiutil_info_snapshot', lambda: 'snapshot')
    monkeypatch.setattr(
        validator,
        '_run_best_effort',
        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''),
    )

    validator._cleanup_dmg_attach_state(dmg_path, mount_dir)

    assert calls == [['hdiutil', 'detach', '/dev/disk5']]

def test_validator_attach_stops_on_non_transient_failure(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    dmg_path.write_bytes(b'dmg')
    mount_dir = tmp_path / 'mount-1'
    run_calls = []

    class FakeTemporaryDirectory:
        def __init__(self, *, prefix):
            self.name = str(mount_dir)
            mount_dir.mkdir()

        def cleanup(self):
            pass

    def fake_cleanup(path, _mount_dir):
        return 'hdiutil info snapshot'

    def fake_run(cmd, *, check, capture_output, text):
        run_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, 'attach stdout', 'permission denied')

    monkeypatch.setattr(validator.tempfile, 'TemporaryDirectory', FakeTemporaryDirectory)
    monkeypatch.setattr(validator, '_cleanup_dmg_attach_state', fake_cleanup)
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._attach_dmg_with_retries(dmg_path, attempts=3, delay_seconds=0.01)
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError('expected non-transient attach failure to exit')

    assert len(run_calls) == 1
    assert 'attach attempts: 1/3' in message
    assert 'permission denied' in message
    assert 'redacted hdiutil info snapshot' in message
