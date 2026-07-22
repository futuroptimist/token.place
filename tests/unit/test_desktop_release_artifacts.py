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
    dmg_path = Path('release-artifacts/token.place-desktop-0.1.3-apple-silicon.dmg')
    mount_dir = tmp_path / 'mount'
    mount_dir.mkdir()
    calls = []
    raw_info = """
image-path: /private/var/folders/zz/redacted-test/release-artifacts/token.place-desktop-0.1.3-apple-silicon.dmg
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
    raw_path = '/private/var/folders/zz/example/release-artifacts/token.place-desktop-0.1.3-apple-silicon.dmg'

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




def _load_prepare_module():
    import importlib.util

    script_path = Path('desktop-tauri/scripts/prepare_windows_embedded_python_runtime.py')
    spec = importlib.util.spec_from_file_location('prepare_windows_embedded_python_runtime', script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_windows_release_validator():
    import importlib.util
    script_path = Path('scripts/validate_windows_desktop_release_artifacts.py')
    spec = importlib.util.spec_from_file_location('validate_windows_desktop_release_artifacts', script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_windows_runtime_fixture(root: Path, *, version: str = '0.1.3') -> tuple[Path, Path]:
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


def test_windows_release_workflow_version_args_are_tag_only_and_untagged_derives_package_version() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'validator_version_args=()' in text
    assert 'validator_version_args=(--release-tag "${tag_name}")' in text
    assert '--expected-version "0.1.3"' not in text
    assert r'desktop-v[0-9]+\.[0-9]+\.[0-9]+' in text


def test_windows_validator_without_version_args_derives_package_json_version(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    package = tmp_path / 'package.json'
    package.write_text(json.dumps({'version': '7.8.9'}), encoding='utf-8')
    monkeypatch.setattr(validator, 'PACKAGE_JSON', package)
    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(validator, 'validate_config_versions', lambda expected: observed.append(('version', expected)))
    monkeypatch.setattr(validator, 'validate_artifact', lambda artifact, expected, kind, manifest: observed.append((kind, expected)))
    monkeypatch.setattr(validator, '_load_json', lambda path: {'version': '7.8.9'} if path == package else {})

    assert validator.main([
        '--windows-nsis', str(tmp_path / 'token.place-desktop-7.8.9-setup.exe'),
        '--windows-msi', str(tmp_path / 'token.place-desktop-7.8.9.msi'),
    ]) == 0
    assert observed == [('version', '7.8.9'), ('NSIS', '7.8.9'), ('MSI', '7.8.9')]


def test_windows_release_validator_accepts_extracted_msi_and_nsis(tmp_path):
    validator = _load_windows_release_validator()
    nsis, msi = _write_windows_runtime_fixture(tmp_path)
    assert validator.main(['--windows-nsis', str(nsis), '--windows-msi', str(msi), '--expected-version', '0.1.3']) == 0


def test_windows_release_validator_rejects_version_and_provenance_mismatch(tmp_path):
    validator = _load_windows_release_validator()
    nsis, msi = _write_windows_runtime_fixture(tmp_path)
    wrong_name = tmp_path / 'wrong-token.place-desktop-0.1.3-setup.exe'
    wrong_name.write_bytes(b'installer')
    with pytest.raises(validator.ValidationError, match='filename does not match'):
        validator.validate_artifact(wrong_name, '9.9.9', 'NSIS', json.loads(validator.MANIFEST.read_text(encoding='utf-8')))
    provenance = next(msi.rglob('embedded_python_runtime_provenance.json'))
    data = json.loads(provenance.read_text(encoding='utf-8'))
    data['llama_cpp_cuda_wheel']['flavor'] = 'cpu'
    provenance.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='incomplete Windows runtime provenance'):
        validator.main(['--windows-nsis', str(nsis), '--windows-msi', str(msi), '--expected-version', '0.1.3'])


def test_release_workflow_runs_windows_validator_and_preserves_skipped_nvidia_gate() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'Validate Windows MSI and NSIS artifact contents' in text
    assert 'validate_windows_desktop_release_artifacts.py' in text
    assert 'windows-nvidia-release-gate' in text
    assert 'needs: build' in text
    assert 'Intentionally bypass the hardware smoke gate while no matching self-hosted runner exists.' in text
    assert 'windows_nvidia_gpu_smoke_test.py --artifact-root release-assets/windows' in text


def test_windows_validator_version_tag_config_and_extract_edges(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    assert validator.expected_version_from_tag(None, '0.1.3') == '0.1.3'
    assert validator.expected_version_from_tag('desktop-v1.2.3', '0.1.3') == '1.2.3'
    with pytest.raises(validator.ValidationError, match='desktop-vX.Y.Z'):
        validator.expected_version_from_tag('1495/merge', '0.1.3')

    package = tmp_path / 'package.json'
    lock = tmp_path / 'package-lock.json'
    tauri = tmp_path / 'tauri.conf.json'
    cargo = tmp_path / 'Cargo.toml'
    cargo_lock = tmp_path / 'Cargo.lock'
    package.write_text(json.dumps({'version': '0.1.3'}), encoding='utf-8')
    lock.write_text(json.dumps({'version': '0.1.3'}), encoding='utf-8')
    tauri.write_text(json.dumps({'version': '9.9.9'}), encoding='utf-8')
    cargo.write_text('[package]\nname = "token-place-desktop-tauri"\nversion = "0.1.3"\n', encoding='utf-8')
    cargo_lock.write_text('version = 4\n\n[[package]]\nname = "token-place-desktop-tauri"\nversion = "0.1.3"\n', encoding='utf-8')
    monkeypatch.setattr(validator, 'PACKAGE_JSON', package)
    monkeypatch.setattr(validator, 'PACKAGE_LOCK', lock)
    monkeypatch.setattr(validator, 'TAURI_CONFIG', tauri)
    monkeypatch.setattr(validator, 'CARGO_MANIFEST', cargo)
    monkeypatch.setattr(validator, 'CARGO_LOCK', cargo_lock)
    with pytest.raises(validator.ValidationError, match='Windows release version mismatch'):
        validator.validate_config_versions('0.1.3')
    tauri.write_text(json.dumps({'version': '0.1.3'}), encoding='utf-8')
    cargo.write_text('[package]\nname = "token-place-desktop-tauri"\nversion = "0.1.0"\n', encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='Cargo.toml'):
        validator.validate_config_versions('0.1.3')
    cargo.write_text('[package]\nname = "token-place-desktop-tauri"\nversion = "0.1.3"\n', encoding='utf-8')
    cargo_lock.write_text('version = 4\n\n[[package]]\nname = "token-place-desktop-tauri"\nversion = "0.1.0"\n', encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='Cargo.lock'):
        validator.validate_config_versions('0.1.3')
    cargo_lock.write_text('version = 4\n\n[[package]]\nname = "token-place-desktop-tauri"\nversion = "0.1.3"\n', encoding='utf-8')
    validator.validate_config_versions('0.1.3')

    source_dir = tmp_path / 'already-extracted'
    source_dir.mkdir()
    (source_dir / 'marker.txt').write_text('ok', encoding='utf-8')
    dest = tmp_path / 'dest'
    validator._run_extract(source_dir, dest)
    assert (dest / 'marker.txt').read_text(encoding='utf-8') == 'ok'

    artifact = tmp_path / 'installer.exe'
    artifact.write_bytes(b'fake')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Linux')
    with pytest.raises(validator.ValidationError, match='native Windows installer materialization is unavailable'):
        validator._run_extract(artifact, tmp_path / 'no-native')

    calls = []
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(validator.subprocess, 'run', lambda cmd, **kwargs: calls.append((cmd, kwargs)))
    cleanup = validator._run_extract(artifact, tmp_path / 'nsis-install-root')
    assert calls[0][0][1] == '/S'
    assert calls[0][0][-1].startswith('/D=')
    assert Path(calls[0][0][-1][3:]).is_absolute()
    cleanup()

    calls.clear()
    msi_artifact = tmp_path / 'installer.msi'
    msi_artifact.write_bytes(b'fake')
    validator._run_extract(msi_artifact, tmp_path / 'msi-root')
    assert calls[0][0][0:2] == ['msiexec.exe', '/a']
    assert '/qn' in calls[0][0]
    assert '/norestart' in calls[0][0]
    targetdir = next(arg for arg in calls[0][0] if arg.startswith('TARGETDIR='))
    assert Path(targetdir.split('=', 1)[1]).is_absolute()


def test_windows_validator_native_materialization_contracts_and_cleanup(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return type('Completed', (), {'stdout': ''})()

    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    msi = tmp_path / 'token.place-desktop-0.1.3.msi'
    nsis = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    msi.write_bytes(b'msi')
    nsis.write_bytes(b'nsis')

    msi_cleanup = validator._run_extract(msi, tmp_path / 'first-root')
    nsis_cleanup = validator._run_extract(nsis, tmp_path / 'second-root')
    assert calls[0][0][:3] == ['msiexec.exe', '/a', str(msi.resolve())]
    assert calls[0][0][3:5] == ['/qn', '/norestart']
    assert calls[0][0][-1].startswith('TARGETDIR=')
    assert Path(calls[0][0][-1].split('=', 1)[1]).is_absolute()
    assert calls[1][0] == [str(nsis.resolve()), '/S', f'/D={(tmp_path / "second-root").resolve()}']
    assert calls[1][0][-1].startswith('/D=')
    assert calls[1][0][-1] == f'/D={(tmp_path / "second-root").resolve()}'
    msi_cleanup()
    nsis_cleanup()
    assert len(calls) == 2

    (tmp_path / 'second-root').mkdir(exist_ok=True)
    (tmp_path / 'second-root' / 'uninstall.exe').write_bytes(b'uninstall')
    nsis_cleanup()
    assert calls[-1][0] == [str((tmp_path / 'second-root' / 'uninstall.exe').resolve()), '/S']


def test_windows_validator_native_materialization_failures_are_fail_closed(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    artifact = tmp_path / 'secret-user-path-installer.exe'
    artifact.write_bytes(b'nsis')

    def timeout_run(cmd, **kwargs):
        raise validator.subprocess.TimeoutExpired(cmd, kwargs.get('timeout', 1), output=f'raw {artifact.resolve()} {(tmp_path / "dest").resolve()}')

    monkeypatch.setattr(validator.subprocess, 'run', timeout_run)
    with pytest.raises(validator.ValidationError) as excinfo:
        validator._run_extract(artifact, tmp_path / 'dest')
    message = str(excinfo.value)
    assert 'timed out' in message
    assert str(tmp_path) not in message
    assert artifact.name in message

    def nonzero_run(cmd, **kwargs):
        raise validator.subprocess.CalledProcessError(7, cmd, output=f'failed {artifact.resolve()}')

    monkeypatch.setattr(validator.subprocess, 'run', nonzero_run)
    with pytest.raises(validator.ValidationError, match='exit=7'):
        validator._run_extract(artifact, tmp_path / 'dest2')


def test_windows_validator_generic_archive_shape_without_runtime_cannot_pass(tmp_path):
    validator = _load_windows_release_validator()
    extracted_listing = tmp_path / 'token.place-desktop-0.1.3-seven-zip-listing'
    extracted_listing.mkdir()
    (extracted_listing / '$PLUGINSDIR').mkdir()
    with pytest.raises(validator.ValidationError, match='found 0'):
        validator.validate_artifact(
            extracted_listing,
            '0.1.3',
            'NSIS',
            json.loads(validator.MANIFEST.read_text(encoding='utf-8')),
        )

def test_windows_validator_runtime_and_provenance_fail_closed_edges(tmp_path):
    validator = _load_windows_release_validator()
    manifest = json.loads(validator.MANIFEST.read_text(encoding='utf-8'))

    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(validator.ValidationError, match='found 0'):
        validator._find_runtime(empty)
    for idx in range(2):
        runtime = empty / f'app{idx}' / 'python-runtime'
        runtime.mkdir(parents=True)
        (runtime / 'python.exe').write_bytes(b'MZ')
    with pytest.raises(validator.ValidationError, match='found 2'):
        validator._find_runtime(empty)

    runtime_root = tmp_path / 'runtime' / 'python-runtime'
    runtime_root.mkdir(parents=True)
    with pytest.raises(validator.ValidationError, match='bundled python.exe is missing'):
        validator._validate_runtime_tree(runtime_root, manifest)
    (runtime_root / 'python.exe').write_bytes(b'MZ')
    with pytest.raises(validator.ValidationError, match='missing required DLLs'):
        validator._validate_runtime_tree(runtime_root, manifest)
    for name in manifest['required_native_dlls']:
        (runtime_root / name).write_bytes(b'MZ')
    (runtime_root / 'cmake.exe').write_bytes(b'MZ')
    with pytest.raises(validator.ValidationError, match='forbidden compiler/toolkit'):
        validator._validate_runtime_tree(runtime_root, manifest)
    (runtime_root / 'cmake.exe').unlink()
    with pytest.raises(validator.ValidationError, match='missing or corrupt Windows runtime provenance'):
        validator._validate_runtime_tree(runtime_root, manifest)

    provenance = {
        'runtime_id': 'bundled-cpython-3.11-win-x86_64-cu124',
        'cpython_version': '3.11.13',
        'target_triple': 'x86_64-pc-windows-msvc',
        'source_archive_sha256': manifest['sha256'],
        'llama_cpp_cuda_wheel': manifest['llama_cpp_cuda_wheel'],
        'required_packages': manifest['required_packages'],
        'python_package_wheels': manifest['python_package_wheels'],
        'required_native_dlls': manifest['required_native_dlls'],
        'pe_dll_closure': [],
    }
    prov_path = runtime_root / validator.PROVENANCE
    prov_path.write_text(json.dumps(provenance), encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='non-empty PE DLL closure'):
        validator._validate_provenance(runtime_root, manifest)
    provenance['pe_dll_closure'] = [
        {'name': manifest['required_native_dlls'][0], 'path': manifest['required_native_dlls'][0], 'machine': 'IMAGE_FILE_MACHINE_I386', 'imports': []}
    ]
    prov_path.write_text(json.dumps(provenance), encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='missing required entries'):
        validator._validate_provenance(runtime_root, manifest)
    provenance['pe_dll_closure'] = [
        {'name': name, 'path': name, 'machine': 'IMAGE_FILE_MACHINE_I386', 'imports': []}
        for name in manifest['required_native_dlls']
    ]
    prov_path.write_text(json.dumps(provenance), encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='non-AMD64'):
        validator._validate_provenance(runtime_root, manifest)


def test_prepare_url_and_microsoft_extraction_failure_edges(tmp_path, monkeypatch):
    prep = _load_prepare_module()
    with pytest.raises(prep.RuntimePrepError, match='HTTPS'):
        prep._validated_artifact_url('http://github.com/example/runtime.zip')
    with pytest.raises(prep.RuntimePrepError, match='credentials'):
        prep._validated_artifact_url('https://user:pass@github.com/example/runtime.zip')
    with pytest.raises(prep.RuntimePrepError, match='default HTTPS port'):
        prep._validated_artifact_url('https://github.com:444/example/runtime.zip')
    original = prep._validated_artifact_url('https://github.com/futuroptimist/token.place/releases/download/a/runtime.zip')
    for redirect in [
        'http://release-assets.githubusercontent.com/file',
        'https://token@release-assets.githubusercontent.com/file',
        'https://files.pythonhosted.org/file',
    ]:
        with pytest.raises(prep.RuntimePrepError, match='redirected to an unapproved'):
            prep._validate_redirect_url(original, redirect)

    cabinet = tmp_path / 'payload.cab'
    cabinet.write_bytes(b'MSCFfake')
    monkeypatch.setattr(prep.subprocess, 'run', lambda *args, **kwargs: type('R', (), {'returncode': 1})())
    with pytest.raises(prep.RuntimePrepError, match='member missing or duplicate'):
        prep._expand_cab_member(cabinet, 'msvcp140.dll', tmp_path / 'out')
    def fake_expand(*args, **kwargs):
        out = Path(args[0][-1])
        out.mkdir(parents=True, exist_ok=True)
        (out / 'a' / 'msvcp140.dll').parent.mkdir(parents=True, exist_ok=True)
        (out / 'a' / 'msvcp140.dll').write_bytes(b'one')
        (out / 'b' / 'msvcp140.dll').parent.mkdir(parents=True, exist_ok=True)
        (out / 'b' / 'msvcp140.dll').write_bytes(b'two')
        return type('R', (), {'returncode': 0})()
    monkeypatch.setattr(prep.subprocess, 'run', fake_expand)
    with pytest.raises(prep.RuntimePrepError, match='member missing or duplicate'):
        prep._expand_cab_member(cabinet, 'msvcp140.dll', tmp_path / 'multi')

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Windows')
    with pytest.raises(prep.RuntimePrepError, match="'<cab-key>/<file>'"):
        prep.extract_microsoft_burn_member(tmp_path / 'redist.exe', '../msvcp140.dll', tmp_path / 'dll')
    redist = tmp_path / 'redist.exe'
    redist.write_bytes(b'not-a-cab')
    with pytest.raises(prep.RuntimePrepError, match='missing cabinet payload'):
        prep.extract_microsoft_burn_member(redist, 'a12/msvcp140.dll', tmp_path / 'dll')


def test_prepare_fetch_success_redirect_digest_and_cached_digest_edges(tmp_path, monkeypatch):
    prep = _load_prepare_module()
    payload = b'pinned-runtime-artifact'
    digest = prep.hashlib.sha256(payload).hexdigest()
    dest = tmp_path / 'runtime.zip'

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_exc):
            return False
        def geturl(self):
            return 'https://release-assets.githubusercontent.com/runtime.zip'
        def read(self, size=-1):
            if getattr(self, '_read', False):
                return b''
            self._read = True
            return payload

    calls = []
    monkeypatch.setattr(prep.urllib.request, 'urlopen', lambda url, timeout: calls.append((url, timeout)) or Response())
    assert prep.fetch('https://github.com/futuroptimist/token.place/releases/download/x/runtime.zip', digest, dest) == dest
    assert dest.read_bytes() == payload
    assert calls == [('https://github.com/futuroptimist/token.place/releases/download/x/runtime.zip', 120)]

    # A subsequent cached mismatch is quarantined before failing closed.
    dest.write_bytes(b'corrupt-after-cache')
    good_digest_for_other_payload = prep.hashlib.sha256(b'other-payload').hexdigest()
    with pytest.raises(prep.RuntimePrepError, match='digest mismatch'):
        prep.fetch('https://github.com/futuroptimist/token.place/releases/download/x/runtime.zip', good_digest_for_other_payload, dest)
    assert (tmp_path / 'runtime.zip.poisoned').exists()


def test_prepare_load_manifest_additional_rejection_branches(tmp_path):
    prep = _load_prepare_module()
    manifest = json.loads(Path('desktop-tauri/src-tauri/python/embedded_python_runtime_windows_x86_64_manifest.json').read_text(encoding='utf-8'))

    cases = [
        ('schema_version', 2, 'schema_version'),
        ('expected_architecture', 'ARM64', 'architecture must be AMD64'),
    ]
    for key, value, message in cases:
        bad = json.loads(json.dumps(manifest))
        bad[key] = value
        path = tmp_path / f'{key}.json'
        path.write_text(json.dumps(bad), encoding='utf-8')
        with pytest.raises(prep.RuntimePrepError, match=message):
            prep.load_manifest(path)

    bad = json.loads(json.dumps(manifest))
    bad['llama_cpp_cuda_wheel']['name'] = 'llama_cpp_python-0.3.32-py3-none-win_arm64.whl'
    path = tmp_path / 'bad-wheel-name.json'
    path.write_text(json.dumps(bad), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='unexpected llama-cpp-python wheel name'):
        prep.load_manifest(path)

    bad = json.loads(json.dumps(manifest))
    bad['native_dll_artifacts'][0]['architecture'] = 'I386'
    path = tmp_path / 'bad-native-arch.json'
    path.write_text(json.dumps(bad), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='native DLL artifacts must be AMD64'):
        prep.load_manifest(path)

    bad = json.loads(json.dumps(manifest))
    bad['python_package_wheels'] = 'not-a-list'
    path = tmp_path / 'bad-wheelhouse-shape.json'
    path.write_text(json.dumps(bad), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='python_package_wheels must be a list'):
        prep.load_manifest(path)

def test_windows_validator_normalizes_and_rejects_internal_version_metadata(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    app = tmp_path / 'token.place.exe'
    app.write_bytes(b'MZ-app')
    artifact = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    artifact.write_bytes(b'MZ-nsis')
    monkeypatch.setattr(validator, '_find_tauri_app_exe', lambda _dest: app)
    monkeypatch.setattr(
        validator,
        '_read_pe_version_info',
        lambda path: {'ProductVersion': '0.1.3.0', 'FileVersion': '0.1.3.0'} if path == artifact else {'ProductVersion': '0.1.3.0', 'FileVersion': '0.1.3.0'},
    )
    validator._validate_version_metadata(artifact, tmp_path, '0.1.3', 'NSIS')

    monkeypatch.setattr(
        validator,
        '_read_pe_version_info',
        lambda path: {'ProductVersion': '0.1.1.0', 'FileVersion': '0.1.3.0'},
    )
    with pytest.raises(validator.ValidationError, match='NSIS ProductVersion=0.1.1'):
        validator._validate_version_metadata(artifact, tmp_path, '0.1.3', 'NSIS')


def test_windows_validator_rejects_stale_msi_or_installed_app_version(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    app = tmp_path / 'token.place.exe'
    app.write_bytes(b'MZ-app')
    msi = tmp_path / 'token.place-desktop-0.1.3.msi'
    msi.write_bytes(b'MZ-msi')
    monkeypatch.setattr(validator, '_find_tauri_app_exe', lambda _dest: app)
    monkeypatch.setattr(validator, '_read_msi_product_version', lambda _path: '0.1.1')
    monkeypatch.setattr(validator, '_read_pe_version_info', lambda _path: {'ProductVersion': '0.1.3', 'FileVersion': '0.1.3'})
    with pytest.raises(validator.ValidationError, match='MSI ProductVersion=0.1.1'):
        validator._validate_version_metadata(msi, tmp_path, '0.1.3', 'MSI')

    monkeypatch.setattr(validator, '_read_msi_product_version', lambda _path: '0.1.3')
    monkeypatch.setattr(validator, '_read_pe_version_info', lambda _path: {'ProductVersion': '0.1.1', 'FileVersion': '0.1.3'})
    with pytest.raises(validator.ValidationError, match='app ProductVersion=0.1.1'):
        validator._validate_version_metadata(msi, tmp_path, '0.1.3', 'MSI')

def test_windows_nvidia_smoke_installer_handoff_is_one_shot(tmp_path, monkeypatch):
    import importlib.util
    script_path = Path('desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py')
    spec = importlib.util.spec_from_file_location('windows_nvidia_gpu_smoke_test_unit', script_path)
    smoke = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(smoke)

    installer = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    installer.write_bytes(b'installer')
    calls = {'materialize': 0, 'run': [], 'cleanup': []}
    materialized = tmp_path / 'materialized'
    python_exe = materialized / 'python-runtime' / 'python.exe'
    resource_root = materialized

    def fake_mkdtemp(prefix):
        materialized.mkdir(parents=True, exist_ok=True)
        return str(materialized)

    def fake_materialize(artifact, root):
        calls['materialize'] += 1
        assert artifact == installer
        assert root == materialized
        python_exe.parent.mkdir(parents=True, exist_ok=True)
        python_exe.write_text('python', encoding='utf-8')
        (resource_root / 'python').mkdir(exist_ok=True)
        (resource_root / 'python' / 'compute_node_bridge.py').write_text('', encoding='utf-8')

    def fake_run(cmd, **kwargs):
        calls['run'].append((cmd, kwargs))
        return type('Completed', (), {'returncode': 17})()

    monkeypatch.setattr(smoke.tempfile, 'mkdtemp', fake_mkdtemp)
    monkeypatch.setattr(smoke, '_materialize_release_artifact', fake_materialize)
    monkeypatch.setattr(smoke.subprocess, 'run', fake_run)
    monkeypatch.setattr(smoke.shutil, 'rmtree', lambda path, ignore_errors=False: calls['cleanup'].append((Path(path), ignore_errors)))
    args = smoke.argparse.Namespace(installer=installer, artifact_root=None, python_exe=None, resource_root=None, model='model.gguf', mode='gpu', context_tier='64k-full')

    assert smoke._launch_materialized_child(args) == 17
    assert calls['materialize'] == 1
    assert len(calls['run']) == 1
    cmd = calls['run'][0][0]
    assert cmd[0] == str(python_exe)
    assert '--installer' not in cmd
    assert '--artifact-root' not in cmd
    assert cmd[cmd.index('--python-exe') + 1] == str(python_exe)
    assert cmd[cmd.index('--resource-root') + 1] == str(resource_root)
    assert cmd[cmd.index('--mode') + 1] == 'gpu'
    assert cmd[cmd.index('--context-tier') + 1] == '64k-full'
    assert calls['cleanup'] == [(materialized, True)]


def test_windows_metadata_readers_use_environment_path_and_structured_json(monkeypatch, tmp_path):
    validator = _load_windows_release_validator()
    artifact = tmp_path / 'TokenPlace.Setup.exe'
    artifact.write_bytes(b'MZ')
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert '$args[0]' not in cmd[3]
        assert kwargs['env']['TOKEN_PLACE_ARTIFACT_PATH'] == str(artifact)
        assert str(artifact) not in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({'ProductVersion': '0.1.3.0', 'FileVersion': '0.1.3.0'}), stderr='')

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)
    assert validator._read_pe_version_info(artifact) == {'ProductVersion': '0.1.3.0', 'FileVersion': '0.1.3.0'}

    def fake_msi_run(cmd, **kwargs):
        assert '$args[0]' not in cmd[3]
        assert kwargs['env']['TOKEN_PLACE_ARTIFACT_PATH'] == str(artifact)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({'ProductVersion': '0.1.3.0'}), stderr='')

    monkeypatch.setattr(validator.subprocess, 'run', fake_msi_run)
    assert validator._read_msi_product_version(artifact) == '0.1.3.0'


def test_windows_metadata_readers_fail_closed_on_empty_or_malformed(monkeypatch, tmp_path):
    validator = _load_windows_release_validator()
    artifact = tmp_path / 'TokenPlace.Setup.exe'
    artifact.write_bytes(b'MZ')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(validator.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout='', stderr=''))
    with pytest.raises(validator.ValidationError, match='empty metadata'):
        validator._read_pe_version_info(artifact)
    monkeypatch.setattr(validator.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout='not-json', stderr=''))
    with pytest.raises(validator.ValidationError, match='malformed metadata'):
        validator._read_msi_product_version(artifact)


def test_windows_version_normalization_is_strict():
    validator = _load_windows_release_validator()
    assert validator._normalize_windows_version('0.1.3') == '0.1.3'
    assert validator._normalize_windows_version('0.1.3.0') == '0.1.3'
    for value in ['0.1', '0.1.3.1', '0.1.3.0.0', '0.1.x']:
        with pytest.raises(validator.ValidationError):
            validator._normalize_windows_version(value)


def test_find_tauri_app_exe_uses_expected_name_and_rejects_ambiguous(monkeypatch, tmp_path):
    validator = _load_windows_release_validator()
    monkeypatch.setattr(validator, '_expected_tauri_binary_name', lambda: 'token-place-desktop-tauri.exe')
    root = tmp_path / 'payload'
    (root / 'helpers').mkdir(parents=True)
    (root / 'helpers' / 'helper.exe').write_bytes(b'MZ')
    expected = root / 'Token-Place-Desktop-Tauri.EXE'
    expected.write_bytes(b'MZ')
    assert validator._find_tauri_app_exe(root) == expected
    with pytest.raises(validator.ValidationError, match='found 0'):
        validator._find_tauri_app_exe(tmp_path / 'missing')
    dup = root / 'nested'
    dup.mkdir()
    (dup / 'token-place-desktop-tauri.exe').write_bytes(b'MZ')
    with pytest.raises(validator.ValidationError, match='found 2'):
        validator._find_tauri_app_exe(root)


def test_validate_version_metadata_requires_app_versions_on_windows(monkeypatch, tmp_path):
    validator = _load_windows_release_validator()
    artifact = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    artifact.write_bytes(b'MZ')
    app = tmp_path / 'token-place-desktop-tauri.exe'
    app.write_bytes(b'MZ')
    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(validator, '_find_tauri_app_exe', lambda _dest: app)
    monkeypatch.setattr(validator, '_read_pe_version_info', lambda path: {'ProductVersion': '', 'FileVersion': ''} if path == app else {'ProductVersion': '0.1.3.0', 'FileVersion': '0.1.3.0'})
    with pytest.raises(validator.ValidationError, match='app ProductVersion=missing'):
        validator._validate_version_metadata(artifact, tmp_path, '0.1.3', 'NSIS')


def test_windows_nvidia_smoke_runs_nsis_uninstaller_after_success_and_failure(tmp_path, monkeypatch):
    import importlib.util
    script_path = Path('desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py')
    spec = importlib.util.spec_from_file_location('windows_nvidia_gpu_smoke_test_unit_cleanup', script_path)
    smoke = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(smoke)

    installer = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    installer.write_bytes(b'installer')

    for child_code in (0, 23):
        root = tmp_path / f'install-{child_code}'
        python_exe = root / 'python-runtime' / 'python.exe'
        uninstaller = root / 'Uninstall token.place.exe'
        calls = []
        monkeypatch.setattr(smoke.tempfile, 'mkdtemp', lambda prefix, root=root: str(root))

        def fake_materialize(_artifact, materialized_root, python_exe=python_exe, uninstaller=uninstaller):
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_text('python', encoding='utf-8')
            (materialized_root / 'python').mkdir(exist_ok=True)
            (materialized_root / 'python' / 'compute_node_bridge.py').write_text('', encoding='utf-8')
            uninstaller.write_text('uninstall', encoding='utf-8')

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, child_code if len(calls) == 1 else 0, stdout='')

        monkeypatch.setattr(smoke, '_materialize_release_artifact', fake_materialize)
        monkeypatch.setattr(smoke.subprocess, 'run', fake_run)
        monkeypatch.setattr(smoke.shutil, 'rmtree', lambda *a, **k: None)
        args = smoke.argparse.Namespace(installer=installer, artifact_root=None, python_exe=None, resource_root=None, model='model.gguf', mode='gpu', context_tier='64k-full')
        assert smoke._launch_materialized_child(args) == child_code
        assert len(calls) == 2
        assert calls[1] == [str(uninstaller), '/S']


def test_windows_nvidia_smoke_cleanup_failure_overrides_child_exit(tmp_path, monkeypatch):
    import importlib.util
    script_path = Path('desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py')
    spec = importlib.util.spec_from_file_location('windows_nvidia_gpu_smoke_test_unit_cleanup_fail', script_path)
    smoke = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(smoke)

    root = tmp_path / 'install'
    installer = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    installer.write_bytes(b'installer')
    monkeypatch.setattr(smoke.tempfile, 'mkdtemp', lambda prefix: str(root))

    def fake_materialize(_artifact, materialized_root):
        python_exe = materialized_root / 'python-runtime' / 'python.exe'
        python_exe.parent.mkdir(parents=True, exist_ok=True)
        python_exe.write_text('python', encoding='utf-8')
        (materialized_root / 'python').mkdir(exist_ok=True)
        (materialized_root / 'python' / 'compute_node_bridge.py').write_text('', encoding='utf-8')
        (materialized_root / 'unins000.exe').write_text('uninstall', encoding='utf-8')

    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 2:
            raise subprocess.CalledProcessError(7, cmd, output='cleanup failed')
        return subprocess.CompletedProcess(cmd, 0, stdout='')

    monkeypatch.setattr(smoke, '_materialize_release_artifact', fake_materialize)
    monkeypatch.setattr(smoke.subprocess, 'run', fake_run)
    monkeypatch.setattr(smoke.shutil, 'rmtree', lambda *a, **k: None)
    args = smoke.argparse.Namespace(installer=installer, artifact_root=None, python_exe=None, resource_root=None, model='model.gguf', mode='gpu', context_tier='64k-full')
    with pytest.raises(subprocess.CalledProcessError):
        smoke._launch_materialized_child(args)
    assert len(calls) == 2


def test_windows_validator_metadata_and_binary_name_failure_edges(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    artifact = tmp_path / 'token.place-desktop-0.1.3-setup.exe'
    artifact.write_bytes(b'MZ')

    def run_with(returncode=0, stdout='{}'):
        def fake_run(cmd, **kwargs):
            assert kwargs['env']['TOKEN_PLACE_ARTIFACT_PATH'] == str(artifact)
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr='secret path C:/Users/Secret')
        return fake_run

    monkeypatch.setattr(validator.subprocess, 'run', run_with(returncode=9, stdout='{}'))
    with pytest.raises(validator.ValidationError, match=artifact.name):
        validator._powershell_json('ConvertTo-Json @{}', artifact, description='metadata')

    monkeypatch.setattr(validator.subprocess, 'run', run_with(stdout=''))
    with pytest.raises(validator.ValidationError, match='empty metadata'):
        validator._powershell_json('ConvertTo-Json @{}', artifact, description='metadata')

    monkeypatch.setattr(validator.subprocess, 'run', run_with(stdout='[]'))
    with pytest.raises(validator.ValidationError, match='malformed metadata'):
        validator._powershell_json('ConvertTo-Json @{}', artifact, description='metadata')

    monkeypatch.setattr(validator.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(validator.subprocess, 'run', run_with(stdout=json.dumps({'ProductVersion': '', 'FileVersion': '0.1.3'})))
    with pytest.raises(validator.ValidationError, match='incomplete metadata'):
        validator._read_pe_version_info(artifact)

    monkeypatch.setattr(validator.subprocess, 'run', run_with(stdout=json.dumps({'ProductVersion': ''})))
    with pytest.raises(validator.ValidationError, match='incomplete metadata'):
        validator._read_msi_product_version(artifact)

    tauri = tmp_path / 'tauri.conf.json'
    cargo = tmp_path / 'Cargo.toml'
    tauri.write_text(json.dumps({'bundle': {'windows': {'mainBinaryName': ''}}}), encoding='utf-8')
    cargo.write_text('[package]\nname = ""\nversion = "0.1.3"\n', encoding='utf-8')
    monkeypatch.setattr(validator, 'TAURI_CONFIG', tauri)
    monkeypatch.setattr(validator, 'CARGO_MANIFEST', cargo)
    with pytest.raises(validator.ValidationError, match='unable to determine'):
        validator._expected_tauri_binary_name()


def _load_publish_step(name: str) -> dict:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
    steps = workflow['jobs']['publish']['steps']
    return next(step for step in steps if step.get('name') == name)


def _load_publish_manifest_provenance_source() -> str:
    script = _load_publish_step('Verify immutable tag, release absence, and artifact provenance')['run']
    marker = "python - <<'PY'\n"
    start = script.index(marker) + len(marker)
    end = script.index("\nPY", start)
    return script[start:end]


def test_publish_release_creation_is_create_only_with_no_overwrite_path() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'softprops/action-gh-release' not in text
    assert 'overwrite_files' not in text
    assert '--clobber' not in text
    assert 'gh release view' not in text
    assert '|| true' not in text
    assert 'gh api --method POST "/repos/${GITHUB_REPOSITORY}/releases" \\' in text
    assert '-f "tag_name=${TAG_NAME}"' in text
    assert 'No release ID is ever looked up.' in text
    assert "grep -qi 'already_exists' /tmp/release-create.log" in text
    assert 'Release ${TAG_NAME} already exists; desktop releases are immutable.' in text


def test_publish_release_lookup_distinguishes_absence_from_api_failure() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert "grep -qi '404' /tmp/release-lookup.log" in text
    assert 'Unable to confirm release ${TAG_NAME} is absent (gh api did not report HTTP 404).' in text


def test_publish_uploads_assets_via_captured_upload_url_without_clobber() -> None:
    create_step = _load_publish_step('Create immutable GitHub Release')
    script = create_step['run']
    assert 'upload_url="$(jq -r \'.upload_url\' /tmp/release-create.json)"' in script
    assert 'release_id="$(jq -r \'.id\' /tmp/release-create.json)"' in script
    assert 'GitHub did not return a release id for ${TAG_NAME}.' in script
    assert 'GitHub did not return an upload URL for release ${TAG_NAME}.' in script
    assert 'Content-Type: application/octet-stream' in script
    assert 'No release assets were uploaded for ${TAG_NAME}.' in script
    assert '--clobber' not in script


def test_publish_manifest_provenance_requires_exact_two_target_manifests_in_source() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert "'release-assets/macos/token.place-desktop-aarch64-apple-darwin-build-manifest.json': {" in text
    assert "'release-assets/windows/token.place-desktop-x86_64-pc-windows-msvc-build-manifest.json': {" in text
    assert "'target_triple': 'aarch64-apple-darwin'," in text
    assert "'bundled_runtime_id': 'bundled-cpython-3.11-macos-arm64'," in text
    assert "'target_triple': 'x86_64-pc-windows-msvc'," in text
    assert "'bundled_runtime_id': 'bundled-cpython-3.11-win-x86_64-cu124'," in text
    assert 'expected exactly manifests' in text
    assert 'target_triple mismatch' in text
    assert 'bundled_runtime_id mismatch' in text


def _write_manifest_and_asset(directory, manifest_filename, asset_filename, *, tag, commit, target_triple, runtime_id):
    import hashlib

    asset_path = directory / asset_filename
    asset_path.write_bytes(f'contents-of-{asset_filename}'.encode('utf-8'))
    manifest = {
        'public_version': tag.removeprefix('desktop-v'),
        'tag': tag,
        'tagged_commit': commit,
        'target_triple': target_triple,
        'bundled_runtime_id': runtime_id,
        'artifacts': [
            {
                'filename': asset_filename,
                'sha256': hashlib.sha256(asset_path.read_bytes()).hexdigest(),
            }
        ],
    }
    (directory / manifest_filename).write_text(json.dumps(manifest), encoding='utf-8')


def _run_publish_manifest_provenance_checker(tmp_path, monkeypatch, tag='desktop-v0.1.3', commit='a' * 40):
    source = _load_publish_manifest_provenance_source()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TAG_NAME', tag)
    monkeypatch.setenv('TOKENPLACE_TAG_COMMIT', commit)
    namespace = {'__name__': '__publish_manifest_provenance__'}
    exec(compile(source, '<publish-manifest-provenance>', 'exec'), namespace)


def test_publish_manifest_provenance_rejects_missing_manifests(tmp_path, monkeypatch) -> None:
    (tmp_path / 'release-assets' / 'macos').mkdir(parents=True)
    (tmp_path / 'release-assets' / 'windows').mkdir(parents=True)

    with pytest.raises(SystemExit, match='expected exactly manifests'):
        _run_publish_manifest_provenance_checker(tmp_path, monkeypatch)


def test_publish_manifest_provenance_rejects_additional_target_manifest(tmp_path, monkeypatch) -> None:
    tag = 'desktop-v0.1.3'
    commit = 'a' * 40
    macos_dir = tmp_path / 'release-assets' / 'macos'
    windows_dir = tmp_path / 'release-assets' / 'windows'
    linux_dir = tmp_path / 'release-assets' / 'linux'
    macos_dir.mkdir(parents=True)
    windows_dir.mkdir(parents=True)
    linux_dir.mkdir(parents=True)

    _write_manifest_and_asset(
        macos_dir,
        'token.place-desktop-aarch64-apple-darwin-build-manifest.json',
        'token.place-desktop-0.1.3-apple-silicon.dmg',
        tag=tag, commit=commit,
        target_triple='aarch64-apple-darwin', runtime_id='bundled-cpython-3.11-macos-arm64',
    )
    _write_manifest_and_asset(
        windows_dir,
        'token.place-desktop-x86_64-pc-windows-msvc-build-manifest.json',
        'token.place-desktop-0.1.3-x86_64-pc-windows-msvc-setup.exe',
        tag=tag, commit=commit,
        target_triple='x86_64-pc-windows-msvc', runtime_id='bundled-cpython-3.11-win-x86_64-cu124',
    )
    _write_manifest_and_asset(
        linux_dir,
        'token.place-desktop-x86_64-unknown-linux-gnu-build-manifest.json',
        'token.place-desktop-0.1.3-x86_64.AppImage',
        tag=tag, commit=commit,
        target_triple='x86_64-unknown-linux-gnu', runtime_id='bundled-cpython-3.11-linux-x86_64',
    )

    with pytest.raises(SystemExit, match='expected exactly manifests'):
        _run_publish_manifest_provenance_checker(tmp_path, monkeypatch, tag=tag, commit=commit)


def test_publish_manifest_provenance_rejects_wrong_target_triple_mapping(tmp_path, monkeypatch) -> None:
    tag = 'desktop-v0.1.3'
    commit = 'b' * 40
    macos_dir = tmp_path / 'release-assets' / 'macos'
    windows_dir = tmp_path / 'release-assets' / 'windows'
    macos_dir.mkdir(parents=True)
    windows_dir.mkdir(parents=True)

    _write_manifest_and_asset(
        macos_dir,
        'token.place-desktop-aarch64-apple-darwin-build-manifest.json',
        'token.place-desktop-0.1.3-apple-silicon.dmg',
        tag=tag, commit=commit,
        target_triple='x86_64-pc-windows-msvc',  # wrong: macOS manifest claims the Windows target
        runtime_id='bundled-cpython-3.11-macos-arm64',
    )
    _write_manifest_and_asset(
        windows_dir,
        'token.place-desktop-x86_64-pc-windows-msvc-build-manifest.json',
        'token.place-desktop-0.1.3-x86_64-pc-windows-msvc-setup.exe',
        tag=tag, commit=commit,
        target_triple='x86_64-pc-windows-msvc', runtime_id='bundled-cpython-3.11-win-x86_64-cu124',
    )

    with pytest.raises(SystemExit, match='target_triple mismatch'):
        _run_publish_manifest_provenance_checker(tmp_path, monkeypatch, tag=tag, commit=commit)


def test_publish_manifest_provenance_rejects_wrong_bundled_runtime_id(tmp_path, monkeypatch) -> None:
    tag = 'desktop-v0.1.3'
    commit = 'c' * 40
    macos_dir = tmp_path / 'release-assets' / 'macos'
    windows_dir = tmp_path / 'release-assets' / 'windows'
    macos_dir.mkdir(parents=True)
    windows_dir.mkdir(parents=True)

    _write_manifest_and_asset(
        macos_dir,
        'token.place-desktop-aarch64-apple-darwin-build-manifest.json',
        'token.place-desktop-0.1.3-apple-silicon.dmg',
        tag=tag, commit=commit,
        target_triple='aarch64-apple-darwin',
        runtime_id='bundled-cpython-3.11-win-x86_64-cu124',  # wrong: macOS manifest claims the Windows runtime id
    )
    _write_manifest_and_asset(
        windows_dir,
        'token.place-desktop-x86_64-pc-windows-msvc-build-manifest.json',
        'token.place-desktop-0.1.3-x86_64-pc-windows-msvc-setup.exe',
        tag=tag, commit=commit,
        target_triple='x86_64-pc-windows-msvc', runtime_id='bundled-cpython-3.11-win-x86_64-cu124',
    )

    with pytest.raises(SystemExit, match='bundled_runtime_id mismatch'):
        _run_publish_manifest_provenance_checker(tmp_path, monkeypatch, tag=tag, commit=commit)


def test_publish_manifest_provenance_accepts_exact_expected_manifest_pair(tmp_path, monkeypatch) -> None:
    tag = 'desktop-v0.1.3'
    commit = 'd' * 40
    macos_dir = tmp_path / 'release-assets' / 'macos'
    windows_dir = tmp_path / 'release-assets' / 'windows'
    macos_dir.mkdir(parents=True)
    windows_dir.mkdir(parents=True)

    _write_manifest_and_asset(
        macos_dir,
        'token.place-desktop-aarch64-apple-darwin-build-manifest.json',
        'token.place-desktop-0.1.3-apple-silicon.dmg',
        tag=tag, commit=commit,
        target_triple='aarch64-apple-darwin', runtime_id='bundled-cpython-3.11-macos-arm64',
    )
    _write_manifest_and_asset(
        windows_dir,
        'token.place-desktop-x86_64-pc-windows-msvc-build-manifest.json',
        'token.place-desktop-0.1.3-x86_64-pc-windows-msvc-setup.exe',
        tag=tag, commit=commit,
        target_triple='x86_64-pc-windows-msvc', runtime_id='bundled-cpython-3.11-win-x86_64-cu124',
    )

    _run_publish_manifest_provenance_checker(tmp_path, monkeypatch, tag=tag, commit=commit)


def _load_windows_installer_identity():
    import importlib.util

    script_path = Path('desktop-tauri/scripts/test_windows_installer_identity.py')
    spec = importlib.util.spec_from_file_location('test_windows_installer_identity', script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_installer_identity_requires_previous_artifacts(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current_nsis = tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe'
    current_msi = tmp_path / 'token.place-desktop-0.1.3-x64.msi'
    previous_nsis = tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe'
    previous_msi = tmp_path / 'token.place-desktop-0.1.2-x64.msi'
    for path in (current_nsis, current_msi, previous_nsis, previous_msi):
        path.write_text('artifact', encoding='utf-8')

    scenarios = guard.build_scenarios(current_nsis, current_msi, previous_nsis, previous_msi, '0.1.3', '0.1.2')

    assert [scenario.name for scenario in scenarios] == [
        'clean-nsis-0.1.3',
        'clean-msi-0.1.3',
        'upgrade-nsis-to-nsis',
        'upgrade-msi-to-msi',
        'cross-nsis-to-msi',
        'cross-msi-to-nsis',
    ]
    assert scenarios[2].previous.kind == 'nsis'
    assert scenarios[3].previous.kind == 'msi'
    with pytest.raises(guard.InstallerIdentityError, match='filename must include 0.1.2'):
        guard.validate_previous_artifacts(previous_nsis, current_msi, '0.1.2')


def test_windows_installer_identity_main_requires_exact_build_id(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    artifacts = {
        'current_nsis': tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe',
        'current_msi': tmp_path / 'token.place-desktop-0.1.3-x64.msi',
        'previous_nsis': tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe',
        'previous_msi': tmp_path / 'token.place-desktop-0.1.2-x64.msi',
    }
    for path in artifacts.values():
        path.write_text('artifact', encoding='utf-8')

    argv = [
        'prog',
        '--windows-nsis', str(artifacts['current_nsis']),
        '--windows-msi', str(artifacts['current_msi']),
        '--previous-windows-nsis', str(artifacts['previous_nsis']),
        '--previous-windows-msi', str(artifacts['previous_msi']),
        '--expected-build-id', 'too-short',
    ]
    old_argv = sys.argv
    try:
        sys.argv = argv
        with pytest.raises(guard.InstallerIdentityError, match='12-character'):
            guard.main()
    finally:
        sys.argv = old_argv


def test_windows_installer_identity_shortcut_authority_rejects_duplicates_and_stale(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()

    def completed(payload):
        return subprocess.CompletedProcess(['powershell'], 0, json.dumps(payload), '')

    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    stale_target = tmp_path / 'token.place-0.1.2.exe'
    stale_target.write_text('exe', encoding='utf-8')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: completed({'Shortcut': str(tmp_path / 'app.lnk'), 'Target': str(stale_target)}))
    with pytest.raises(guard.InstallerIdentityError, match='stale'):
        guard.resolve_authoritative_shortcut()

    current_target = tmp_path / 'token.place.exe'
    current_target.write_text('exe', encoding='utf-8')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: completed([
        {'Shortcut': str(tmp_path / 'a.lnk'), 'Target': str(current_target)},
        {'Shortcut': str(tmp_path / 'b.lnk'), 'Target': str(current_target)},
    ]))
    with pytest.raises(guard.InstallerIdentityError, match='expected one authoritative'):
        guard.resolve_authoritative_shortcut()


def test_windows_installer_identity_configuration_preservation(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    config = tmp_path / guard.CONFIG_NAME
    expected = {'relay_url': 'https://relay.invalid', 'model': 'qwen3', 'context_tier': '64k-full', 'n_ctx': 65536}
    config.write_text(json.dumps(expected), encoding='utf-8')
    guard.verify_config_preserved(config, expected)
    config.write_text(json.dumps({**expected, 'n_ctx': 8192}), encoding='utf-8')
    with pytest.raises(guard.InstallerIdentityError, match='not preserved'):
        guard.verify_config_preserved(config, expected)


def test_windows_installer_identity_sentinel_failure_detection(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    sentinel_dir = guard._sentinel_dir(tmp_path)
    names = {path.stem for path in sentinel_dir.glob('*.cmd')}
    assert set(guard.SENTINELS) == names
    log = tmp_path / 'sentinel.log'
    env = guard._safe_env(sentinel_dir, log)
    assert env['PATH'] == str(sentinel_dir)
    assert env['TOKENPLACE_SENTINEL_LOG'] == str(log)
    assert 'SystemRoot' in env or sys.platform != 'win32'


def test_windows_installer_identity_cross_installer_fail_closed(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64.msi', 'msi', '0.1.3')
    previous = guard.Installer(tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe', 'nsis', '0.1.2')
    scenario = guard.Scenario('cross-nsis-to-msi', current, previous)
    for path in (current.path, previous.path):
        path.write_text('artifact', encoding='utf-8')
    installs = []

    def fake_install(installer):
        installs.append(installer.kind)
        return subprocess.CompletedProcess([str(installer.path)], 1603 if installer.kind == 'msi' else 0, 'remove the competing installation first', '')

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, '_terminate_processes', lambda: None)
    monkeypatch.setattr(guard, 'uninstall_best_effort', lambda: None)
    monkeypatch.setattr(guard, 'install', fake_install)
    monkeypatch.setattr(guard, '_sentinel_dir', lambda root: root / 'sentinel')
    monkeypatch.setattr(guard, '_safe_env', lambda sentinel, log: {'PATH': str(sentinel), 'TOKENPLACE_SENTINEL_LOG': str(log)})

    guard.run_scenario(scenario, 'abcdef123456')
    assert installs == ['nsis', 'msi']
