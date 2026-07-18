from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
    monkeypatch.setattr(validator.shutil, 'which', lambda name: '/usr/bin/codesign' if name == 'codesign' else None)
    monkeypatch.setattr(validator, '_run', lambda cmd: '')
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
    monkeypatch.setattr(validator, '_hdiutil_info_raw', lambda: ('hdiutil info raw', 0))
    monkeypatch.setattr(
        validator,
        '_hdiutil_info_snapshot',
        lambda raw_info=None, returncode=0: snapshots.append((raw_info, returncode)) or 'snapshot',
    )
    monkeypatch.setattr(validator, '_run_best_effort', fake_run_best_effort)

    assert validator._cleanup_dmg_attach_state(dmg_path, mount_dir) == 'snapshot'

    assert calls == [['hdiutil', 'detach', str(mount_dir)], ['hdiutil', 'detach', '/dev/disk4s1']]
    assert len(plist_calls) == 2
    assert snapshots == [('hdiutil info raw', 0)]
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
    monkeypatch.setattr(validator, '_hdiutil_info_raw', lambda: ('hdiutil info raw', 0))
    monkeypatch.setattr(validator, '_hdiutil_info_snapshot', lambda raw_info=None, returncode=0: 'snapshot')
    monkeypatch.setattr(
        validator,
        '_run_best_effort',
        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''),
    )

    validator._cleanup_dmg_attach_state(dmg_path, mount_dir)

    assert calls == [['hdiutil', 'detach', '/dev/disk5']]


def test_validator_cleanup_uses_raw_hdiutil_info_for_matching_but_redacts_diagnostics(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = Path('release-artifacts/token.place-desktop-0.1.2-apple-silicon.dmg')
    mount_dir = tmp_path / 'mount'
    mount_dir.mkdir()
    calls = []
    raw_info = """
image-path: /private/var/folders/zz/redacted-test/release-artifacts/token.place-desktop-0.1.2-apple-silicon.dmg
/dev/disk8           Apple_partition_scheme
/dev/disk8s1         Apple_HFS
image-path: /private/var/folders/zz/redacted-test/release-artifacts/other.dmg
/dev/disk9           Apple_partition_scheme
""".strip()

    monkeypatch.setattr(validator, '_hdiutil_info_raw', lambda: (raw_info, 0))
    monkeypatch.setattr(validator, '_hdiutil_info_plist', lambda: {})
    monkeypatch.setattr(
        validator,
        '_run_best_effort',
        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''),
    )

    diagnostic = validator._cleanup_dmg_attach_state(dmg_path, mount_dir)

    assert calls == [['hdiutil', 'detach', '/dev/disk8'], ['hdiutil', 'detach', '/dev/disk8s1']]
    assert '/dev/disk9' not in {call[-1] for call in calls}
    assert '/private/var/folders/<redacted>' in diagnostic
    assert '/private/var/folders/zz/redacted-test' not in diagnostic

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


def test_validator_hdiutil_info_helpers_preserve_raw_matching_and_redact(monkeypatch, tmp_path, capsys) -> None:
    validator = _load_release_artifact_validator()
    raw_path = '/private/var/folders/zz/example/release-artifacts/token.place-desktop-0.1.2-apple-silicon.dmg'

    def fake_run(cmd, *, check, capture_output, text=None):
        if cmd == ['hdiutil', 'info']:
            return subprocess.CompletedProcess(cmd, 3, f'image-path: {raw_path}', 'raw stderr')
        if cmd == ['hdiutil', 'info', '-plist']:
            return subprocess.CompletedProcess(cmd, 0, b'not a plist', b'')
        return subprocess.CompletedProcess(cmd, 7, 'best stdout', 'best stderr')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    assert validator._hdiutil_info_raw() == (f'image-path: {raw_path}\nraw stderr', 3)
    snapshot = validator._hdiutil_info_snapshot()
    assert 'hdiutil info failed with exit code 3' in snapshot
    assert '/private/var/folders/<redacted>' in snapshot
    assert raw_path not in snapshot
    assert validator._hdiutil_info_plist() == {}

    result = validator._run_best_effort(['hdiutil', 'detach', str(tmp_path / 'missing')])

    assert result.returncode == 7
    warning = capsys.readouterr().out
    assert '::warning::Best-effort command failed' in warning
    assert 'best stdout' in warning
    assert 'best stderr' in warning


def test_validator_hdiutil_matching_helpers_handle_malformed_entries(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = tmp_path / 'target.dmg'
    dmg_path.write_bytes(b'dmg')
    other_path = tmp_path / 'other.dmg'
    other_path.write_bytes(b'dmg')
    mount_dir = tmp_path / 'mount'

    original_resolve = validator.Path.resolve

    def fake_resolve(self):
        if str(self).startswith('/bad/path'):
            raise OSError('unresolvable')
        return original_resolve(self)

    monkeypatch.setattr(validator.Path, 'resolve', fake_resolve)

    assert validator._image_entries({'images': 'not-a-list'}) == []
    assert not validator._image_matches_dmg({'image-path': 123}, dmg_path)
    relative_dmg_path = Path('release-artifacts/target.dmg')
    assert validator._path_matches_dmg('/bad/path/release-artifacts/target.dmg', relative_dmg_path)
    assert not validator._path_matches_dmg('/bad/path/release-artifacts/other.dmg', relative_dmg_path)
    assert not validator._mountpoint_referenced(
        mount_dir,
        {'images': [{'image-path': str(dmg_path), 'system-entities': 'not-a-list'}]},
    )

    devices = validator._matching_hdiutil_devices(
        dmg_path,
        {
            'images': [
                {'image-path': str(other_path), 'system-entities': [{'dev-entry': '/dev/disk2'}]},
                {'image-path': str(dmg_path), 'system-entities': 'not-a-list'},
                {
                    'image-path': str(dmg_path),
                    'system-entities': [
                        'not-a-dict',
                        {'dev-entry': 'not-a-disk'},
                        {'dev-entry': '/dev/disk3'},
                    ],
                },
            ]
        },
        raw_info=f'image-path: {dmg_path}\n/dev/disk3\n/dev/disk4\nimage-path: {other_path}\n/dev/disk9',
    )

    assert devices == ['/dev/disk3', '/dev/disk4']


def test_validator_hdiutil_plist_and_retry_edge_branches(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    dmg_path = tmp_path / 'target.dmg'
    dmg_path.write_bytes(b'dmg')
    plist_payload = validator.plistlib.dumps({'images': []})
    calls = []

    def fake_run(cmd, *, check, capture_output, text=None):
        calls.append(cmd)
        if cmd == ['hdiutil', 'info', '-plist'] and len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 2, b'', b'plist failed')
        if cmd == ['hdiutil', 'info', '-plist']:
            return subprocess.CompletedProcess(cmd, 0, plist_payload, b'')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    assert validator._hdiutil_info_plist() == {}
    assert validator._hdiutil_info_plist() == {'images': []}
    assert validator._path_matches_dmg(str(dmg_path.resolve()), dmg_path)

    try:
        validator._attach_dmg_with_retries(dmg_path, attempts=0)
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError('expected zero-attempt attach helper call to exit')

    assert 'no attempts were run' in message

def test_tauri_config_bundles_embedded_python_runtime_exactly() -> None:
    config = json.loads(TAURI_CONFIG.read_text(encoding='utf-8'))
    resources = config['bundle']['resources']
    assert resources['python-runtime'] == 'python-runtime'


def test_workflow_prepares_and_validates_embedded_macos_runtime() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'Prepare embedded macOS Python runtime' in text
    assert 'python scripts/prepare_embedded_python_runtime.py' in text
    assert 'src-tauri/python-runtime/bin/python3 -m pip check' in text
    assert '--require-embedded-python-runtime' in text


def test_validator_contains_embedded_runtime_guardrails() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert 'Contents" / "Resources" / "python-runtime' in text
    assert '"PYTHONPATH": os.pathsep.join(' in text
    assert 'str(app_for_subprocess / "Contents" / "Resources" / "python")' in text
    assert 'str(app_for_subprocess / "Contents" / "Resources")' in text
    assert 'xcode-select' in text
    assert 'otool' in text
    assert 'embedded runtime probe did not report Metal GPU offload' in text


def test_validator_uses_packaged_python_resources_for_runtime_probe() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert '"PYTHONPATH": os.pathsep.join(' in text
    assert 'str(app_for_subprocess / "Contents" / "Resources" / "python")' in text
    assert 'str(app_for_subprocess / "Contents" / "Resources")' in text
    assert "Path.cwd() / 'src-tauri' / 'python'" not in text
    assert "qwen_64k_yarn_support" in text
    assert "model_bridge.py" in text
    assert "'inspect'" in text


def test_validator_sanitized_python_env_replaces_parent_environment(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = tmp_path / 'python3'
    py.write_text('#!/bin/sh\n', encoding='utf-8')
    captured = {}

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        captured['env'] = env
        return subprocess.CompletedProcess(cmd, 0, 'ok', '')

    forbidden_parent_env = {
        'PYTHONHOME': '/bad/pythonhome',
        'PYTHONSTARTUP': '/bad/startup.py',
        'PYTHONUSERBASE': '/bad/userbase',
        'TOKEN_PLACE_PYTHON': '/usr/bin/python3',
        'TOKEN_PLACE_SIDECAR_PYTHON': '/usr/bin/python3',
    }
    for key, value in forbidden_parent_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    assert validator._run_python_sanitized(py, 'print(1)', app) == 'ok'
    assert captured['env']['PIP_NO_INDEX'] == '1'
    assert captured['env']['PYTHONNOUSERSITE'] == '1'
    assert captured['env']['PATH'] == '/usr/bin:/bin'
    assert captured['env']['PYTHONPATH'] == subprocess.os.pathsep.join([
        str((app / 'Contents' / 'Resources' / 'python').absolute()),
        str((app / 'Contents' / 'Resources').absolute()),
    ])
    for key in forbidden_parent_env:
        assert key not in captured['env']


def test_background_probe_bootstraps_nested_tauri_resources_before_utils_import(tmp_path) -> None:
    resources = tmp_path / 'token.place desktop.app' / 'Contents' / 'Resources'
    python_resources = resources / 'python'
    runtime_import_root = resources / '_up_' / '_up_'
    nested_utils = runtime_import_root / 'utils' / 'llm'
    python_resources.mkdir(parents=True)
    nested_utils.mkdir(parents=True)
    (runtime_import_root / 'requirements.txt').write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    (python_resources / 'desktop_runtime_setup.py').write_text(
        """
import json
import shutil
import os
from pathlib import Path

RUNTIME_PROBE_ENV = 'TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON'

def ensure_desktop_llama_runtime(mode, *, repo_root=None, context_tier=None):
    root = Path(repo_root)
    if mode != 'auto' or context_tier != '64k-full':
        raise RuntimeError('unexpected runtime arguments')
    if not (root / 'requirements.txt').is_file():
        raise RuntimeError('missing packaged requirements metadata')
    os.environ[RUNTIME_PROBE_ENV] = json.dumps({'private': True})
    return {'repo_root_name': root.name, 'requirements_found': True}
""",
        encoding='utf-8',
    )
    (python_resources / 'path_bootstrap.py').write_text(
        Path('desktop-tauri/src-tauri/python/path_bootstrap.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (nested_utils.parent / '__init__.py').write_text('', encoding='utf-8')
    (nested_utils / '__init__.py').write_text('', encoding='utf-8')
    (nested_utils / 'model_manager.py').write_text('BOOTSTRAPPED = True\n', encoding='utf-8')

    code = """
import json
import shutil
from pathlib import Path
import desktop_runtime_setup
from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(
    desktop_runtime_setup.__file__,
    avoid_llama_cpp_shadowing=True,
)

from desktop_runtime_setup import ensure_desktop_llama_runtime
from utils.llm import model_manager

runtime_import_root = Path(model_manager.__file__).resolve().parents[2]
if not (runtime_import_root / 'requirements.txt').is_file():
    raise SystemExit('requirements metadata not found')
setup = ensure_desktop_llama_runtime(
    'auto',
    repo_root=runtime_import_root,
    context_tier='64k-full',
)
print(json.dumps({
    'bootstrapped': model_manager.BOOTSTRAPPED,
    'repo_root_name': setup['repo_root_name'],
    'requirements_found': setup['requirements_found'],
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, '-c', code],
        check=False,
        capture_output=True,
        text=True,
        env={
            'PYTHONPATH': str(python_resources),
            'PYTHONNOUSERSITE': '1',
        },
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        'bootstrapped': True,
        'repo_root_name': '_up_',
        'requirements_found': True,
    }


def test_background_probe_uses_production_runtime_import_and_private_identity(tmp_path) -> None:
    resources = tmp_path / 'token.place desktop.app' / 'Contents' / 'Resources'
    python_resources = resources / 'python'
    runtime_import_root = resources / '_up_' / '_up_'
    nested_utils = runtime_import_root / 'utils' / 'llm'
    python_resources.mkdir(parents=True)
    nested_utils.mkdir(parents=True)
    sentinel_path = str((tmp_path / 'private' / 'llama_cpp' / '__init__.py').resolve())
    sentinel_digest = 'sha256:' + ('a' * 64)
    (runtime_import_root / 'requirements.txt').write_text('llama-cpp-python==0.3.32\n', encoding='utf-8')
    (python_resources / 'desktop_runtime_setup.py').write_text(
        f"""
import json
import shutil
import os
from pathlib import Path

RUNTIME_PROBE_ENV = 'TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON'

def ensure_desktop_llama_runtime(mode, *, repo_root=None, context_tier=None):
    root = Path(repo_root)
    if mode != 'auto' or context_tier != '64k-full':
        raise RuntimeError('unexpected runtime arguments')
    if not (root / 'requirements.txt').is_file():
        raise RuntimeError('missing packaged requirements metadata')
    os.environ[RUNTIME_PROBE_ENV] = json.dumps({{
        'llama_module_identity': {sentinel_digest!r},
        'llama_module_path': {sentinel_path!r},
    }})
    return {{
        'runtime_action': 'metal_already_supported',
        'selected_backend': 'metal',
        'llama_cpp_python_version_match': 'match',
        'llama_module_path_present': True,
        'capability_source': 'desktop_runtime_setup_probe',
    }}
""",
        encoding='utf-8',
    )
    (python_resources / 'path_bootstrap.py').write_text(
        Path('desktop-tauri/src-tauri/python/path_bootstrap.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (nested_utils.parent / '__init__.py').write_text('', encoding='utf-8')
    (nested_utils / '__init__.py').write_text('', encoding='utf-8')
    (nested_utils / 'model_manager.py').write_text(
        f"""
from pathlib import Path
import threading

RUNTIME_CALLS = []
SUBPROCESS_CALLS = []

class _SubprocessLlamaCppModule:
    __file__ = {sentinel_path!r}
    backend = 'metal'
    class Llama:
        pass

def _import_llama_cpp_subprocess_module(*args, **kwargs):
    SUBPROCESS_CALLS.append((args, kwargs))
    raise AssertionError('direct subprocess facade constructor must not be used')

def _import_llama_cpp_runtime(*, require_real_runtime, timeout_seconds, desktop_runtime_probe):
    if threading.current_thread() is threading.main_thread():
        raise AssertionError('runtime import did not run in background thread')
    RUNTIME_CALLS.append(dict(desktop_runtime_probe))
    if require_real_runtime is not True or timeout_seconds != 10:
        raise AssertionError('unexpected import arguments')
    if desktop_runtime_probe.get('llama_module_identity') != {sentinel_digest!r}:
        raise AssertionError('private identity was not merged')
    if 'llama_module_path' in desktop_runtime_probe:
        raise AssertionError('raw private module path leaked into runtime setup')
    return _SubprocessLlamaCppModule()

def _safe_constructor_capability_payload(facade):
    return {{
        'backend': 'metal',
        'gpu_offload_supported': True,
        'constructor_kwarg_support': {{name: True for name in (
            'type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch',
            'rope_scaling_type', 'yarn_ext_factor', 'yarn_attn_factor', 'yarn_beta_fast',
            'yarn_beta_slow', 'yarn_orig_ctx', 'rope_freq_base', 'rope_freq_scale')}},
    }}

def _runtime_supports_qwen_yarn_rope(facade, llama_cls):
    return {{
        'llama_cpp_python_version': '0.3.32',
        'yarn_resolver_source': 'top_level_enum',
        'constructor_signature_inspectable': True,
        'constructor_kwarg_support': {{name: True for name in (
            'rope_scaling_type', 'yarn_ext_factor', 'yarn_attn_factor', 'yarn_beta_fast',
            'yarn_beta_slow', 'yarn_orig_ctx', 'rope_freq_base', 'rope_freq_scale')}},
        'llama_module_identity_match': True,
        'supported': True,
        'desktop_probe_authoritative': True,
        'child_probe_reprobe_skipped_reason': 'desktop_probe_authoritative',
        'capability_source': 'desktop_runtime_setup_probe',
        'incomplete_probe_fields': [],
    }}
""",
        encoding='utf-8',
    )

    code = """
import json, os, threading
import desktop_runtime_setup
from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(
    desktop_runtime_setup.__file__,
    avoid_llama_cpp_shadowing=True,
)

from desktop_runtime_setup import RUNTIME_PROBE_ENV, ensure_desktop_llama_runtime
from pathlib import Path
from utils.llm import model_manager

runtime_import_root = Path(model_manager.__file__).resolve().parents[2]
if not (runtime_import_root / 'requirements.txt').is_file():
    raise SystemExit('packaged runtime metadata missing: requirements.txt')

result = {}

def worker():
    setup = ensure_desktop_llama_runtime('auto', repo_root=runtime_import_root, context_tier='64k-full')
    private_runtime_setup = dict(setup)
    private_probe = json.loads(os.environ.get(RUNTIME_PROBE_ENV) or '{}')
    private_identity = private_probe.get('llama_module_identity')
    if isinstance(private_identity, str):
        private_runtime_setup['llama_module_identity'] = private_identity
    facade = model_manager._import_llama_cpp_runtime(
        require_real_runtime=True,
        timeout_seconds=10,
        desktop_runtime_probe=private_runtime_setup,
    )
    gate = model_manager._runtime_supports_qwen_yarn_rope(facade, facade.Llama)
    facade_capabilities = model_manager._safe_constructor_capability_payload(facade)
    result.update({
        'facade_type': type(facade).__name__,
        'facade_file_discovered': bool(getattr(facade, '__file__', None)),
        'backend': facade_capabilities.get('backend'),
        'gpu_offload_supported': facade_capabilities.get('gpu_offload_supported') is True,
        'required_kwargs_supported': all(
            (facade_capabilities.get('constructor_kwarg_support') or {{}}).get(name)
            for name in (
                'type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch',
                'rope_scaling_type', 'yarn_ext_factor', 'yarn_attn_factor', 'yarn_beta_fast',
                'yarn_beta_slow', 'yarn_orig_ctx', 'rope_freq_base', 'rope_freq_scale'
            )
        ),
        'llama_module_identity_match': gate.get('llama_module_identity_match') is True,
        'supported': gate.get('supported') is True,
        'desktop_probe_authoritative': gate.get('desktop_probe_authoritative') is True,
        'runtime_call_count': len(model_manager.RUNTIME_CALLS),
        'subprocess_call_count': len(model_manager.SUBPROCESS_CALLS),
        'runtime_probe_keys': sorted(model_manager.RUNTIME_CALLS[0]),
    })

thread = threading.Thread(target=worker, name='token-place-release-qwen64k-probe')
thread.start()
thread.join(timeout=30)
if thread.is_alive():
    raise SystemExit('background facade probe timed out')
print(json.dumps(result, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, '-c', code],
        check=False,
        capture_output=True,
        text=True,
        env={'PYTHONPATH': str(python_resources), 'PYTHONNOUSERSITE': '1'},
        cwd=str(tmp_path),
    )

    captured = result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert sentinel_path not in captured
    assert sentinel_digest not in captured
    payload = json.loads(result.stdout.strip())
    assert payload == {
        'backend': 'metal',
        'desktop_probe_authoritative': True,
        'facade_file_discovered': True,
        'facade_type': '_SubprocessLlamaCppModule',
        'gpu_offload_supported': True,
        'llama_module_identity_match': True,
        'required_kwargs_supported': True,
        'runtime_call_count': 1,
        'runtime_probe_keys': [
            'capability_source',
            'llama_cpp_python_version_match',
            'llama_module_identity',
            'llama_module_path_present',
            'runtime_action',
            'selected_backend',
        ],
        'subprocess_call_count': 0,
        'supported': True,
    }

def test_release_workflow_does_not_rebuild_llama_cpp_on_release_matrix() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    install_step = text.split('- name: Install desktop llama-cpp runtime with platform GPU backend', 1)[1]
    install_step = install_step.split('- name: Cache embedded macOS Python archive and wheels', 1)[0]
    assert "if: runner.os == 'Linux'" in install_step
    assert "macOS prepares and validates its bundled interpreter" in install_step
    assert 'llama_cpp_install_plan_fallbacks(platform=sys.platform' in install_step


def test_release_workflow_uses_explicit_arm64_macos_runner_for_embedded_runtime() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "- os: macos-26" in workflow
    assert "- os: macos-latest" not in workflow
    assert 'test "$(uname -m)" = "arm64"' in workflow


def test_release_workflow_uses_explicit_windows_x86_64_target_and_bundle_paths() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "tauri_target: x86_64-pc-windows-msvc" in workflow
    assert "npm run tauri build -- --target ${{ matrix.tauri_target }}" in workflow
    assert "src-tauri/target/${{ matrix.tauri_target }}/release/bundle/nsis" in workflow
    assert "src-tauri/target/${{ matrix.tauri_target }}/release/bundle/msi" in workflow
    assert "aarch64-pc-windows-msvc" not in workflow


def test_release_workflow_installs_pytest_before_macos_probe() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    install_index = workflow.index('- name: Install macOS validation test dependencies')
    validate_index = workflow.index('- name: Validate macOS staged artifact guardrails')
    assert install_index < validate_index
    install_step = workflow[install_index:validate_index]
    assert "if: runner.os == 'macOS'" in install_step
    assert 'python -m pip install --upgrade pip' in install_step
    assert "python -m pip install 'pytest>=8.1'" in install_step
    validate_step = workflow[validate_index:]
    assert (
        "python -m pytest --confcutdir=tests/integration "
        "tests/integration/test_macos_release_probe.py -q"
    ) in validate_step


def test_run_python_sanitized_rejects_forbidden_markers_and_cleans_home(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = tmp_path / 'python3'
    created_home = {}

    def fake_mkdtemp(*, prefix):
        home = tmp_path / f'{prefix}abc'
        home.mkdir()
        created_home['path'] = home
        return str(home)

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        assert env['HOME'] == str(created_home['path'] / 'home')
        assert Path(env['HOME']).is_dir()
        assert env['TOKEN_PLACE_MODELS_DIR'] == str(created_home['path'] / 'token.place' / 'models')
        assert env['XDG_CACHE_HOME'] == str(created_home['path'] / 'token.place' / 'cache')
        assert env['XDG_CONFIG_HOME'] == str(created_home['path'] / 'token.place' / 'config')
        assert env['XDG_DATA_HOME'] == str(created_home['path'] / 'token.place' / 'data')
        assert not Path(cwd).resolve().is_relative_to(app.resolve())
        return subprocess.CompletedProcess(cmd, 0, '/usr/bin/python3 leaked', '')

    monkeypatch.setattr(validator.tempfile, 'mkdtemp', fake_mkdtemp)
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._run_python_sanitized(py, 'print(1)', app)
        assert False
    except SystemExit as exc:
        assert 'forbidden marker' in str(exc)
    assert not created_home['path'].exists()


def test_run_python_sanitized_allows_paths_inside_runner_app_bundle(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Users' / 'runner' / 'work' / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = app / 'Contents' / 'Resources' / 'python-runtime' / 'bin' / 'python3'

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        payload = {
            'executable': str(py),
            'prefix': str(app / 'Contents' / 'Resources' / 'python-runtime'),
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), '')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    output = validator._run_python_sanitized(py, 'print(1)', app)

    assert '/Users/runner' in output


def test_run_python_sanitized_runs_probes_from_packaged_python_resources(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Users' / 'runner' / 'work' / 'token.place desktop.app'
    resources_python = app / 'Contents' / 'Resources' / 'python'
    resources_python.mkdir(parents=True)
    py = app / 'Contents' / 'Resources' / 'python-runtime' / 'bin' / 'python3'
    seen = {}

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        seen['cwd'] = cwd
        return subprocess.CompletedProcess(cmd, 0, 'ok', '')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    assert validator._run_python_sanitized(py, 'print(1)', app) == 'ok'
    assert not Path(seen['cwd']).resolve().is_relative_to(app.resolve())


def test_run_python_sanitized_absolutizes_relative_interpreter_before_resource_cwd(
    monkeypatch, tmp_path
) -> None:
    validator = _load_release_artifact_validator()
    app = Path('src-tauri/target/aarch64-apple-darwin/release/bundle/macos/token.place desktop.app')
    resources_python = tmp_path / app / 'Contents' / 'Resources' / 'python'
    resources_python.mkdir(parents=True)
    py = app / 'Contents' / 'Resources' / 'python-runtime' / 'bin' / 'python3'
    seen = {}

    monkeypatch.chdir(tmp_path)

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        seen['cmd'] = cmd
        seen['cwd'] = cwd
        return subprocess.CompletedProcess(cmd, 0, 'ok', '')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    assert validator._run_python_sanitized(py, 'print(1)', app) == 'ok'
    assert seen['cmd'][0] == str((tmp_path / py).absolute())
    assert not Path(seen['cwd']).resolve().is_relative_to((tmp_path / app).resolve())

def test_redact_allowed_app_locations_still_flags_runner_paths_outside_app(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Users' / 'runner' / 'work' / 'token.place desktop.app'
    output = f"{app}/Contents/Resources/python-runtime/bin/python3\n/Users/runner/.cache/pip"

    redacted = validator._redact_allowed_app_locations(output, app)

    assert '<app-bundle>/Contents/Resources/python-runtime/bin/python3' in redacted
    assert '/Users/runner/.cache/pip' in redacted


def test_redact_allowed_app_locations_accepts_same_app_under_ci_absolute_prefix(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'relative' / 'token.place desktop.app'
    output = (
        '/Users/runner/work/token.place/token.place/desktop-tauri/src-tauri/target/'
        'aarch64-apple-darwin/release/bundle/macos/token.place desktop.app/'
        'Contents/Resources/python-runtime/bin/python3\n'
        '/Users/runner/work/token.place/token.place/desktop-tauri/'
        'src-tauri/target/aarch64-apple-darwin/release/bundle/macos/'
        'token.place desktop.app/Contents/Resources/python-runtime\n'
        '/Users/runner/.cache/pip'
    )

    redacted = validator._redact_allowed_app_locations(output, app)

    assert '<app-bundle>/Contents/Resources/python-runtime/bin/python3' in redacted
    assert '<app-bundle>/Contents/Resources/python-runtime' in redacted
    assert '/Users/runner/work/token.place/token.place/desktop-tauri/<app-bundle>' not in redacted
    assert '/Users/runner/.cache/pip' in redacted



def test_validate_embedded_python_runtime_absolutizes_packaged_model_bridge(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = Path('src-tauri/target/aarch64-apple-darwin/release/bundle/macos/token.place desktop.app')
    app_root = tmp_path / app
    runtime = app_root / 'Contents' / 'Resources' / 'python-runtime'
    resources_python = app_root / 'Contents' / 'Resources' / 'python'
    (runtime / 'bin').mkdir(parents=True)
    resources_python.mkdir(parents=True)
    py = runtime / 'bin' / 'python3'
    py.write_text('#!/bin/sh\n', encoding='utf-8')
    py.chmod(0o755)
    (runtime / 'embedded_python_runtime_provenance.json').write_text('{}', encoding='utf-8')
    (runtime / 'LICENSE-PYTHON.txt').write_text('PSF', encoding='utf-8')
    (runtime / 'LICENSE-python-build-standalone.txt').write_text('PBS', encoding='utf-8')
    model_bridge = resources_python / 'model_bridge.py'
    model_bridge.write_text('', encoding='utf-8')
    calls = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(validator, '_validate_macho_linkage', lambda candidate, app_path: None)

    def fake_run_python_sanitized(_py_arg, code, _app_path_arg):
        calls.append(code)
        if 'sys.version_info' in code:
            return json.dumps({
                'version': [3, 11],
                'machine': 'arm64',
                'executable': str(py),
                'prefix': str(runtime),
                'llama_cpp_python_version': '0.3.32',
            })
        if '_probe_llama_runtime' in code:
            return json.dumps({
                'backend': 'metal',
                'gpu_offload_supported': True,
                'qwen_64k_yarn_support': 'supported',
                'rope_scaling_type_supported': True,
                'rope_freq_scale_supported': True,
                'yarn_orig_ctx_supported': True,
                'constructor_kwarg_support': {
                    'flash_attn': True,
                    'offload_kqv': True,
                    'n_batch': True,
                    'n_ubatch': True,
                },
            })
        if 'token-place-release-qwen64k-probe' in code:
            return json.dumps({
                'runtime_action_ok': True,
                'facade_type': '_SubprocessLlamaCppModule',
                'backend': 'metal',
                'gpu_offload_supported': True,
                'version': '0.3.32',
                'yarn_resolver_source': 'top_level_enum',
                'constructor_signature_inspectable': True,
                'required_kwargs_supported': True,
                'llama_module_identity_match': True,
                'supported': True,
                'desktop_probe_authoritative': True,
                'secondary_reprobe_skipped': True,
            })
        return 'ok'

    monkeypatch.setattr(validator, '_run_python_sanitized', fake_run_python_sanitized)

    validator._validate_embedded_python_runtime(app)

    bridge_calls = [code for code in calls if 'model_bridge.py' in code]
    assert bridge_calls
    assert str(model_bridge.absolute()) in bridge_calls[-1]
    assert f"{str(app / 'Contents' / 'Resources' / 'python' / 'model_bridge.py')!r}" not in bridge_calls[-1]

def test_run_python_sanitized_formats_probe_failures_without_raw_code(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = tmp_path / 'python3'

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        return subprocess.CompletedProcess(cmd, 7, 'stdout', 'stderr')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._run_python_sanitized(py, 'secret code body', app)
        assert False
    except SystemExit as exc:
        message = str(exc)
    assert '<probe>' in message
    assert 'secret code body' not in message


def test_validate_macho_linkage_rejects_forbidden_external_dependency(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    binary = app / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'bad.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, check=False, capture_output=True, text=True):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')
        raise AssertionError(cmd)

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    def fake_tool(cmd):
        if cmd[0] == 'lipo':
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{binary}:\n\t/opt/homebrew/lib/libbad.dylib (compatibility version 1.0.0, current version 1.0.0)'
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D should not be used')
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'cmd LC_ID_DYLIB\ncmdsize 48\nname @rpath/bad.dylib (offset 24)'
        raise AssertionError(cmd)

    monkeypatch.setattr(validator, '_run', fake_tool)

    try:
        validator._validate_macho_linkage(binary, app)
        assert False
    except SystemExit as exc:
        assert 'category=dependency ref=libbad.dylib' in str(exc)


def test_validate_embedded_python_runtime_fails_incomplete_app_before_publication(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources').mkdir(parents=True)
    try:
        validator._validate_embedded_python_runtime(app)
        assert False
    except SystemExit as exc:
        assert 'embedded Python interpreter missing' in str(exc)



def test_validate_macho_linkage_rejects_homebrew_openssl_dependency(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    binary = app / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'libllama-common.0.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, check=False, capture_output=True, text=True):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')
        raise AssertionError(cmd)

    def fake_tool(cmd):
        if cmd[0] == 'lipo':
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return (
                f'{binary}:\n'
                '\t/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib '
                '(compatibility version 3.0.0, current version 3.0.0)'
            )
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D should not be used')
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'cmd LC_ID_DYLIB\ncmdsize 64\nname @rpath/libllama-common.0.dylib (offset 24)'
        raise AssertionError(cmd)

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    monkeypatch.setattr(validator, '_run', fake_tool)

    try:
        validator._validate_macho_linkage(binary, app)
        assert False
    except SystemExit as exc:
        assert 'category=dependency ref=libssl.3.dylib' in str(exc)


def test_validate_macho_linkage_allows_runtime_relative_rpath_and_system_dependencies(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    binary = app / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'libok.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, check=False, capture_output=True, text=True):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')
        raise AssertionError(cmd)

    def fake_tool(cmd):
        if cmd[0] == 'lipo':
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return (
                f'{binary}:\n'
                '\t@loader_path/libllama.dylib (compatibility version 0.0.0, current version 0.0.0)\n'
                '\t@rpath/libggml.dylib (compatibility version 0.0.0, current version 0.0.0)\n'
                '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n'
                '\t/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit '
                '(compatibility version 45.0.0, current version 2575.40.3)'
            )
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D should not be used')
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'cmd LC_ID_DYLIB\ncmdsize 48\nname @rpath/libok.dylib (offset 24)\ncmd LC_RPATH\ncmdsize 32\npath @loader_path/.. (offset 12)'
        raise AssertionError(cmd)

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    monkeypatch.setattr(validator, '_run', fake_tool)

    validator._validate_macho_linkage(binary, app)


def test_workflow_validates_app_before_creating_dmg() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    app_only_index = text.index('--app-only')
    hdiutil_index = text.index('hdiutil create -volname "token.place desktop"')
    final_validator_index = text.rindex('--dmg-path "${dmg_path}"')
    assert app_only_index < hdiutil_index < final_validator_index

def test_validate_macho_linkage_allows_bundle_without_install_id_and_skips_otool_d(monkeypatch, tmp_path):
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    bundle = app / 'Contents/Resources/python-runtime/lib/python3.11/site-packages/ada92cb5d92a588d1b93__mypyc.cpython-311-darwin.so'
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    def fake_run_file(cmd, check=False, capture_output=True, text=True):
        assert cmd[0] == 'file'
        return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit bundle arm64', '')

    def fake_run(cmd):
        if cmd[:2] == ['lipo', '-archs']:
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{bundle}:\n\t@loader_path/libexample.dylib (compatibility version 1.0.0, current version 1.0.0)'
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'Load command 0\ncmd LC_RPATH\ncmdsize 48\npath @loader_path (offset 12)\n'
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D must not be used')
        raise AssertionError(cmd)

    monkeypatch.setattr(validator.subprocess, 'run', fake_run_file)
    monkeypatch.setattr(validator, '_run', fake_run)
    validator._validate_macho_linkage(bundle, app)


def test_validate_macho_linkage_rejects_absolute_install_id_inside_app(monkeypatch, tmp_path):
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    dylib = app / 'Contents/Resources/python-runtime/lib/libexample.dylib'
    dylib.parent.mkdir(parents=True, exist_ok=True)
    dylib.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    def fake_run_file(cmd, check=False, capture_output=True, text=True):
        assert cmd[0] == 'file'
        return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')

    def fake_run(cmd):
        if cmd[:2] == ['lipo', '-archs']:
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{dylib}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)'
        if cmd[0] == 'otool' and '-l' in cmd:
            return f'Load command 0\ncmd LC_ID_DYLIB\ncmdsize 80\nname {dylib} (offset 24)\n'
        raise AssertionError(cmd)

    monkeypatch.setattr(validator.subprocess, 'run', fake_run_file)
    monkeypatch.setattr(validator, '_run', fake_run)
    try:
        validator._validate_macho_linkage(dylib, app)
        assert False
    except SystemExit as exc:
        assert 'category=install_id ref=libexample.dylib' in str(exc)


def test_validate_macho_linkage_handles_universal_bundle_and_matching_dylib_ids(monkeypatch, tmp_path):
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    bundle = app / 'Contents/Resources/python-runtime/lib/python3.11/site-packages/ada92cb5d92a588d1b93__mypyc.cpython-311-darwin.so'
    dylib = app / 'Contents/Resources/python-runtime/lib/libexample.dylib'
    bundle.parent.mkdir(parents=True)
    dylib.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b'macho')
    dylib.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    def fake_file(cmd, check=False, capture_output=True, text=True):
        if cmd[0] == 'file':
            kind = 'bundle' if str(cmd[1]).endswith('.so') else 'dynamically linked shared library'
            return subprocess.CompletedProcess(cmd, 0, f'Mach-O universal binary {kind}', '')
        raise AssertionError(cmd)

    ids = {'x86_64': '@rpath/libexample.dylib', 'arm64': '@rpath/libexample.dylib'}

    def fake_run(cmd):
        if cmd[:2] == ['lipo', '-archs']:
            return 'x86_64 arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            path = Path(cmd[-1])
            arch = cmd[cmd.index('-arch') + 1]
            return f'{path} (architecture {arch}):\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n'
        if cmd[0] == 'otool' and '-l' in cmd:
            path = Path(cmd[-1])
            if path == bundle:
                return 'Load command 0\ncmd LC_RPATH\ncmdsize 32\npath @loader_path (offset 12)\n'
            arch = cmd[cmd.index('-arch') + 1]
            return f'Load command 0\ncmd LC_ID_DYLIB\ncmdsize 64\nname {ids[arch]} (offset 24)\n'
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D must not be used')
        raise AssertionError(cmd)

    monkeypatch.setattr(validator.subprocess, 'run', fake_file)
    monkeypatch.setattr(validator, '_run', fake_run)
    validator._validate_macho_linkage(bundle, app)
    validator._validate_macho_linkage(dylib, app)

    ids['arm64'] = '@rpath/libdifferent.dylib'
    try:
        validator._validate_macho_linkage(dylib, app)
        assert False
    except SystemExit as exc:
        assert 'install IDs differ by architecture' in str(exc)


def test_validator_parse_otool_libraries_rejects_structural_errors(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    owner = tmp_path / 'Example.app' / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'libexample.dylib'
    valid_header = f'{owner} (architecture arm64):'
    cases = [
        f'{valid_header}\n{valid_header}\n',
        '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n',
        f'{valid_header}\n/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n',
        '\n',
    ]
    for output in cases:
        try:
            validator._parse_otool_libraries(output, owner, 'arm64')
            assert False
        except SystemExit:
            pass


def test_validator_arch_and_install_id_parsers_reject_bad_shapes(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    binary = tmp_path / 'Example.app' / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'libexample.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')

    monkeypatch.setattr(validator, '_run', lambda cmd: '')
    try:
        validator._macho_archs(binary)
        assert False
    except SystemExit as exc:
        assert 'no architectures' in str(exc)

    monkeypatch.setattr(validator, '_run', lambda cmd: 'x86_64')
    try:
        validator._macho_archs(binary)
        assert False
    except SystemExit as exc:
        assert 'not arm64' in str(exc)

    try:
        validator._parse_otool_install_ids('Load command 0\ncmd LC_ID_DYLIB\ncmdsize 48\n')
        assert False
    except SystemExit as exc:
        assert 'without name' in str(exc)
    try:
        validator._parse_otool_install_ids(
            'Load command 0\ncmd LC_ID_DYLIB\nname @rpath/a.dylib (offset 24)\n'
            'Load command 1\ncmd LC_ID_DYLIB\nname @rpath/b.dylib (offset 24)\n'
        )
        assert False
    except SystemExit as exc:
        assert 'multiple LC_ID_DYLIB' in str(exc)


def test_validator_native_ref_and_macho_kind_edge_paths(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    owner = app / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'libexample.dylib'
    owner.parent.mkdir(parents=True)
    owner.write_text('x')

    validator._validate_macho_ref('', owner, app)
    assert validator._macho_file_kind('plain text') == 'other'
    try:
        validator._validate_macho_ref('/usr/lib/libSystem.B.dylib', owner, app, install_id=True)
        assert False
    except SystemExit as exc:
        assert 'absolute Mach-O install ID' in str(exc)
    try:
        validator._validate_macho_ref('@rpath/libexample.dylib', owner, app, rpath=True)
        assert False
    except SystemExit as exc:
        assert 'forbidden external Mach-O LC_RPATH' in str(exc)


def test_validator_run_and_sha_failure_paths(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    monkeypatch.setattr(
        validator.subprocess,
        'run',
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 3, 'out', 'err'),
    )
    try:
        validator._run(['bad'])
        assert False
    except SystemExit as exc:
        assert 'Command failed (bad)' in str(exc)

    payload = tmp_path / 'payload'
    payload.write_bytes(b'abc')
    assert validator._sha256(payload) == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'


def test_validate_macho_linkage_rejects_file_and_lipo_failures(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    binary = app / 'Contents' / 'Resources' / 'python-runtime' / 'lib' / 'libexample.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')

    monkeypatch.setattr(
        validator.subprocess,
        'run',
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, '', 'file failed'),
    )
    try:
        validator._validate_macho_linkage(binary, app)
        assert False
    except SystemExit as exc:
        assert 'Command failed (file ' in str(exc)

    monkeypatch.setattr(
        validator.subprocess,
        'run',
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', ''),
    )
    monkeypatch.setattr(validator, '_run', lambda cmd: (_ for _ in ()).throw(SystemExit('lipo failed')))
    try:
        validator._validate_macho_linkage(binary, app)
        assert False
    except SystemExit as exc:
        assert 'lipo failed' in str(exc)

def test_validator_app_only_main_validates_app_without_dmg(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    contents = app / 'Contents'
    macos = contents / 'MacOS'
    resources = contents / 'Resources'
    macos.mkdir(parents=True)
    resources.mkdir()
    executable = macos / 'token-place-desktop-tauri'
    executable.write_bytes(b'exe')
    icon = tmp_path / 'icon.icns'
    icon.write_bytes(b'icon')
    bundled_icon = resources / 'icon.icns'
    bundled_icon.write_bytes(b'icon')
    info = {
        'CFBundleName': 'token.place desktop',
        'CFBundleDisplayName': 'token.place desktop',
        'CFBundleExecutable': executable.name,
        'CFBundleIconFile': 'icon.icns',
    }
    (contents / 'Info.plist').write_bytes(validator.plistlib.dumps(info))
    tauri_config = tmp_path / 'tauri.conf.json'
    tauri_config.write_text(json.dumps({'bundle': {'icon': ['icons/icon.icns', 'icons/icon.ico', 'icons/128x128@2x.png']}}))
    embedded_calls = []
    dmg_calls = []
    monkeypatch.setattr(
        validator,
        '_parse_args',
        lambda: validator.argparse.Namespace(
            app_path=str(app),
            dmg_path=None,
            app_only=True,
            tauri_config=str(tauri_config),
            expected_icon=str(icon),
            expect_signing=False,
            require_embedded_python_runtime=True,
            expect_notarization=False,
        ),
    )
    monkeypatch.setattr(validator, '_run', lambda cmd: 'arm64' if cmd[:2] == ['lipo', '-archs'] else '')
    monkeypatch.setattr(validator, '_validate_embedded_python_runtime', lambda path: embedded_calls.append(path))
    monkeypatch.setattr(validator, '_validate_dmg_contents', lambda *args, **kwargs: dmg_calls.append(args))

    validator.main()

    assert len(embedded_calls) == 1
    assert embedded_calls[0].name == app.name
    assert embedded_calls[0] != app
    assert dmg_calls == []


def test_validator_dmg_contents_checks_preview_readme(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    mount = tmp_path / 'mount'
    mount.mkdir()
    (mount / 'token.place desktop.app').mkdir()
    readme = mount / 'README BEFORE OPENING.txt'
    readme.write_text(
        'This preview build is ad-hoc signed and not notarized.\n'
        'Apple could not verify. Open Privacy & Security. Developer ID notarization.\n',
        encoding='utf-8',
    )

    class MountHandle:
        name = str(mount)
        cleaned = False

        def cleanup(self):
            self.cleaned = True

    handle = MountHandle()
    cleanup_calls = []
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: '/usr/bin/codesign' if name == 'codesign' else None)
    monkeypatch.setattr(validator, '_run', lambda cmd: '')
    monkeypatch.setattr(validator, '_attach_dmg_with_retries', lambda dmg: handle)
    monkeypatch.setattr(validator, '_cleanup_dmg_attach_state', lambda dmg, path: cleanup_calls.append((dmg, path)))

    dmg = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    dmg.write_bytes(b'dmg')
    validator._validate_dmg_contents(dmg, expect_signing=False)

    assert cleanup_calls == [(dmg, mount)]
    assert handle.cleaned is True


def test_validator_full_main_validates_dmg_and_signing(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    contents = app / 'Contents'
    macos = contents / 'MacOS'
    resources = contents / 'Resources'
    macos.mkdir(parents=True)
    resources.mkdir()
    executable = macos / 'token-place-desktop-tauri'
    executable.write_bytes(b'exe')
    icon = tmp_path / 'icon.icns'
    icon.write_bytes(b'icon')
    (resources / 'token-icon.icns').write_bytes(b'icon')
    (contents / 'Info.plist').write_bytes(
        validator.plistlib.dumps(
            {
                'CFBundleName': 'token.place desktop',
                'CFBundleDisplayName': 'token.place desktop',
                'CFBundleExecutable': executable.name,
                'CFBundleIconFile': 'token-icon',
            }
        )
    )
    tauri_config = tmp_path / 'tauri.conf.json'
    tauri_config.write_text(json.dumps({'bundle': {'icon': ['icons/icon.icns', 'icons/icon.ico', 'icons/128x128@2x.png']}}))
    dmg = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    dmg.write_bytes(b'dmg')
    dmg_calls = []
    run_calls = []
    monkeypatch.setattr(
        validator,
        '_parse_args',
        lambda: validator.argparse.Namespace(
            app_path=str(app),
            dmg_path=str(dmg),
            app_only=False,
            tauri_config=str(tauri_config),
            expected_icon=str(icon),
            expect_signing=True,
            require_embedded_python_runtime=False,
            expect_notarization=True,
        ),
    )
    monkeypatch.setattr(validator, '_validate_dmg_contents', lambda path, **kwargs: dmg_calls.append((path, kwargs.get('expect_signing'))))

    def fake_run(cmd):
        run_calls.append(cmd)
        return 'arm64' if cmd[:2] == ['lipo', '-archs'] else ''

    monkeypatch.setattr(validator, '_run', fake_run)
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: '/usr/bin/codesign' if name == 'codesign' else None)

    validator.main()

    assert dmg_calls == [(dmg, True)]
    assert run_calls.count(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)]) == 2
    assert ['spctl', '-a', '-vv', '--type', 'execute', str(app)] in run_calls


def test_codesign_verify_fails_on_darwin_when_codesign_missing(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    app.mkdir()
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: None)

    try:
        validator._codesign_verify(app)
        assert False
    except SystemExit as exc:
        assert 'codesign not found in PATH on this macOS machine' in str(exc)


def test_codesign_verify_skips_outside_darwin_when_codesign_missing(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'Example.app'
    app.mkdir()
    calls = []
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: None)
    monkeypatch.setattr(validator, '_run', lambda cmd: calls.append(cmd))

    validator._codesign_verify(app)

    assert calls == []


def test_validator_dmg_contents_rejects_missing_signed_preview_phrase(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    mount = tmp_path / 'mount'
    mount.mkdir()
    (mount / 'token.place desktop.app').mkdir()
    (mount / 'README BEFORE OPENING.txt').write_text(
        'This preview build is ad-hoc signed and not notarized.\n'
        'Apple could not verify. Open Privacy & Security. Developer ID notarization.\n',
        encoding='utf-8',
    )

    class MountHandle:
        name = str(mount)
        def cleanup(self):
            pass

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_attach_dmg_with_retries', lambda dmg: MountHandle())
    monkeypatch.setattr(validator, '_cleanup_dmg_attach_state', lambda dmg, path: None)

    try:
        validator._validate_dmg_contents(tmp_path / 'token.place-desktop-test-apple-silicon.dmg', expect_signing=True)
        assert False
    except SystemExit as exc:
        assert 'configured Apple signing identity' in str(exc)


def _minimal_validator_app(validator, tmp_path: Path) -> tuple[Path, Path, Path]:
    app = tmp_path / 'token.place desktop.app'
    contents = app / 'Contents'
    macos = contents / 'MacOS'
    resources = contents / 'Resources'
    macos.mkdir(parents=True)
    resources.mkdir()
    executable = macos / 'token-place-desktop-tauri'
    executable.write_bytes(b'exe')
    icon = tmp_path / 'icon.icns'
    icon.write_bytes(b'icon')
    (resources / 'token-icon.icns').write_bytes(b'icon')
    (contents / 'Info.plist').write_bytes(
        validator.plistlib.dumps(
            {
                'CFBundleName': 'token.place desktop',
                'CFBundleDisplayName': 'token.place desktop',
                'CFBundleExecutable': executable.name,
                'CFBundleIconFile': 'token-icon',
            }
        )
    )
    tauri_config = tmp_path / 'tauri.conf.json'
    tauri_config.write_text(
        json.dumps({'bundle': {'icon': ['icons/icon.icns', 'icons/icon.ico', 'icons/128x128@2x.png']}}),
        encoding='utf-8',
    )
    return app, tauri_config, icon


def test_validator_main_rejects_app_and_dmg_shape_errors(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app, tauri_config, icon = _minimal_validator_app(validator, tmp_path)

    def set_args(**overrides):
        values = {
            'app_path': str(app),
            'dmg_path': None,
            'app_only': False,
            'tauri_config': str(tauri_config),
            'expected_icon': str(icon),
            'expect_signing': False,
            'require_embedded_python_runtime': False,
            'expect_notarization': False,
        }
        values.update(overrides)
        monkeypatch.setattr(validator, '_parse_args', lambda: validator.argparse.Namespace(**values))

    for overrides, expected in (
        ({}, '--dmg-path is required'),
        ({'dmg_path': str(tmp_path / 'artifact.zip')}, 'dmg artifact missing'),
        ({'dmg_path': str(tmp_path / 'wrong.dmg')}, 'DMG filename must match'),
    ):
        set_args(**overrides)
        if overrides.get('dmg_path'):
            Path(overrides['dmg_path']).write_bytes(b'dmg')
        try:
            validator.main()
            assert False
        except SystemExit as exc:
            assert expected in str(exc)

    bad_name = tmp_path / 'token.place-desktop-test.dmg'
    bad_name.write_bytes(b'dmg')
    set_args(dmg_path=str(bad_name))
    try:
        validator.main()
        assert False
    except SystemExit as exc:
        assert 'DMG filename must match' in str(exc)

    set_args(app_only=True, app_path=str(tmp_path / 'missing.app'))
    try:
        validator.main()
        assert False
    except SystemExit as exc:
        assert 'app bundle missing' in str(exc)

    set_args(app_only=True, expected_icon=str(tmp_path / 'missing.icns'))
    try:
        validator.main()
        assert False
    except SystemExit as exc:
        assert 'expected icon missing' in str(exc)


def test_validator_embedded_runtime_failure_paths(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app, _tauri_config, _icon = _minimal_validator_app(validator, tmp_path)
    runtime = app / 'Contents' / 'Resources' / 'python-runtime'
    py = runtime / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('python')
    py.chmod(0o755)

    try:
        validator._validate_embedded_python_runtime(app)
        assert False
    except SystemExit as exc:
        assert 'embedded runtime provenance missing' in str(exc)

    (runtime / 'embedded_python_runtime_provenance.json').write_text('{}')
    try:
        validator._validate_embedded_python_runtime(app)
        assert False
    except SystemExit as exc:
        assert 'embedded runtime notice missing' in str(exc)

    for notice in ('LICENSE-PYTHON.txt', 'LICENSE-python-build-standalone.txt'):
        (runtime / notice).write_text('notice')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_run', lambda cmd: 'x86_64' if cmd[:2] == ['lipo', '-archs'] else '')
    try:
        validator._validate_embedded_python_runtime(app)
        assert False
    except SystemExit as exc:
        assert 'embedded Python is not arm64' in str(exc)

    payload = {
        'version': [3, 11],
        'machine': 'arm64',
        'executable': str(py),
        'prefix': str(runtime),
        'llama_cpp_python_version': '0.3.32',
    }
    probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'qwen_64k_yarn_support': 'supported',
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': True,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {'flash_attn': True, 'offload_kqv': True, 'n_batch': True, 'n_ubatch': True},
    }
    calls = []
    monkeypatch.setattr(validator, '_run', lambda cmd: 'arm64')
    monkeypatch.setattr(
        validator,
        '_run_python_sanitized',
        lambda _python, code, app_path: calls.append(code) or json.dumps(
            payload if 'version_info' in code else (
                {
                    'runtime_action_ok': True,
                    'facade_type': '_SubprocessLlamaCppModule',
                    'backend': 'metal',
                    'gpu_offload_supported': True,
                    'version': '0.3.32',
                    'yarn_resolver_source': 'top_level_enum',
                    'constructor_signature_inspectable': True,
                    'required_kwargs_supported': True,
                    'llama_module_identity_match': True,
                    'supported': True,
                    'desktop_probe_authoritative': True,
                    'secondary_reprobe_skipped': True,
                } if 'token-place-release-qwen64k-probe' in code else probe
            )
        ),
    )

    try:
        validator._validate_embedded_python_runtime(app)
        assert False
    except SystemExit as exc:
        assert 'packaged model_bridge.py missing' in str(exc)

    (app / 'Contents' / 'Resources' / 'python').mkdir()
    (app / 'Contents' / 'Resources' / 'python' / 'model_bridge.py').write_text('print("inspect")')
    monkeypatch.setattr(validator, '_validate_macho_linkage', lambda path, app_path: calls.append(str(path)))
    validator._validate_embedded_python_runtime(app)
    assert any('model_bridge.py' in code for code in calls)


@pytest.mark.parametrize(
    ('mutate_payload', 'mutate_probe', 'expected'),
    (
        (lambda payload: payload.update({'version': [3, 12]}), lambda probe: None, 'embedded Python is not CPython 3.11'),
        (lambda payload: payload.update({'machine': 'x86_64'}), lambda probe: None, 'embedded Python is not arm64'),
        (lambda payload: payload.update({'llama_cpp_python_version': '0.3.31'}), lambda probe: None, 'embedded runtime has wrong llama-cpp-python version'),
        (lambda payload: payload.update({'prefix': '/tmp/outside-python-runtime'}), lambda probe: None, 'embedded Python prefix escaped app bundle'),
        (lambda payload: None, lambda probe: probe.update({'backend': 'cpu'}), 'embedded runtime probe did not report Metal GPU offload'),
        (lambda payload: None, lambda probe: probe.update({'qwen_64k_yarn_support': 'unsupported'}), 'embedded runtime probe missing capability: qwen_64k_yarn_support'),
        (lambda payload: None, lambda probe: probe.update({'rope_freq_scale_supported': False}), 'embedded runtime probe missing capability: rope_freq_scale'),
        (lambda payload: None, lambda probe: probe.update({'constructor_kwarg_support': {'flash_attn': True, 'offload_kqv': True, 'n_batch': True}}), 'embedded runtime probe missing capability: n_ubatch'),
    ),
)
def test_validate_embedded_python_runtime_safe_capability_failures(
    monkeypatch, tmp_path, mutate_payload, mutate_probe, expected
) -> None:
    validator = _load_release_artifact_validator()
    app, _tauri_config, _icon = _minimal_validator_app(validator, tmp_path)
    runtime = app / 'Contents' / 'Resources' / 'python-runtime'
    py = runtime / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('python', encoding='utf-8')
    py.chmod(0o755)
    for notice in (
        'embedded_python_runtime_provenance.json',
        'LICENSE-PYTHON.txt',
        'LICENSE-python-build-standalone.txt',
    ):
        (runtime / notice).write_text('notice', encoding='utf-8')

    payload = {
        'version': [3, 11],
        'machine': 'arm64',
        'executable': str(py),
        'prefix': str(runtime),
        'llama_cpp_python_version': '0.3.32',
    }
    probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'qwen_64k_yarn_support': 'supported',
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': True,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {
            'flash_attn': True,
            'offload_kqv': True,
            'n_batch': True,
            'n_ubatch': True,
        },
    }
    mutate_payload(payload)
    mutate_probe(probe)

    def fake_run_python(_python, code, _app_path):
        if 'version_info' in code:
            return json.dumps(payload)
        if '_probe_llama_runtime' in code:
            return json.dumps(probe)
        return 'ok'

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_run', lambda cmd: 'arm64')
    monkeypatch.setattr(validator, '_run_python_sanitized', fake_run_python)

    with pytest.raises(SystemExit) as excinfo:
        validator._validate_embedded_python_runtime(app)

    assert expected in str(excinfo.value)


def test_validate_embedded_background_probe_failure_diagnostics_are_path_free(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app, _tauri_config, _icon = _minimal_validator_app(validator, tmp_path)
    runtime = app / 'Contents' / 'Resources' / 'python-runtime'
    py = runtime / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('python', encoding='utf-8')
    py.chmod(0o755)
    for notice in (
        'embedded_python_runtime_provenance.json',
        'LICENSE-PYTHON.txt',
        'LICENSE-python-build-standalone.txt',
    ):
        (runtime / notice).write_text('notice', encoding='utf-8')
    resources = app / 'Contents' / 'Resources' / 'python'
    resources.mkdir(parents=True)
    (resources / 'model_bridge.py').write_text('print("inspect")', encoding='utf-8')
    private_path = str((tmp_path / 'private' / 'llama_cpp' / '__init__.py').resolve())
    private_digest = 'sha256:' + ('b' * 64)

    payload = {
        'version': [3, 11],
        'machine': 'arm64',
        'executable': str(py),
        'prefix': str(runtime),
        'llama_cpp_python_version': '0.3.32',
    }
    probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'qwen_64k_yarn_support': 'supported',
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': True,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {
            'flash_attn': True,
            'offload_kqv': True,
            'n_batch': True,
            'n_ubatch': True,
        },
    }
    background = {
        'runtime_action': 'metal_already_supported',
        'runtime_action_ok': True,
        'selected_backend': 'metal',
        'llama_cpp_python_version_match': 'match',
        'capability_source': 'desktop_runtime_setup_probe',
        'incomplete_probe_fields': ['llama_module_identity_match'],
        'facade_type': '_SubprocessLlamaCppModule',
        'backend': 'metal',
        'gpu_offload_supported': True,
        'version': '0.3.32',
        'yarn_resolver_source': 'top_level_enum',
        'constructor_signature_inspectable': True,
        'required_kwargs_supported': True,
        'llama_module_identity_match': False,
        'supported': False,
        'desktop_probe_authoritative': True,
        'secondary_reprobe_skipped': True,
        'llama_module_identity': private_digest,
        'llama_module_path': private_path,
    }

    def fake_run_python(_python, code, _app_path):
        if 'version_info' in code:
            return json.dumps(payload)
        if 'token-place-release-qwen64k-probe' in code:
            return 'probe log without secrets\n' + json.dumps(background)
        if 'model_bridge.py' in code:
            return 'model bridge ok'
        return json.dumps(probe)

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_run', lambda cmd: 'arm64')
    monkeypatch.setattr(validator, '_run_python_sanitized', fake_run_python)
    monkeypatch.setattr(validator, '_validate_macho_linkage', lambda path, app_path: None)

    with pytest.raises(SystemExit) as excinfo:
        validator._validate_embedded_python_runtime(app)

    message = str(excinfo.value)
    assert 'embedded background Qwen 64K facade probe failed llama_module_identity_match: False' in message
    assert "'capability_source': 'desktop_runtime_setup_probe'" in message
    assert "'incomplete_probe_fields': ['llama_module_identity_match']" in message
    assert private_digest not in message
    assert private_path not in message


@pytest.mark.parametrize(
    ('missing_key', 'bad_value'),
    (
        ('runtime_action_ok', False),
        ('backend', None),
        ('gpu_offload_supported', False),
        ('required_kwargs_supported', False),
    ),
)
def test_validate_embedded_background_probe_reports_each_safe_failure_key(monkeypatch, tmp_path, missing_key, bad_value) -> None:
    validator = _load_release_artifact_validator()
    app, _tauri_config, _icon = _minimal_validator_app(validator, tmp_path)
    runtime = app / 'Contents' / 'Resources' / 'python-runtime'
    py = runtime / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('python', encoding='utf-8')
    py.chmod(0o755)
    for notice in (
        'embedded_python_runtime_provenance.json',
        'LICENSE-PYTHON.txt',
        'LICENSE-python-build-standalone.txt',
    ):
        (runtime / notice).write_text('notice', encoding='utf-8')
    resources = app / 'Contents' / 'Resources' / 'python'
    resources.mkdir(parents=True)
    (resources / 'model_bridge.py').write_text('print("inspect")', encoding='utf-8')
    payload = {
        'version': [3, 11],
        'machine': 'arm64',
        'executable': str(py),
        'prefix': str(runtime),
        'llama_cpp_python_version': '0.3.32',
    }
    probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'qwen_64k_yarn_support': 'supported',
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': True,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {'flash_attn': True, 'offload_kqv': True, 'n_batch': True, 'n_ubatch': True},
    }
    background = {
        'runtime_action_ok': True,
        'facade_type': '_SubprocessLlamaCppModule',
        'backend': 'metal',
        'gpu_offload_supported': True,
        'version': '0.3.32',
        'yarn_resolver_source': 'top_level_enum',
        'constructor_signature_inspectable': True,
        'required_kwargs_supported': True,
        'llama_module_identity_match': True,
        'supported': True,
        'desktop_probe_authoritative': True,
        'secondary_reprobe_skipped': True,
        'runtime_action': 'metal_already_supported',
        'selected_backend': 'metal',
        'llama_cpp_python_version_match': True,
        'capability_source': 'desktop_runtime_setup_probe',
        'incomplete_probe_fields': [],
    }
    background[missing_key] = bad_value

    def fake_run_python(_python, code, _app_path):
        if 'version_info' in code:
            return json.dumps(payload)
        if 'token-place-release-qwen64k-probe' in code:
            return json.dumps(background)
        if 'model_bridge.py' in code:
            return 'model bridge ok'
        return json.dumps(probe)

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_run', lambda cmd: 'arm64')
    monkeypatch.setattr(validator, '_run_python_sanitized', fake_run_python)
    monkeypatch.setattr(validator, '_validate_macho_linkage', lambda path, app_path: None)

    with pytest.raises(SystemExit) as excinfo:
        validator._validate_embedded_python_runtime(app)

    message = str(excinfo.value)
    assert f'embedded background Qwen 64K facade probe failed {missing_key}: {bad_value!r}' in message
    assert 'desktop_runtime_setup_probe' in message
    assert 'diagnostics=' in message

def test_run_python_sanitized_disables_bytecode_and_uses_external_writable_locations(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = app / 'Contents' / 'Resources' / 'python-runtime' / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\n', encoding='utf-8')
    captured = {}

    def fake_run(cmd, *, check, capture_output, text, env, cwd=None):
        assert Path(env['HOME']).is_dir()
        assert Path(env['TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET']).is_dir()
        captured.update(cmd=cmd, env=env, cwd=cwd)
        return subprocess.CompletedProcess(cmd, 0, 'ok', '')

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    assert validator._run_python_sanitized(py, 'print(1)', app) == 'ok'
    assert captured['cmd'][1] == '-B'
    env = captured['env']
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert env['PYTHONNOUSERSITE'] == '1'
    assert env['PATH'] == '/usr/bin:/bin'
    assert env['PYTHONPATH'] == subprocess.os.pathsep.join([
        str(app / 'Contents' / 'Resources' / 'python'),
        str(app / 'Contents' / 'Resources'),
    ])
    writable_keys = [
        'PYTHONPYCACHEPREFIX',
        'TMPDIR',
        'PIP_CACHE_DIR',
        'HOME',
        'XDG_CACHE_HOME',
        'XDG_CONFIG_HOME',
        'XDG_DATA_HOME',
        'TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET',
        'TOKEN_PLACE_MODELS_DIR',
        'HF_HOME',
        'TRANSFORMERS_CACHE',
    ]
    for key in writable_keys:
        assert not Path(env[key]).resolve().is_relative_to(app.resolve()), key
    assert not Path(captured['cwd']).resolve().is_relative_to(app.resolve())


def test_run_python_sanitized_real_import_and_child_import_do_not_create_pycache(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    pkg = app / 'Contents' / 'Resources' / 'python'
    pkg.mkdir(parents=True)
    (pkg / 'probe_module.py').write_text('VALUE = 42\n', encoding='utf-8')
    code = "import probe_module, subprocess, sys; assert probe_module.VALUE == 42; subprocess.check_call([sys.executable, '-c', 'import probe_module; assert probe_module.VALUE == 42'])"
    validator._run_python_sanitized(Path(__import__('sys').executable), code, app)
    assert not list(pkg.rglob('__pycache__'))
    assert not list(pkg.rglob('*.pyc'))


def test_app_tree_fingerprint_reports_mutations(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'x.app'
    d = app / 'Contents' / 'Resources'
    d.mkdir(parents=True)
    f = d / 'file.txt'; f.write_text('one', encoding='utf-8')
    exe = d / 'tool'; exe.write_text('tool', encoding='utf-8'); exe.chmod(0o644)
    link = d / 'link'; link.symlink_to('file.txt')
    base = validator._app_tree_fingerprint(app)
    pyc = d / '__pycache__' / 'm.cpython-311.pyc'; pyc.parent.mkdir(); pyc.write_bytes(b'pyc')
    f.unlink()
    exe.write_text('changed', encoding='utf-8'); exe.chmod(0o755)
    link.unlink(); link.symlink_to('tool')
    changes = '\n'.join(validator._describe_app_tree_changes(base, validator._app_tree_fingerprint(app)))
    assert 'unsealed Python bytecode' in changes
    assert 'removed: Contents/Resources/file.txt' in changes
    assert 'rewritten: Contents/Resources/tool' in changes
    mode_only = app / 'Contents' / 'Resources' / 'mode-only'
    mode_only.write_text('same', encoding='utf-8')
    before_mode = validator._app_tree_fingerprint(app)
    mode_only.chmod(0o755)
    mode_changes = validator._describe_app_tree_changes(before_mode, validator._app_tree_fingerprint(app))
    assert 'chmodded: Contents/Resources/mode-only' in mode_changes
    assert 'retargeted symlink: Contents/Resources/link' in changes


def test_mutation_guard_fails_for_mutating_probe(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'x.app'; app.mkdir()
    try:
        validator._run_with_app_mutation_guard(app, 'test probe', lambda: (app / '__pycache__' / 'x.pyc').parent.mkdir() or (app / '__pycache__' / 'x.pyc').write_bytes(b'x'))
        assert False
    except SystemExit as exc:
        assert 'unsealed Python bytecode' in str(exc)


def test_codesign_verification_is_ad_hoc_and_not_spctl_gated(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'x.app'; app.mkdir()
    calls = []
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_run', lambda cmd: calls.append(cmd) or '')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: '/usr/bin/codesign' if name == 'codesign' else None)
    validator._codesign_verify(app)
    assert ['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)] in calls
    assert not any(cmd and cmd[0] in {'spctl', 'notarytool', 'stapler'} for cmd in calls)


def test_dmg_validation_runs_against_mounted_app(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    mount = tmp_path / 'mount'; mount.mkdir()
    mounted_app = mount / 'mounted.app'; mounted_app.mkdir()
    (mount / 'README BEFORE OPENING.txt').write_text('ad-hoc signed not notarized Apple could not verify Privacy & Security Developer ID notarization', encoding='utf-8')
    class Handle:
        name = str(mount)
        def cleanup(self): pass
    seen = []
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator, '_attach_dmg_with_retries', lambda dmg: Handle())
    monkeypatch.setattr(validator, '_cleanup_dmg_attach_state', lambda dmg, path: None)
    monkeypatch.setattr(validator, '_validate_embedded_python_runtime_non_mutating', lambda app: seen.append(('runtime', app)))
    monkeypatch.setattr(validator, '_codesign_verify', lambda app: seen.append(('codesign', app)))
    dmg = tmp_path / 'token.place-desktop-x-apple-silicon.dmg'; dmg.write_bytes(b'dmg')
    validator._validate_dmg_contents(dmg, expect_signing=False, require_embedded_python_runtime=True)
    assert ('runtime', mounted_app) in seen
    assert ('codesign', mounted_app) in seen


def test_validator_main_macos_require_runtime_without_dmg_validates_disposable_copy(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app, tauri_config, icon = _minimal_validator_app(validator, tmp_path)
    runtime_apps = []
    codesign_apps = []
    run_calls = []
    monkeypatch.setattr(
        validator,
        '_parse_args',
        lambda: validator.argparse.Namespace(
            app_path=str(app),
            dmg_path=None,
            app_only=False,
            tauri_config=str(tauri_config),
            expected_icon=str(icon),
            expect_signing=False,
            require_embedded_python_runtime=True,
            expect_notarization=False,
        ),
    )
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: '/usr/bin/codesign' if name == 'codesign' else None)

    def fake_run(cmd):
        run_calls.append(cmd)
        return 'arm64' if cmd[:2] == ['lipo', '-archs'] else ''

    def fake_runtime(probe_app):
        runtime_apps.append(probe_app)
        assert probe_app != app
        assert probe_app.name == app.name
        assert probe_app.exists()

    monkeypatch.setattr(validator, '_run', fake_run)
    monkeypatch.setattr(validator, '_validate_embedded_python_runtime_non_mutating', fake_runtime)
    original_codesign = validator._codesign_verify
    monkeypatch.setattr(validator, '_codesign_verify', lambda probe_app: codesign_apps.append(probe_app) or original_codesign(probe_app))

    validator.main()

    assert len(runtime_apps) == 1
    assert runtime_apps[0].parent != app.parent
    assert codesign_apps.count(app) == 2
    assert runtime_apps[0] in codesign_apps


def test_validator_main_macos_dmg_runtime_validation_does_not_probe_source_app(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app, tauri_config, icon = _minimal_validator_app(validator, tmp_path)
    dmg = tmp_path / 'token.place-desktop-test-apple-silicon.dmg'
    dmg.write_bytes(b'dmg')
    dmg_calls = []
    runtime_apps = []
    run_calls = []
    monkeypatch.setattr(
        validator,
        '_parse_args',
        lambda: validator.argparse.Namespace(
            app_path=str(app),
            dmg_path=str(dmg),
            app_only=False,
            tauri_config=str(tauri_config),
            expected_icon=str(icon),
            expect_signing=False,
            require_embedded_python_runtime=True,
            expect_notarization=False,
        ),
    )
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(validator.shutil, 'which', lambda name: '/usr/bin/codesign' if name == 'codesign' else None)
    monkeypatch.setattr(validator, '_validate_dmg_contents', lambda path, **kwargs: dmg_calls.append((path, kwargs)))
    monkeypatch.setattr(validator, '_validate_embedded_python_runtime_non_mutating', lambda probe_app: runtime_apps.append(probe_app))

    def fake_run(cmd):
        run_calls.append(cmd)
        return 'arm64' if cmd[:2] == ['lipo', '-archs'] else ''

    monkeypatch.setattr(validator, '_run', fake_run)

    validator.main()

    assert dmg_calls == [(dmg, {'expect_signing': False, 'require_embedded_python_runtime': True})]
    assert runtime_apps == []
    assert run_calls.count(['codesign', '--verify', '--deep', '--strict', '--verbose=4', str(app)]) == 2


def test_validate_embedded_python_runtime_requires_background_facade_probe(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app, _tauri_config, _icon = _minimal_validator_app(validator, tmp_path)
    runtime = app / 'Contents' / 'Resources' / 'python-runtime'
    py = runtime / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('python')
    py.chmod(0o755)
    (runtime / 'embedded_python_runtime_provenance.json').write_text('{}')
    (runtime / 'LICENSE-PYTHON.txt').write_text('notice')
    (runtime / 'LICENSE-python-build-standalone.txt').write_text('notice')
    resources_python = app / 'Contents' / 'Resources' / 'python'
    resources_python.mkdir()
    (resources_python / 'model_bridge.py').write_text('print("inspect")')
    payload = {
        'version': [3, 11],
        'machine': 'arm64',
        'executable': str(py),
        'prefix': str(runtime),
        'llama_cpp_python_version': '0.3.32',
    }
    probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'qwen_64k_yarn_support': 'supported',
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': True,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {'flash_attn': True, 'offload_kqv': True, 'n_batch': True, 'n_ubatch': True},
    }
    background = {
        'runtime_action_ok': True,
        'facade_type': '_SubprocessLlamaCppModule',
        'backend': 'metal',
        'gpu_offload_supported': True,
        'version': '0.3.32',
        'yarn_resolver_source': 'top_level_enum',
        'constructor_signature_inspectable': True,
        'required_kwargs_supported': True,
        'llama_module_identity_match': True,
        'supported': True,
        'desktop_probe_authoritative': True,
        'secondary_reprobe_skipped': False,
    }
    calls = []
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(validator, '_validate_macho_linkage', lambda path, app_path: None)

    def fake_run_python_sanitized(_python, code, _app_path):
        calls.append(code)
        if 'version_info' in code:
            return json.dumps(payload)
        if 'token-place-release-qwen64k-probe' in code:
            return json.dumps(background)
        if '_probe_llama_runtime' in code:
            return json.dumps(probe)
        return 'ok'

    monkeypatch.setattr(validator, '_run_python_sanitized', fake_run_python_sanitized)
    with pytest.raises(SystemExit, match='secondary_reprobe_skipped'):
        validator._validate_embedded_python_runtime(app)
    background['secondary_reprobe_skipped'] = True
    validator._validate_embedded_python_runtime(app)
    assert any('ensure_runtime_import_paths(' in code for code in calls)
    assert any("repo_root=runtime_import_root" in code for code in calls)
    assert any('_runtime_supports_qwen_yarn_rope' in code for code in calls)
    assert any('_import_llama_cpp_runtime' in code for code in calls)
    assert any('_safe_constructor_capability_payload' in code for code in calls)
    assert any("facade_capabilities.get('constructor_kwarg_support')" in code for code in calls)
    assert not any("gate.get('backend')" in code for code in calls)
    assert not any("gate.get('gpu_offload_supported')" in code for code in calls)
    assert not any("gate.get('constructor_kwarg_support')" in code for code in calls)
    assert not any('_import_llama_cpp_subprocess_module' in code for code in calls)


def _load_windows_release_validator():
    import importlib.util
    script_path = Path('scripts/validate_windows_desktop_release_artifacts.py')
    spec = importlib.util.spec_from_file_location('validate_windows_desktop_release_artifacts', script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_windows_runtime_fixture(root: Path, *, version: str = '0.1.2') -> tuple[Path, Path]:
    validator = _load_windows_release_validator()
    manifest = json.loads(Path('desktop-tauri/src-tauri/python/embedded_python_runtime_windows_x86_64_manifest.json').read_text(encoding='utf-8'))
    runtime = root / 'resources' / 'python-runtime'
    runtime.mkdir(parents=True)
    for name in manifest['required_native_dlls']:
        (runtime / name).write_bytes(b'MZ placeholder')
    (runtime / 'python.exe').write_bytes(b'MZ placeholder')
    provenance = {
        'runtime_id': 'bundled-cpython-3.11-win-x86_64-cu124',
        'cpython_version': '3.11.13',
        'target_triple': 'x86_64-pc-windows-msvc',
        'source_archive_sha256': manifest['sha256'],
        'llama_cpp_cuda_wheel': manifest['llama_cpp_cuda_wheel'],
        'required_packages': manifest['required_packages'],
        'python_package_wheels': manifest['python_package_wheels'],
        'required_native_dlls': manifest['required_native_dlls'],
        'pe_dll_closure': [
            {'name': name, 'path': name, 'machine': 'IMAGE_FILE_MACHINE_AMD64', 'imports': []}
            for name in manifest['required_native_dlls']
        ],
    }
    (runtime / validator.PROVENANCE).write_text(json.dumps(provenance), encoding='utf-8')
    nsis = root / f'token.place-desktop-{version}-setup.exe'
    msi = root / f'token.place-desktop-{version}.msi'
    nsis.mkdir()
    msi.mkdir()
    shutil.copytree(root / 'resources', nsis / 'resources')
    shutil.copytree(root / 'resources', msi / 'resources')
    return nsis, msi


def test_windows_release_validator_accepts_extracted_msi_and_nsis(tmp_path):
    validator = _load_windows_release_validator()
    nsis, msi = _write_windows_runtime_fixture(tmp_path)
    assert validator.main(['--windows-nsis', str(nsis), '--windows-msi', str(msi), '--expected-version', '0.1.2']) == 0


def test_windows_release_validator_rejects_version_and_provenance_mismatch(tmp_path):
    validator = _load_windows_release_validator()
    nsis, msi = _write_windows_runtime_fixture(tmp_path)
    with pytest.raises(validator.ValidationError, match='filename does not contain'):
        validator.validate_artifact(nsis, '9.9.9', 'NSIS', json.loads(validator.MANIFEST.read_text(encoding='utf-8')))
    provenance = next(msi.rglob('embedded_python_runtime_provenance.json'))
    data = json.loads(provenance.read_text(encoding='utf-8'))
    data['llama_cpp_cuda_wheel']['flavor'] = 'cpu'
    provenance.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='incomplete Windows runtime provenance'):
        validator.main(['--windows-nsis', str(nsis), '--windows-msi', str(msi), '--expected-version', '0.1.2'])


def test_release_workflow_runs_windows_validator_and_blocks_publish_on_nvidia_gate() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'Validate Windows MSI and NSIS artifact contents' in text
    assert 'validate_windows_desktop_release_artifacts.py' in text
    assert 'windows-nvidia-release-gate' in text
    assert 'needs: [build, windows-nvidia-release-gate]' in text
    assert 'windows_nvidia_gpu_smoke_test.py --artifact-root release-assets/windows' in text
