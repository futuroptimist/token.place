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


def _write_windows_runtime_fixture(root: Path, *, version: str = '0.1.5') -> tuple[Path, Path]:
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
    assert validator.main(['--windows-nsis', str(nsis), '--windows-msi', str(msi), '--expected-version', '0.1.5']) == 0


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
        validator.main(['--windows-nsis', str(nsis), '--windows-msi', str(msi), '--expected-version', '0.1.5'])


def _extract_workflow_job_block(text: str, job_key: str) -> str:
    """Return the text from ``job_key:`` up to (but not including) the next sibling job."""
    import re

    start = text.index(job_key + ':')
    rest = text[start:]
    # Next top-level job starts with a newline, two spaces, a word-character, then a colon.
    m = re.search(r'\n  [a-z][a-z0-9_-]*:', rest[1:])
    return rest[: m.start() + 1] if m else rest


def test_release_workflow_runs_windows_validator_and_preserves_skipped_nvidia_gate() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    # Windows MSI/NSIS artifact validation remains required.
    assert 'Validate Windows MSI and NSIS artifact contents' in text
    assert 'validate_windows_desktop_release_artifacts.py' in text
    assert 'windows-nvidia-release-gate' in text
    assert 'needs: build' in text
    assert 'Intentionally bypass the hardware smoke gate while no matching self-hosted runner exists.' in text
    assert 'windows_nvidia_gpu_smoke_test.py --artifact-root release-assets/windows' in text
    # windows-nvidia-release-gate job is still present but explicitly disabled until
    # a matching self-hosted runner is available.
    assert 'windows-nvidia-release-gate' in text
    # Isolate the gate job block and assert the disabled condition.
    gate_block = _extract_workflow_job_block(text, 'windows-nvidia-release-gate')
    assert 'if: ${{ false }}' in gate_block
    # Durable comment explaining the gate must be restored when hardware is ready.
    assert 'windows-nvidia-release-gate back to publish.needs' in text
    # Isolate the publish job block; publish must depend on build only (not the disabled gate).
    publish_block = _extract_workflow_job_block(text, 'publish')
    assert 'needs: build' in publish_block
    assert 'windows-nvidia-release-gate' not in publish_block.split('needs:')[1].split('\n')[0]


def test_windows_validator_version_tag_config_and_extract_edges(tmp_path, monkeypatch):
    validator = _load_windows_release_validator()
    assert validator.expected_version_from_tag(None, '0.1.5') == '0.1.5'
    assert validator.expected_version_from_tag('desktop-v1.2.3', '0.1.5') == '1.2.3'
    with pytest.raises(validator.ValidationError, match='desktop-vX.Y.Z'):
        validator.expected_version_from_tag('1495/merge', '0.1.5')

    package = tmp_path / 'package.json'
    lock = tmp_path / 'package-lock.json'
    tauri = tmp_path / 'tauri.conf.json'
    cargo = tmp_path / 'Cargo.toml'
    cargo_lock = tmp_path / 'Cargo.lock'
    package.write_text(json.dumps({'version': '0.1.5'}), encoding='utf-8')
    lock.write_text(json.dumps({'version': '0.1.5'}), encoding='utf-8')
    tauri.write_text(json.dumps({'version': '9.9.9'}), encoding='utf-8')
    cargo.write_text('[package]\nname = "token-place-desktop-tauri"\nversion = "0.1.3"\n', encoding='utf-8')
    cargo_lock.write_text('version = 4\n\n[[package]]\nname = "token-place-desktop-tauri"\nversion = "0.1.3"\n', encoding='utf-8')
    monkeypatch.setattr(validator, 'PACKAGE_JSON', package)
    monkeypatch.setattr(validator, 'PACKAGE_LOCK', lock)
    monkeypatch.setattr(validator, 'TAURI_CONFIG', tauri)
    monkeypatch.setattr(validator, 'CARGO_MANIFEST', cargo)
    monkeypatch.setattr(validator, 'CARGO_LOCK', cargo_lock)
    with pytest.raises(validator.ValidationError, match='Windows release version mismatch'):
        validator.validate_config_versions('0.1.5')
    tauri.write_text(json.dumps({'version': '0.1.5'}), encoding='utf-8')
    cargo.write_text('[package]\nname = "token-place-desktop-tauri"\nversion = "0.1.0"\n', encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='Cargo.toml'):
        validator.validate_config_versions('0.1.5')
    cargo.write_text('[package]\nname = "token-place-desktop-tauri"\nversion = "0.1.5"\n', encoding='utf-8')
    cargo_lock.write_text('version = 4\n\n[[package]]\nname = "token-place-desktop-tauri"\nversion = "0.1.0"\n', encoding='utf-8')
    with pytest.raises(validator.ValidationError, match='Cargo.lock'):
        validator.validate_config_versions('0.1.5')
    cargo_lock.write_text('version = 4\n\n[[package]]\nname = "token-place-desktop-tauri"\nversion = "0.1.5"\n', encoding='utf-8')
    validator.validate_config_versions('0.1.5')

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
    marker = "import hashlib, json, os, pathlib, re, sys\n"
    start = script.index(marker)
    end = script.index("\nPY", start)
    return script[start:end]


def test_publish_job_checks_out_immutable_tag_before_downloading_artifacts() -> None:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
    steps = workflow['jobs']['publish']['steps']
    names = [step.get('name') for step in steps]

    resolve_index = names.index('Resolve release tag')
    checkout_index = names.index('Check out repository for immutable tag verification')
    macos_index = names.index('Download macOS artifacts')
    windows_index = names.index('Download Windows artifacts')
    verify_index = names.index('Verify immutable tag, release absence, and artifact provenance')
    create_index = names.index('Create immutable GitHub Release')

    assert checkout_index == resolve_index + 1
    assert checkout_index < macos_index < windows_index < verify_index < create_index

    checkout_step = steps[checkout_index]
    assert checkout_step['uses'] == 'actions/checkout@v5'
    assert checkout_step['with']['ref'] == '${{ steps.tag.outputs.tag }}'
    assert checkout_step['with']['fetch-depth'] == 0
    assert 'clean' not in checkout_step.get('with', {})

    macos_step = steps[macos_index]
    assert macos_step['uses'] == 'actions/download-artifact@v5'
    assert macos_step['with'] == {
        'name': 'macos-arm64-bundles',
        'path': 'release-assets/macos',
    }

    windows_step = steps[windows_index]
    assert windows_step['uses'] == 'actions/download-artifact@v5'
    assert windows_step['with'] == {
        'name': 'windows-x64-bundles',
        'path': 'release-assets/windows',
    }


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
    assert 'created_release_id="$(jq -r \'.id\' /tmp/release-create.json)"' in script
    assert 'GitHub did not return a release id for ${TAG_NAME}.' in script
    assert 'GitHub did not return an upload URL for release ${TAG_NAME}.' in script
    assert "encoded_asset_name=\"$(jq -rn --arg value \"${asset_name}\" '$value | @uri')\"" in script
    assert '"${upload_url}?name=${encoded_asset_name}"' in script
    assert '"${upload_url}?name=${asset_name}"' not in script
    assert 'Content-Type: application/octet-stream' in script
    assert 'No release assets were uploaded for ${TAG_NAME}.' in script
    assert '--clobber' not in script


def test_publish_upload_url_encodes_notice_asset_names() -> None:
    create_step = _load_publish_step('Create immutable GitHub Release')
    script = create_step['run']
    assert 'README BEFORE OPENING.txt' in WORKFLOW.read_text(encoding='utf-8')
    assert "'$value | @uri'" in script
    encoded = subprocess.run(
        ['jq', '-rn', '--arg', 'value', 'README BEFORE OPENING.txt', '$value | @uri'],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    assert encoded == 'README%20BEFORE%20OPENING.txt'
    assert '"${upload_url}?name=${encoded_asset_name}"' in script
    assert '"${upload_url}?name=${asset_name}"' not in script


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
    current_nsis = tmp_path / 'token.place-desktop-0.1.5-x64-setup.exe'
    current_msi = tmp_path / 'token.place-desktop-0.1.5-x64.msi'
    previous_nsis = tmp_path / 'token.place-desktop-0.1.4-x64-setup.exe'
    previous_msi = tmp_path / 'token.place-desktop-0.1.4-x64.msi'
    for path in (current_nsis, current_msi, previous_nsis, previous_msi):
        path.write_text('artifact', encoding='utf-8')

    scenarios = guard.build_scenarios(current_nsis, current_msi, previous_nsis, previous_msi, '0.1.5', '0.1.4')

    assert [scenario.name for scenario in scenarios] == [
        'clean-nsis-0.1.5',
        'clean-msi-0.1.5',
        'upgrade-nsis-to-nsis',
        'upgrade-msi-to-msi',
        'cross-nsis-to-msi',
        'cross-msi-to-nsis',
    ]
    assert scenarios[2].previous.kind == 'nsis'
    assert scenarios[3].previous.kind == 'msi'
    with pytest.raises(guard.InstallerIdentityError, match='filename must include 0.1.2'):
        guard.validate_previous_artifacts(previous_nsis, current_msi, '0.1.2')


def test_windows_installer_identity_rejects_duplicate_previous_artifact(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    previous_nsis = tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe'
    previous_nsis.write_text('artifact', encoding='utf-8')

    with pytest.raises(guard.InstallerIdentityError, match='exactly one previous NSIS and one distinct previous MSI'):
        guard.validate_previous_artifacts(previous_nsis, previous_nsis, '0.1.3')


def test_immediate_prior_version_is_semver_aware() -> None:
    guard = _load_windows_installer_identity()
    assert guard.immediate_prior_version('0.1.3') == '0.1.2'
    assert guard.immediate_prior_version('0.1.5') == '0.1.4'
    assert guard.immediate_prior_version('1.0.1') == '1.0.0'
    with pytest.raises(guard.InstallerIdentityError, match='no immediate prior patch release'):
        guard.immediate_prior_version('1.0.0')
    with pytest.raises(guard.InstallerIdentityError, match='expected a semantic version'):
        guard.immediate_prior_version('not-a-version')


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
        guard.resolve_authoritative_shortcut('0.1.2')

    current_target = tmp_path / 'token.place.exe'
    current_target.write_text('exe', encoding='utf-8')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: completed([
        {'Shortcut': str(tmp_path / 'a.lnk'), 'Target': str(current_target)},
        {'Shortcut': str(tmp_path / 'b.lnk'), 'Target': str(current_target)},
    ]))
    assert guard.resolve_authoritative_shortcut().target == current_target.resolve()

    other_target = tmp_path / 'token.place-other.exe'
    other_target.write_text('exe', encoding='utf-8')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: completed([
        {'Shortcut': str(tmp_path / 'a.lnk'), 'Target': str(current_target)},
        {'Shortcut': str(tmp_path / 'b.lnk'), 'Target': str(other_target)},
    ]))
    with pytest.raises(guard.InstallerIdentityError, match='distinct authoritative executable target'):
        guard.resolve_authoritative_shortcut()


def test_windows_installer_identity_configuration_preservation(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    config = tmp_path / guard.CONFIG_NAME
    expected = guard.seeded_config_values()
    config.write_text(json.dumps(expected), encoding='utf-8')
    guard.verify_config_preserved(config, expected)
    config.write_text(json.dumps({**expected, 'context_tier': '8k-fast'}), encoding='utf-8')
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


def _empty_snapshot(guard):
    return guard.AuthoritySnapshot(shortcuts=guard.ShortcutInventory([], [], []), registry=[])


def test_windows_installer_identity_cross_installer_fail_closed(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64.msi', 'msi', '0.1.3')
    previous = guard.Installer(tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe', 'nsis', '0.1.2')
    scenario = guard.Scenario('cross-nsis-to-msi', current, previous)
    for path in (current.path, previous.path):
        path.write_text('artifact', encoding='utf-8')
    installs = []

    def fake_install(installer, log_path=None):
        installs.append(installer.kind)
        return subprocess.CompletedProcess([str(installer.path)], 1603 if installer.kind == 'msi' else 0, 'remove the competing token.place installation first', '')

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, '_terminate_processes', lambda: None)
    monkeypatch.setattr(guard, 'uninstall_best_effort', lambda log_path=None: None)
    monkeypatch.setattr(guard, 'install', fake_install)
    monkeypatch.setattr(guard, '_sentinel_dir', lambda root: root / 'sentinel')
    monkeypatch.setattr(guard, '_safe_env', lambda sentinel, log: {'PATH': str(sentinel), 'TOKENPLACE_SENTINEL_LOG': str(log)})
    monkeypatch.setattr(guard, 'capture_authority_snapshot', lambda: _empty_snapshot(guard))
    monkeypatch.setattr(guard, 'verify_authority_unchanged', lambda before, after: None)
    monkeypatch.setattr(guard, 'verify_no_authority_remains', lambda: None)

    guard.run_scenario(scenario, 'abcdef123456')
    assert installs == ['nsis', 'msi']


def test_windows_installer_identity_probes_scenario_current_version(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current = guard.Installer(tmp_path / 'token.place-desktop-0.1.5-x64-setup.exe', 'nsis', '0.1.5')
    previous = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe', 'nsis', '0.1.3')
    for path in (current.path, previous.path):
        path.write_text('artifact', encoding='utf-8')
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    probed_versions = []

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, '_terminate_processes', lambda: None)
    monkeypatch.setattr(guard, 'uninstall_best_effort', lambda log_path=None: None)
    monkeypatch.setattr(guard, 'install', lambda installer, log_path=None: subprocess.CompletedProcess([str(installer.path)], 0, '', ''))
    monkeypatch.setattr(guard, '_sentinel_dir', lambda root: root / 'sentinel')
    monkeypatch.setattr(guard, '_safe_env', lambda sentinel, log: {'PATH': str(sentinel), 'TOKENPLACE_SENTINEL_LOG': str(log)})
    monkeypatch.setattr(guard, 'seed_config', lambda seeded: tmp_path / guard.CONFIG_NAME)
    monkeypatch.setattr(guard, 'resolve_authoritative_shortcut', lambda rejected_version=None: guard.Shortcut(tmp_path / 'app.lnk', exe))
    monkeypatch.setattr(guard, '_assert_runtime', lambda target: None)
    monkeypatch.setattr(guard, 'launch_for_operator_record', lambda target, env, log_path=None: json.dumps({
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
        'context_tier': guard.seeded_config_values()['context_tier'],
        'preferred_mode': guard.seeded_config_values()['preferred_mode'],
    }))
    monkeypatch.setattr(guard, 'verify_config_preserved', lambda config_path, seeded: None)
    monkeypatch.setattr(guard, 'validate_installed_context_tiers', lambda exe, env, artifact_dir, scenario_name: None)

    def fake_probe(target, env, expected_version, expected_build_id):
        probed_versions.append(expected_version)
        return {'app_version': expected_version, 'build_id': expected_build_id}

    monkeypatch.setattr(guard, 'probe_identity', fake_probe)
    guard.run_scenario(guard.Scenario('upgrade-nsis-to-nsis', current, previous), 'abcdef123456')
    assert probed_versions == ['0.1.5']


def test_windows_installer_identity_rejects_arbitrary_cross_installer_failures(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64.msi', 'msi', '0.1.3')
    previous = guard.Installer(tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe', 'nsis', '0.1.2')
    for path in (current.path, previous.path):
        path.write_text('artifact', encoding='utf-8')

    def fake_install(installer, log_path=None):
        return subprocess.CompletedProcess([str(installer.path)], 55 if installer.kind == 'msi' else 0, 'random failure', '')

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, '_terminate_processes', lambda: None)
    monkeypatch.setattr(guard, 'uninstall_best_effort', lambda log_path=None: None)
    monkeypatch.setattr(guard, 'install', fake_install)
    monkeypatch.setattr(guard, '_sentinel_dir', lambda root: root / 'sentinel')
    monkeypatch.setattr(guard, '_safe_env', lambda sentinel, log: {'PATH': str(sentinel), 'TOKENPLACE_SENTINEL_LOG': str(log)})
    monkeypatch.setattr(guard, 'capture_authority_snapshot', lambda: _empty_snapshot(guard))
    with pytest.raises(guard.InstallerIdentityError, match='current installer failed'):
        guard.run_scenario(guard.Scenario('cross-nsis-to-msi', current, previous), 'abcdef123456')


def test_windows_installer_identity_requires_postconditions_for_competing_rejection(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64.msi', 'msi', '0.1.3')
    previous = guard.Installer(tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe', 'nsis', '0.1.2')
    for path in (current.path, previous.path):
        path.write_text('artifact', encoding='utf-8')

    def fake_install(installer, log_path=None):
        return subprocess.CompletedProcess([str(installer.path)], 1603 if installer.kind == 'msi' else 0, 'remove the competing token.place installation first', '')

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, '_terminate_processes', lambda: None)
    monkeypatch.setattr(guard, 'uninstall_best_effort', lambda log_path=None: None)
    monkeypatch.setattr(guard, 'install', fake_install)
    monkeypatch.setattr(guard, '_sentinel_dir', lambda root: root / 'sentinel')
    monkeypatch.setattr(guard, '_safe_env', lambda sentinel, log: {'PATH': str(sentinel), 'TOKENPLACE_SENTINEL_LOG': str(log)})
    monkeypatch.setattr(guard, 'capture_authority_snapshot', lambda: _empty_snapshot(guard))
    monkeypatch.setattr(guard, 'verify_authority_unchanged', lambda before, after: None)
    monkeypatch.setattr(guard, 'verify_no_authority_remains', lambda: (_ for _ in ()).throw(guard.InstallerIdentityError('ambiguous second shortcut')))
    with pytest.raises(guard.InstallerIdentityError, match='ambiguous second shortcut'):
        guard.run_scenario(guard.Scenario('cross-nsis-to-msi', current, previous), 'abcdef123456')


def test_windows_installer_identity_verify_authority_unchanged_detects_drift() -> None:
    guard = _load_windows_installer_identity()
    before = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory(
            shortcuts=[guard.Shortcut(Path('a.lnk'), Path('C:/prev/token.place.exe'))],
            existing_targets=[Path('C:/prev/token.place.exe')],
            missing_targets=[],
        ),
        registry=[guard.RegistryEntry('key', 'token.place desktop', '', '', False, '')],
    )
    unchanged = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory(
            shortcuts=[guard.Shortcut(Path('a.lnk'), Path('C:/prev/token.place.exe'))],
            existing_targets=[Path('C:/PREV/TOKEN.PLACE.EXE')],
            missing_targets=[],
        ),
        registry=[guard.RegistryEntry('key', 'token.place desktop', '', '', False, '')],
    )
    guard.verify_authority_unchanged(before, unchanged)

    drifted = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory(
            shortcuts=[guard.Shortcut(Path('a.lnk'), Path('C:/prev/token.place.exe')), guard.Shortcut(Path('b.lnk'), Path('C:/new/token.place.exe'))],
            existing_targets=[Path('C:/prev/token.place.exe'), Path('C:/new/token.place.exe')],
            missing_targets=[],
        ),
        registry=[guard.RegistryEntry('key', 'token.place desktop', '', '', False, '')],
    )
    with pytest.raises(guard.InstallerIdentityError, match='changed authority state'):
        guard.verify_authority_unchanged(before, drifted)


def test_windows_installer_identity_verify_authority_removed_detects_residuals(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    lingering_exe = tmp_path / 'token.place.exe'
    lingering_exe.write_text('exe', encoding='utf-8')
    snapshot = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory([], [], []),
        registry=[],
    )
    snapshot_with_target = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory(
            shortcuts=[guard.Shortcut(tmp_path / 'a.lnk', lingering_exe)],
            existing_targets=[lingering_exe],
            missing_targets=[],
        ),
        registry=[],
    )

    monkeypatch.setattr(guard, '_processes_running_targets', lambda targets: [])
    monkeypatch.setattr(guard, 'inventory_registry_entries', lambda: [guard.RegistryEntry('key', 'token.place desktop', '', '', False, '')])
    monkeypatch.setattr(guard, 'inventory_shortcuts', lambda: guard.ShortcutInventory([], [], []))
    with pytest.raises(guard.InstallerIdentityError, match='registry'):
        guard.verify_authority_removed(snapshot)

    monkeypatch.setattr(guard, 'inventory_registry_entries', lambda: [])
    monkeypatch.setattr(guard, 'inventory_shortcuts', lambda: guard.ShortcutInventory([guard.Shortcut(tmp_path / 'a.lnk', lingering_exe)], [], []))
    with pytest.raises(guard.InstallerIdentityError, match='shortcuts'):
        guard.verify_authority_removed(snapshot)

    monkeypatch.setattr(guard, 'inventory_shortcuts', lambda: guard.ShortcutInventory([], [], []))
    with pytest.raises(guard.InstallerIdentityError, match='executables'):
        guard.verify_authority_removed(snapshot_with_target)

    lingering_exe.unlink()
    monkeypatch.setattr(guard, '_processes_running_targets', lambda targets: ['C:/Program Files/token.place/token.place.exe'])
    with pytest.raises(guard.InstallerIdentityError, match='processes'):
        guard.verify_authority_removed(snapshot_with_target)


def _process_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(['powershell'], returncode, stdout, '')


def test_windows_installer_identity_process_inventory_valid_empty_array(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    captured = tmp_path / 'token.place.exe'
    captured.write_text('exe', encoding='utf-8')
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed('[]'))

    assert guard._processes_running_targets([captured]) == []


def test_windows_installer_identity_process_inventory_matches_captured_path_despite_name(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    captured = tmp_path / 'token.place.exe'
    captured.write_text('exe', encoding='utf-8')
    payload = [
        {'Name': 'unexpected-renamed.exe', 'ExecutablePath': str(captured)},
        {'Name': 'token.place.exe', 'ExecutablePath': str(tmp_path / 'unrelated.exe')},
    ]
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed(json.dumps(payload)))

    assert guard._processes_running_targets([captured]) == [str(captured)]


def test_windows_installer_identity_process_inventory_ignores_unrelated_paths(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    captured = tmp_path / 'token.place.exe'
    payload = [{'Name': 'token.place.exe', 'ExecutablePath': str(tmp_path / 'other' / 'token.place.exe')}]
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed(json.dumps(payload)))

    assert guard._processes_running_targets([captured]) == []


@pytest.mark.parametrize(
    ('stdout', 'returncode', 'match'),
    [
        ('[]', 7, 'process inventory command failed'),
        ('', 0, 'emitted no JSON'),
        ('not-json', 0, 'invalid JSON'),
        (json.dumps({'Name': 'token.place.exe', 'ExecutablePath': 'C:/token.place.exe'}), 0, 'must be an array'),
        (json.dumps([{'Name': 'token.place.exe'}]), 0, 'string Name and ExecutablePath'),
        (json.dumps([{'Name': 42, 'ExecutablePath': 'C:/token.place.exe'}]), 0, 'string Name and ExecutablePath'),
        (json.dumps(['C:/token.place.exe']), 0, 'entries must be objects'),
    ],
)
def test_windows_installer_identity_process_inventory_fail_closed(monkeypatch, tmp_path, stdout, returncode, match) -> None:
    guard = _load_windows_installer_identity()
    captured = tmp_path / 'token.place.exe'
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed(stdout, returncode))

    with pytest.raises(guard.InstallerIdentityError, match=match):
        guard._processes_running_targets([captured])


def test_windows_installer_identity_cleanup_polling_propagates_process_inventory_failure(monkeypatch) -> None:
    guard = _load_windows_installer_identity()
    snapshot = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory(
            shortcuts=[guard.Shortcut(Path('a.lnk'), Path('C:/installed/token.place.exe'))],
            existing_targets=[Path('C:/installed/token.place.exe')],
            missing_targets=[],
        ),
        registry=[],
    )
    monkeypatch.setattr(guard, 'inventory_shortcuts', lambda: guard.ShortcutInventory([], [], []))
    monkeypatch.setattr(guard, 'inventory_registry_entries', lambda: [])

    def fail_process_inventory(targets):
        raise guard.InstallerIdentityError('process inventory command failed')

    monkeypatch.setattr(guard, '_processes_running_targets', fail_process_inventory)
    with pytest.raises(guard.InstallerIdentityError, match='process inventory command failed'):
        guard.wait_for_cleanup_convergence(snapshot, deadline_seconds=1, sleeper=lambda _: None)

def test_windows_installer_identity_cleanup_polling_converges_without_sleeping_real_time(monkeypatch) -> None:
    guard = _load_windows_installer_identity()
    snapshot = _empty_snapshot(guard)
    categories = iter([['shortcuts'], ['shortcuts', 'registry'], []])
    sleeps = []
    now = {'value': 0.0}

    def fake_residual(before):
        return next(categories)

    def fake_monotonic():
        now['value'] += 0.1
        return now['value']

    monkeypatch.setattr(guard, 'residual_authority_categories', fake_residual)
    guard.wait_for_cleanup_convergence(
        snapshot,
        deadline_seconds=2,
        poll_seconds=0.25,
        monotonic=fake_monotonic,
        sleeper=sleeps.append,
    )
    assert sleeps == [0.25, 0.25]


def test_windows_installer_identity_cleanup_polling_reports_residual_categories(monkeypatch) -> None:
    guard = _load_windows_installer_identity()
    snapshot = _empty_snapshot(guard)
    times = iter([0.0, 0.2, 0.4, 0.6])
    monkeypatch.setattr(guard, 'residual_authority_categories', lambda before: ['registry', 'processes'])

    with pytest.raises(guard.InstallerIdentityError, match='registry, processes'):
        guard.wait_for_cleanup_convergence(
            snapshot,
            deadline_seconds=0.5,
            poll_seconds=0.1,
            monotonic=lambda: next(times),
            sleeper=lambda _: None,
        )


def test_split_uninstall_command_handles_quoted_and_unquoted_forms() -> None:
    guard = _load_windows_installer_identity()
    assert guard.split_uninstall_command(
        r'"C:\Program Files\token.place desktop\Uninstall token.place desktop.exe" /S'
    ) == (r'C:\Program Files\token.place desktop\Uninstall token.place desktop.exe', '/S')
    assert guard.split_uninstall_command(
        r'MsiExec.exe /X{8FA1D2C0-0000-0000-0000-000000000000}'
    ) == ('MsiExec.exe', r'/X{8FA1D2C0-0000-0000-0000-000000000000}')
    assert guard.split_uninstall_command(r'C:\tools\uninstall.exe') == (r'C:\tools\uninstall.exe', '')
    with pytest.raises(guard.InstallerIdentityError, match='unparsable quoted uninstall command'):
        guard.split_uninstall_command('"unterminated quote')
    with pytest.raises(guard.InstallerIdentityError, match='empty uninstall command'):
        guard.split_uninstall_command('   ')


def test_build_uninstall_invocation_for_msi_uses_product_code_directly() -> None:
    guard = _load_windows_installer_identity()
    entry = guard.RegistryEntry(
        key_path='HKLM:\\...',
        display_name='token.place desktop',
        uninstall_string='MsiExec.exe /I{8FA1D2C0-0000-0000-0000-000000000000}',
        quiet_uninstall_string='',
        windows_installer=True,
        product_code='{8FA1D2C0-0000-0000-0000-000000000000}',
    )
    invocation = guard.build_uninstall_invocation(entry)
    assert invocation[0] == guard._msiexec()
    assert invocation[1:] == ['/x', '{8FA1D2C0-0000-0000-0000-000000000000}', '/qn', '/norestart']


def test_build_uninstall_invocation_for_nsis_appends_silent_flag() -> None:
    guard = _load_windows_installer_identity()
    entry = guard.RegistryEntry(
        key_path='HKLM:\\...',
        display_name='token.place desktop',
        uninstall_string=r'"C:\Program Files\token.place desktop\Uninstall token.place desktop.exe"',
        quiet_uninstall_string='',
        windows_installer=False,
        product_code='',
    )
    assert guard.build_uninstall_invocation(entry) == [
        r'C:\Program Files\token.place desktop\Uninstall token.place desktop.exe',
        '/S',
    ]

    already_silent = guard.RegistryEntry(
        key_path='HKLM:\\...',
        display_name='token.place desktop',
        uninstall_string=r'"C:\Program Files\token.place desktop\Uninstall token.place desktop.exe" /S',
        quiet_uninstall_string='',
        windows_installer=False,
        product_code='',
    )
    assert guard.build_uninstall_invocation(already_silent) == [
        r'C:\Program Files\token.place desktop\Uninstall token.place desktop.exe',
        '/S',
    ]


def test_build_uninstall_invocation_requires_command() -> None:
    guard = _load_windows_installer_identity()
    entry = guard.RegistryEntry('HKLM:\\...', 'token.place desktop', '', '', False, '')
    with pytest.raises(guard.InstallerIdentityError, match='has no uninstall command'):
        guard.build_uninstall_invocation(entry)


def test_assert_runtime_requires_valid_matching_provenance(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    install_dir = tmp_path / 'token.place desktop'
    runtime_dir = install_dir / 'python-runtime'
    runtime_dir.mkdir(parents=True)
    exe = install_dir / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    python_exe = runtime_dir / 'python.exe'
    python_exe.write_text('python', encoding='utf-8')
    obsolete = runtime_dir / guard.OBSOLETE_RUNTIME_PROVENANCE_NAME
    obsolete.write_text(json.dumps({'runtime_id': guard.EXPECTED_RUNTIME_ID}), encoding='utf-8')

    with pytest.raises(guard.InstallerIdentityError, match='missing'):
        guard._assert_runtime(exe)

    provenance = runtime_dir / guard.RUNTIME_PROVENANCE_NAME
    provenance.write_text('{not valid json', encoding='utf-8')
    with pytest.raises(guard.InstallerIdentityError, match='not valid JSON'):
        guard._assert_runtime(exe)

    provenance.write_text(json.dumps({'runtime_id': 'bundled-cpython-3.11-win-x86_64-cu999'}), encoding='utf-8')
    with pytest.raises(guard.InstallerIdentityError, match='unexpected or missing runtime id'):
        guard._assert_runtime(exe)

    provenance.write_text(json.dumps({'runtime_id': guard.EXPECTED_RUNTIME_ID}), encoding='utf-8')
    guard._assert_runtime(exe)


def test_windows_installer_identity_operator_record_rejects_fabricated_or_incomplete() -> None:
    guard = _load_windows_installer_identity()
    guard.assert_operator_record(json.dumps({
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
    }))
    with pytest.raises(guard.InstallerIdentityError, match='did not emit JSON'):
        guard.assert_operator_record('launcher_source=bundled interpreter_basename=python.exe')
    with pytest.raises(guard.InstallerIdentityError, match='operator-start preflight record missing'):
        guard.assert_operator_record(json.dumps({
            'launcher_source': 'bundled',
            'interpreter_basename': 'python.exe',
            'runtime_id': guard.EXPECTED_RUNTIME_ID,
        }))
    with pytest.raises(guard.InstallerIdentityError, match='operator-start preflight record missing'):
        guard.assert_operator_record(json.dumps({'launcher_source': 'system_development', 'interpreter_basename': 'python.exe'}))


def test_windows_installer_identity_uses_tauri_config_path_and_schema(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    config_path = guard.seed_config()
    assert config_path == tmp_path / 'Roaming' / 'place.token.desktop' / 'desktop_tauri_config.json'
    data = json.loads(config_path.read_text(encoding='utf-8'))
    assert set(data) == {'relay_base_url', 'relay_base_urls', 'model_path', 'preferred_mode', 'context_tier'}
    assert data['relay_base_url'] == data['relay_base_urls'][0]


def _load_previous_release_selector_source() -> str:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
    steps = workflow['jobs']['build']['steps']
    script = next(step for step in steps if step.get('name') == 'Validate Windows MSI and NSIS artifact contents')['run']
    marker = 'previous_tag="$(python - "${current_version}" "${release_source_repo}" "${releases_json}" <<\'PY\'\n'
    start = script.index(marker) + len(marker)
    end = script.index('\nPY\n)"', start)
    return script[start:end]


def _run_previous_release_selector(current_version: str, releases: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    releases_path = tmp_path / 'releases.json'
    releases_path.write_text(json.dumps(releases), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['selector', current_version, 'owner/repo', str(releases_path)])
    from io import StringIO
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        exec(compile(_load_previous_release_selector_source(), '<previous-release-selector>', 'exec'), {'__name__': '__selector__'})
        return sys.stdout.getvalue().strip()
    finally:
        sys.stdout = old_stdout


def test_previous_release_selector_uses_camel_case_publication_fields(tmp_path, monkeypatch) -> None:
    releases = [
        {'tagName': 'desktop-v0.1.1', 'isDraft': False, 'isPrerelease': False},
        {'tagName': 'desktop-v0.1.2', 'isDraft': False, 'isPrerelease': False},
        {'tagName': 'desktop-v0.1.3', 'isDraft': False, 'isPrerelease': False},
        {'tagName': 'desktop-v0.1.4', 'isDraft': False, 'isPrerelease': False},
    ]
    assert _run_previous_release_selector('0.1.3', releases, tmp_path, monkeypatch) == 'desktop-v0.1.2'
    assert _run_previous_release_selector('0.1.5', releases, tmp_path, monkeypatch) == 'desktop-v0.1.4'


def test_previous_release_selector_rejects_drafts_prereleases_and_malformed_records(tmp_path, monkeypatch) -> None:
    releases = [
        {'tagName': 'desktop-v0.1.3', 'isDraft': True, 'isPrerelease': False},
        {'tagName': 'desktop-v0.1.2', 'isDraft': False, 'isPrerelease': True},
        {'tagName': 'desktop-v0.1.1', 'isDraft': False},
        {'tagName': 'desktop-v0.1.0', 'isPrerelease': False},
        {'tagName': 'desktop-v0.0.9', 'draft': False, 'prerelease': False},
        {'tagName': 'desktop-v0.0.8', 'isDraft': 'false', 'isPrerelease': False},
        {'tagName': 'desktop-v0.0.7', 'isDraft': False, 'isPrerelease': None},
        {'tagName': 'desktop-vnot-semver', 'isDraft': False, 'isPrerelease': False},
        {'tagName': 'desktop-v0.0.6', 'isDraft': False, 'isPrerelease': False},
    ]
    assert _run_previous_release_selector('0.1.5', releases, tmp_path, monkeypatch) == 'desktop-v0.0.6'


def test_previous_release_selector_reports_documented_error_when_no_valid_predecessor(tmp_path, monkeypatch) -> None:
    releases = [
        {'tagName': 'desktop-v0.1.2', 'isDraft': True, 'isPrerelease': False},
        {'tagName': 'desktop-v0.1.1', 'isDraft': False, 'isPrerelease': True},
        {'tagName': 'desktop-v0.1.0', 'draft': False, 'prerelease': False},
    ]
    with pytest.raises(SystemExit, match='no stable published desktop-vX.Y.Z predecessor exists for current version 0.1.3 in owner/repo'):
        _run_previous_release_selector('0.1.3', releases, tmp_path, monkeypatch)


def test_publish_transaction_orders_draft_create_cleanup_upload_promotion_disarm() -> None:
    script = _load_publish_step('Create immutable GitHub Release')['run']
    create = script.index('-F "draft=true"')
    capture = script.index('created_release_id="$(jq -r')
    arm = script.index('cleanup_armed=1')
    upload = script.index('Content-Type: application/octet-stream')
    promote = script.index('gh api --method PATCH "/repos/${GITHUB_REPOSITORY}/releases/${release_id}"')
    disarm = script.index('cleanup_armed=0', promote)
    assert create < capture < arm < upload < promote < disarm
    assert '-F "draft=false"' in script[promote:disarm]
    assert 'trap cleanup_created_draft EXIT' in script[:create]
    assert 'trap - EXIT' in script[disarm:]


def test_publish_failure_cleanup_targets_only_created_draft_release_id() -> None:
    script = _load_publish_step('Create immutable GitHub Release')['run']
    cleanup_block = script[script.index('cleanup_created_draft() {'):script.index('trap cleanup_created_draft EXIT')]
    assert 'created_release_id' in cleanup_block
    assert 'releases/${created_release_id}' in cleanup_block
    assert 'refs/tags' not in cleanup_block
    assert 'git tag' not in cleanup_block
    assert 'gh release delete' not in cleanup_block
    assert 'release-lookup' not in cleanup_block
    assert 'assets' not in cleanup_block
    assert 'Failed to delete draft release ${created_release_id}' in cleanup_block


def test_publish_flow_absent_overwrite_reuse_and_existing_draft_paths() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    assert '--clobber' not in text
    assert 'overwrite_files' not in text
    assert 'softprops/action-gh-release' not in text
    assert 'gh release view' not in text
    assert 'gh release delete' not in text
    assert 'gh api --paginate --slurp "/repos/${GITHUB_REPOSITORY}/releases"' in text
    assert 'already exists (including draft); desktop releases are immutable' in text


def test_windows_installer_identity_operator_record_requires_exact_context_tier_contract() -> None:
    guard = _load_windows_installer_identity()
    base = {
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
        'selected_model_profile': 'qwen3-8b-q4',
        'startup_phase': 'ready',
        'startup_result': 'ready',
        'startup_deadline_ms': 15000,
        'runtime_action': 'installed_artifact_context_probe',
        'fallback_reason': None,
        'backend_fallback': False,
        'model_fallback': False,
        'context_fallback': False,
    }
    guard.assert_operator_record(json.dumps({**base, 'context_tier': '8k-fast', 'effective_n_ctx': 8192, 'n_ctx': 8192}), expected_tier='8k-fast')
    guard.assert_operator_record(json.dumps({
        **base,
        'context_tier': '64k-full',
        'effective_n_ctx': 65536,
        'n_ctx': 65536,
        'api_v1_readiness_yarn_requested_context_tokens': 65536,
        'api_v1_readiness_yarn_rope_supported': True,
        'startup_phase': 'ready',
        'startup_result': 'ready',
        'startup_deadline_ms': 300000,
    }), expected_tier='64k-full')
    with pytest.raises(guard.InstallerIdentityError, match='mismatched context tier'):
        guard.assert_operator_record(json.dumps({**base, 'context_tier': '8k-fast', 'effective_n_ctx': 65536, 'n_ctx': 65536}), expected_tier='8k-fast')
    with pytest.raises(guard.InstallerIdentityError, match='fallback'):
        guard.assert_operator_record(json.dumps({**base, 'context_tier': '8k-fast', 'effective_n_ctx': 8192, 'n_ctx': 8192, 'fallback_reason': 'cpu'}), expected_tier='8k-fast')


def test_windows_installer_identity_second_launch_rejects_repair_or_provisioning() -> None:
    guard = _load_windows_installer_identity()
    record = {
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
        'selected_model_profile': 'qwen3-8b-q4',
        'context_tier': '8k-fast',
        'effective_n_ctx': 8192,
        'n_ctx': 8192,
        'startup_phase': 'ready',
        'startup_result': 'ready',
        'startup_deadline_ms': 15000,
        'fallback_reason': None,
        'backend_fallback': False,
        'model_fallback': False,
        'context_fallback': False,
        'runtime_installation_attempted_count': 1,
    }
    with pytest.raises(guard.InstallerIdentityError, match='forbidden provisioning'):
        guard.assert_operator_record(json.dumps(record), expected_tier='8k-fast', launch_number=2)


def test_windows_installer_identity_context_tier_probe_executes_twice_per_tier(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    launched: list[tuple[str, str]] = []
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))

    def fake_launch(path, env, log_path=None):
        tier = json.loads((tmp_path / 'Roaming' / guard.TAURI_IDENTIFIER / guard.CONFIG_NAME).read_text(encoding='utf-8'))['context_tier']
        launch = env['TOKENPLACE_INSTALLER_IDENTITY_LAUNCH_NUMBER']
        launched.append((tier, launch))
        n_ctx = 65536 if tier == '64k-full' else 8192
        payload = {
            'record': 'desktop.compute_node.session.layout',
            'operator_start_preflight': 'ok',
            'resource_context_source': 'tauri_app_handle',
            'bridge_child_spawned': True,
            'bridge_event_received': True,
            'launcher_source': 'bundled',
            'interpreter_basename': 'python.exe',
            'runtime_id': guard.EXPECTED_RUNTIME_ID,
            'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
            'bridge_preflight': 'ok',
            'model_artifact_inspect': 'ok',
            'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
            'startup_result': 'ready',
            'selected_model_profile': 'qwen3-8b-q4',
            'context_tier': tier,
            'effective_n_ctx': n_ctx,
            'n_ctx': n_ctx,
            'startup_phase': 'ready',
            'startup_result': 'ready',
            'startup_deadline_ms': 15000,
            'runtime_action': 'installed_artifact_context_probe',
            'fallback_reason': None,
            'backend_fallback': False,
            'model_fallback': False,
            'context_fallback': False,
            'runtime_installation_attempted_count': 0,
            'runtime_repair_attempted_count': 0,
            'dependency_provisioning_attempted_count': 0,
            'runtime_mutation': False,
            'api_v1_readiness_yarn_requested_context_tokens': n_ctx,
            'api_v1_readiness_yarn_rope_supported': True,
        }
        return json.dumps(payload)

    monkeypatch.setattr(guard, 'launch_for_operator_record', fake_launch)
    guard.validate_installed_context_tiers(exe, {}, None, 'scenario')
    assert launched == [('8k-fast', '1'), ('8k-fast', '2'), ('64k-full', '1'), ('64k-full', '2')]


def test_installed_context_smoke_probe_uses_context_profile_helper() -> None:
    import importlib.util

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    eight = module.installed_context_smoke_payload('8k-fast', '1')
    full = module.installed_context_smoke_payload('64k-full', '2')
    assert eight['effective_n_ctx'] == 8192
    assert full['effective_n_ctx'] == 65536
    assert full['api_v1_readiness_yarn_requested_context_tokens'] == 65536
    assert full['gpu_capability'] == 'mocked_hosted_windows_contract_no_real_cuda'


def test_operator_session_smoke_rust_invocation_does_not_pass_launch_number_cli_arg() -> None:
    source = Path('desktop-tauri/src-tauri/src/compute_node.rs').read_text(encoding='utf-8')
    function = source[source.index('pub(crate) fn operator_session_smoke_record'):source.index('\npub async fn start_compute_node', source.index('pub(crate) fn operator_session_smoke_record'))]
    assert '.arg("--installed-context-smoke")' in function
    assert '.arg("--context-tier")' in function
    assert '--launch-number' not in function
    assert 'TOKENPLACE_INSTALLER_IDENTITY_LAUNCH_NUMBER' not in function


def test_installed_context_smoke_cli_inherits_launch_number_from_environment(monkeypatch, capsys) -> None:
    import importlib.util

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_cli_env', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'argv', ['compute_node_bridge.py', '--installed-context-smoke', '--context-tier', '8k-fast'])
    monkeypatch.setenv('TOKENPLACE_INSTALLER_IDENTITY_LAUNCH_NUMBER', '2')
    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['launch_number'] == '2'
    assert payload['runtime_installation_attempted_count'] == 0
    assert payload['runtime_repair_attempted_count'] == 0


def test_installed_context_smoke_cli_rejects_unknown_rust_supplied_arguments(monkeypatch) -> None:
    import importlib.util

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_cli_unknown', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'argv', ['compute_node_bridge.py', '--installed-context-smoke', '--context-tier', '8k-fast', '--launch-number', '1'])
    with pytest.raises(SystemExit):
        module.main()


def test_installed_context_smoke_probe_constructs_model_once_and_reports_observed_state() -> None:
    import importlib.util

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_observed', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    eight = module.installed_context_smoke_payload('8k-fast', '1')
    full = module.installed_context_smoke_payload('64k-full', '2')
    assert eight['constructor_call_count'] == 1
    assert full['constructor_call_count'] == 1
    assert eight['constructor_observed_n_ctx'] == eight['effective_n_ctx'] == 8192
    assert full['constructor_observed_n_ctx'] == full['effective_n_ctx'] == 65536
    assert eight['startup_result'] == full['startup_result'] == 'ready'
    assert eight['fallback_reason'] is None
    assert full['api_v1_readiness_yarn_rope_supported'] is True
    assert full['api_v1_readiness_yarn_rope_enabled'] is True
    assert full['api_v1_readiness_yarn_configuration_valid'] is True
    assert full['gpu_capability'] == 'mocked_hosted_windows_contract_no_real_cuda'



def test_windows_installer_identity_manifest_detects_added_removed_modified(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    runtime = tmp_path / 'python-runtime'
    resources = tmp_path / 'resources'
    runtime.mkdir()
    resources.mkdir()
    tracked = runtime / 'python.exe'
    tracked.write_text('runtime', encoding='utf-8')
    before = guard.capture_installed_resource_manifest(exe)
    (resources / 'added.txt').write_text('new', encoding='utf-8')
    after_added = guard.capture_installed_resource_manifest(exe)
    with pytest.raises(guard.InstallerIdentityError, match='added'):
        guard.assert_manifest_unchanged(before, after_added, phase='added')
    (resources / 'added.txt').unlink()
    tracked.write_text('runtime changed', encoding='utf-8')
    after_modified = guard.capture_installed_resource_manifest(exe)
    with pytest.raises(guard.InstallerIdentityError, match='modified'):
        guard.assert_manifest_unchanged(before, after_modified, phase='modified')
    tracked.unlink()
    after_removed = guard.capture_installed_resource_manifest(exe)
    with pytest.raises(guard.InstallerIdentityError, match='removed'):
        guard.assert_manifest_unchanged(before, after_removed, phase='removed')


def test_windows_installer_identity_second_launch_requires_observed_zero_counters() -> None:
    guard = _load_windows_installer_identity()
    base = {
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
        'selected_model_profile': 'qwen3-8b-q4',
        'model_profile_identifier': 'qwen3-8b-q4-k-m',
        'context_tier': '8k-fast',
        'effective_n_ctx': 8192,
        'n_ctx': 8192,
        'startup_phase': 'ready',
        'startup_result': 'ready',
        'startup_deadline_ms': 15000,
        'runtime_action': 'installed_artifact_context_probe_no_provisioning',
        'fallback_reason': None,
        'backend_fallback': False,
        'model_fallback': False,
        'context_fallback': False,
    }
    with pytest.raises(guard.InstallerIdentityError, match='forbidden provisioning'):
        guard.assert_operator_record(json.dumps({**base, 'network_attempted_count': 1}), expected_tier='8k-fast', launch_number=2)
    guard.assert_operator_record(json.dumps({
        **base,
        'network_attempted_count': 0,
        'runtime_installation_attempted_count': 0,
        'runtime_repair_attempted_count': 0,
        'dependency_provisioning_attempted_count': 0,
        'provisioning_attempted_count': 0,
        'model_download_attempted_count': 0,
    }), expected_tier='8k-fast', launch_number=2)


def test_installed_context_smoke_fails_when_get_llm_instance_returns_none(monkeypatch) -> None:
    import importlib.util
    from utils.llm.model_manager import ModelManager

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_none', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(ModelManager, 'get_llm_instance', lambda self: None)

    with pytest.raises(RuntimeError, match='installed_context_get_llm_instance_returned_none'):
        module.installed_context_smoke_payload('8k-fast', '1')

def test_installed_context_smoke_fails_when_constructor_is_skipped(monkeypatch) -> None:
    import importlib.util
    from utils.llm.model_manager import ModelManager

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_skip_constructor', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(ModelManager, 'get_llm_instance', lambda self: object())

    with pytest.raises(RuntimeError, match='installed_context_constructor_not_called_exactly_once'):
        module.installed_context_smoke_payload('8k-fast', '1')


def test_installed_context_smoke_fails_on_forbidden_model_download_attempt(monkeypatch) -> None:
    import importlib.util
    from utils.llm.model_manager import ModelManager

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_forbidden_download', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def get_llm_instance_with_download(self):
        self.download_model_if_needed()
        return object()

    monkeypatch.setattr(ModelManager, 'get_llm_instance', get_llm_instance_with_download)

    with pytest.raises(RuntimeError, match='installed_context_forbidden_model_download_attempted'):
        module.installed_context_smoke_payload('8k-fast', '1')

def test_installed_context_smoke_uses_get_llm_instance_boundary() -> None:
    source = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py').read_text(encoding='utf-8')
    function = source[source.index('def installed_context_smoke_payload'):source.index('\ndef main() -> int:', source.index('def installed_context_smoke_payload'))]
    assert '.get_llm_instance()' in function
    assert 'manager._resolve_compute_plan()' not in function
    assert 'manager._runtime_init_kwargs(' not in function
    assert '"runtime_installation_attempted": False' not in function
    assert 'second_launch_no_repair' not in function


def test_windows_installer_identity_main_non_windows_contract_success(monkeypatch, tmp_path, capsys) -> None:
    guard = _load_windows_installer_identity()
    current_nsis = tmp_path / 'token.place-desktop-0.1.5-x64-setup.exe'
    current_msi = tmp_path / 'token.place-desktop-0.1.5-x64.msi'
    previous_nsis = tmp_path / 'token.place-desktop-0.1.4-x64-setup.exe'
    previous_msi = tmp_path / 'token.place-desktop-0.1.4-x64.msi'
    for path in (current_nsis, current_msi, previous_nsis, previous_msi):
        path.write_text('artifact', encoding='utf-8')
    monkeypatch.setattr(guard.sys, 'platform', 'linux')
    monkeypatch.setattr(sys, 'argv', [
        'test_windows_installer_identity.py',
        '--windows-nsis', str(current_nsis),
        '--windows-msi', str(current_msi),
        '--previous-windows-nsis', str(previous_nsis),
        '--previous-windows-msi', str(previous_msi),
        '--expected-build-id', 'abcdef123456',
    ])

    assert guard.main() == 0

    output = capsys.readouterr().out
    assert 'validated Windows installer scenario contract' in output


def test_windows_installer_identity_run_all_scenarios_uses_custom_runner_contract(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    scenario = guard.Scenario(
        'clean/nsis',
        guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe', 'nsis', '0.1.3'),
    )
    calls: list[tuple[object, str]] = []

    def fake_runner(scenario_arg, expected_build_id):
        calls.append((scenario_arg, expected_build_id))

    guard.run_all_scenarios([scenario], 'abcdef123456', runner=fake_runner, artifact_root=tmp_path / 'logs')

    assert calls == [(scenario, 'abcdef123456')]


def test_windows_installer_identity_probe_identity_accepts_json_and_raw_fallback(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        stdout = 'not json but includes 0.1.3 and abcdef123456' if '--build-identity' in cmd else '{}'
        return subprocess.CompletedProcess(cmd, 0, stdout)

    monkeypatch.setattr(guard, '_run', fake_run)

    assert guard.probe_identity(exe, {}, '0.1.3', 'abcdef123456') == {'raw': 'not json but includes 0.1.3 and abcdef123456'}
    assert calls == [[str(exe), '--build-identity-json'], [str(exe), '--build-identity']]


def test_windows_installer_identity_run_scenario_rejects_sentinel_after_success(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    current = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe', 'nsis', '0.1.3')
    current.path.write_text('installer', encoding='utf-8')
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    monkeypatch.setattr(guard, '_terminate_processes', lambda: None)
    monkeypatch.setattr(guard, 'uninstall_best_effort', lambda log_path=None: None)
    monkeypatch.setattr(guard, 'install', lambda installer, log_path=None: subprocess.CompletedProcess([str(installer.path)], 0, 'ok'))
    monkeypatch.setattr(guard, 'resolve_authoritative_shortcut', lambda rejected_version=None: guard.Shortcut(tmp_path / 'token.place.lnk', exe))
    monkeypatch.setattr(guard, '_assert_runtime', lambda path: None)
    monkeypatch.setattr(guard, 'probe_identity', lambda *args, **kwargs: {})
    monkeypatch.setattr(guard, 'validate_installed_context_tiers', lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, 'launch_for_operator_record', lambda *args, **kwargs: json.dumps({
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
    }))
    real_sentinel_dir = guard._sentinel_dir

    def sentinel_dir_with_activity(root):
        directory = real_sentinel_dir(root)
        (root / 'sentinel.log').write_text('SENTINEL python invoked\n', encoding='utf-8')
        return directory

    monkeypatch.setattr(guard, '_sentinel_dir', sentinel_dir_with_activity)

    with pytest.raises(guard.InstallerIdentityError, match='sentinel was invoked'):
        guard.run_scenario(guard.Scenario('clean-nsis-0.1.5', current), 'abcdef123456')



def test_installed_context_smoke_probe_source_has_fail_closed_constructor_checks() -> None:
    source = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py').read_text(encoding='utf-8')
    function = source[source.index('def installed_context_smoke_payload'):source.index('\ndef main() -> int:', source.index('def installed_context_smoke_payload'))]
    assert 'installed_context_get_llm_instance_returned_none' in function
    assert 'installed_context_constructor_not_called_exactly_once' in function
    assert 'installed_context_constructor_n_ctx_mismatch' in function
    assert 'installed_context_forbidden_attempts' in function
    assert 'unexpected_compute_fallback' in function
    assert 'installed_context_64k_yarn_rope_capability_bypassed_or_unsupported' in function


def test_windows_installer_identity_safe_env_restricts_path_and_preserves_required_vars(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    sentinel = tmp_path / 'sentinel'
    sentinel.mkdir()
    log = tmp_path / 'sentinel.log'
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    monkeypatch.setenv('ComSpec', r'C:\Windows\System32\cmd.exe')
    monkeypatch.setenv('APPDATA', r'C:\Users\runner\AppData\Roaming')
    monkeypatch.setenv('PATH', r'C:\Windows\System32')

    env = guard._safe_env(sentinel, log, {'TOKENPLACE_EXTRA': 'ok'})

    assert env['PATH'] == str(sentinel)
    assert env['TOKENPLACE_SENTINEL_LOG'] == str(log)
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert env['SystemRoot'] == r'C:\Windows'
    assert env['ComSpec'] == r'C:\Windows\System32\cmd.exe'
    assert env['APPDATA'] == r'C:\Users\runner\AppData\Roaming'
    assert env['TOKENPLACE_EXTRA'] == 'ok'


def test_windows_installer_identity_sentinel_dir_creates_every_host_tool_guard(tmp_path) -> None:
    guard = _load_windows_installer_identity()

    directory = guard._sentinel_dir(tmp_path)

    assert directory == tmp_path / 'sentinel-path'
    for name in guard.SENTINELS:
        sentinel = directory / f'{name}.cmd'
        text = sentinel.read_text(encoding='utf-8')
        assert f'SENTINEL {name} invoked' in text
        assert '%TOKENPLACE_SENTINEL_LOG%' in text
        assert 'exit /b 42' in text


def test_windows_installer_identity_probe_attempt_counters_fail_closed() -> None:
    guard = _load_windows_installer_identity()
    guard.assert_no_probe_attempt_counters({
        'runtime_installation_attempted_count': 0,
        'runtime_repair_attempted_count': '0',
        'dependency_provisioning_attempted_count': None,
        'provisioning_attempted_count': 0,
        'network_attempted_count': 0,
        'model_download_attempted_count': 0,
    })

    with pytest.raises(guard.InstallerIdentityError, match='forbidden provisioning/network work'):
        guard.assert_no_probe_attempt_counters({'network_attempted_count': 1})


def test_windows_installer_identity_operator_record_accepts_64k_ready_contract() -> None:
    guard = _load_windows_installer_identity()
    record = {
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
        'context_tier': '64k-full',
        'effective_n_ctx': 65536,
        'n_ctx': 65536,
        'selected_model_profile': 'qwen3-8b-q4',
        'startup_phase': 'ready',
        'startup_deadline_ms': 15000,
        'startup_result': 'ready',
        'fallback_reason': None,
        'backend_fallback': False,
        'model_fallback': False,
        'context_fallback': False,
        'api_v1_readiness_yarn_requested_context_tokens': 65536,
        'api_v1_readiness_yarn_rope_supported': True,
        'runtime_installation_attempted_count': 0,
        'runtime_repair_attempted_count': 0,
        'dependency_provisioning_attempted_count': 0,
        'provisioning_attempted_count': 0,
        'network_attempted_count': 0,
        'model_download_attempted_count': 0,
        'runtime_action': 'installed_artifact_context_probe_no_provisioning',
    }

    parsed = guard.assert_operator_record(json.dumps(record), expected_tier='64k-full', launch_number=2)

    assert parsed['effective_n_ctx'] == 65536


def test_windows_installer_identity_operator_record_rejects_multiline_or_fallback() -> None:
    guard = _load_windows_installer_identity()
    valid = {
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
    }

    with pytest.raises(guard.InstallerIdentityError, match='exactly one'):
        guard.assert_operator_record(json.dumps(valid) + '\n' + json.dumps(valid))

    invalid = dict(valid, context_tier='8k-fast', effective_n_ctx=8192, n_ctx=8192,
                   selected_model_profile='qwen3-8b-q4', startup_phase='ready', startup_deadline_ms=1000,
                   startup_result='ready', backend_fallback=True)
    with pytest.raises(guard.InstallerIdentityError, match='fallback'):
        guard.assert_operator_record(json.dumps(invalid), expected_tier='8k-fast')


def test_installed_context_smoke_fails_on_observed_compute_fallback(monkeypatch) -> None:
    import importlib.util
    from utils.llm.model_manager import ModelManager

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_fallback_guard', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_get = ModelManager.get_llm_instance

    def get_llm_instance_with_fallback(self):
        result = original_get(self)
        self.last_compute_diagnostics = {'fallback_reason': 'forced-test-fallback'}
        return result

    monkeypatch.setattr(ModelManager, 'get_llm_instance', get_llm_instance_with_fallback)

    with pytest.raises(RuntimeError, match='unexpected_compute_fallback'):
        module.installed_context_smoke_payload('8k-fast', '1')


def test_installed_context_smoke_fails_when_64k_capability_is_not_supported(monkeypatch) -> None:
    import importlib.util
    from utils.llm.model_manager import ModelManager

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_yarn_guard', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_get = ModelManager.get_llm_instance

    def get_llm_instance_without_yarn_support(self):
        result = original_get(self)
        self.last_yarn_rope_diagnostics = {'supported': False}
        return result

    monkeypatch.setattr(ModelManager, 'get_llm_instance', get_llm_instance_without_yarn_support)

    with pytest.raises(RuntimeError, match='64k_yarn_rope_capability'):
        module.installed_context_smoke_payload('64k-full', '1')


def test_installed_context_smoke_fails_on_wrong_constructor_n_ctx(monkeypatch) -> None:
    import importlib.util
    from utils.llm.model_manager import ModelManager

    probe_path = Path('desktop-tauri/src-tauri/python/compute_node_bridge.py')
    spec = importlib.util.spec_from_file_location('compute_node_bridge_wrong_n_ctx', probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_get = ModelManager.get_llm_instance

    def get_llm_instance_with_tampered_context_size(self):
        result = original_get(self)
        self.config['model.context_size'] = 1234
        return result

    monkeypatch.setattr(ModelManager, 'get_llm_instance', get_llm_instance_with_tampered_context_size)

    with pytest.raises(RuntimeError, match='constructor_n_ctx_mismatch'):
        module.installed_context_smoke_payload('8k-fast', '1')


def test_windows_installer_identity_classifies_artifacts_and_sanitizes_log_paths(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    nsis = tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe'
    msi = tmp_path / 'token.place-desktop-0.1.3-x64.msi'
    unsupported = tmp_path / 'token.place-desktop-0.1.3-x64.zip'
    wrong_version = tmp_path / 'token.place-desktop-0.1.2-x64-setup.exe'
    for path in (nsis, msi, unsupported, wrong_version):
        path.write_text('artifact', encoding='utf-8')

    assert guard.classify_installer(nsis, '0.1.3').kind == 'nsis'
    assert guard.classify_installer(msi, '0.1.3').kind == 'msi'
    with pytest.raises(guard.InstallerIdentityError, match='unsupported Windows installer type'):
        guard.classify_installer(unsupported, '0.1.3')
    with pytest.raises(guard.InstallerIdentityError, match='filename must include 0.1.3'):
        guard.classify_installer(wrong_version, '0.1.3')
    with pytest.raises(guard.InstallerIdentityError, match='installer does not exist'):
        guard.classify_installer(tmp_path / 'missing-0.1.3-setup.exe', '0.1.3')

    artifact_dir = guard.ScenarioArtifactDir(tmp_path / 'logs')
    log_path = artifact_dir.path(r'cross\nsis/to-msi', 'operator-smoke')
    assert log_path.parent.name == 'cross-nsis-to-msi'
    assert log_path.name == 'operator-smoke.log'
    assert log_path.parent.exists()


def test_windows_installer_identity_inventory_shortcuts_parses_single_dict_and_missing_targets(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    existing_target = tmp_path / 'token.place.exe'
    existing_target.write_text('exe', encoding='utf-8')
    payload = {
        'Shortcut': str(tmp_path / 'Token Place.lnk'),
        'Target': str(existing_target),
        'ResolvedTarget': str(existing_target),
        'Exists': True,
    }
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed(json.dumps(payload)))

    inventory = guard.inventory_shortcuts()

    assert inventory.shortcuts == [guard.Shortcut(tmp_path / 'Token Place.lnk', existing_target)]
    assert inventory.distinct_existing_targets == [existing_target.resolve()]
    assert inventory.missing_targets == []

    missing_target = tmp_path / 'removed' / 'token.place.exe'
    payload = [
        {'Shortcut': str(tmp_path / 'missing.lnk'), 'Target': str(missing_target), 'ResolvedTarget': str(missing_target), 'Exists': False},
        {'Shortcut': str(tmp_path / 'blank.lnk'), 'Target': '', 'ResolvedTarget': '', 'Exists': False},
    ]
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed(json.dumps(payload)))

    inventory = guard.inventory_shortcuts()

    assert inventory.shortcuts == [guard.Shortcut(tmp_path / 'missing.lnk', missing_target)]
    assert inventory.existing_targets == []
    assert inventory.missing_targets == [missing_target]


def test_windows_installer_identity_registry_inventory_and_authority_signature(monkeypatch) -> None:
    guard = _load_windows_installer_identity()
    payload = {
        'KeyPath': r'HKLM:\Software\TokenPlace',
        'DisplayName': 'token.place desktop',
        'UninstallString': r'"C:\Program Files\token.place\uninstall.exe"',
        'QuietUninstallString': r'"C:\Program Files\token.place\uninstall.exe" /S',
        'WindowsInstaller': False,
        'ProductCode': '',
    }
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: _process_completed(json.dumps([payload, {'DisplayName': ''}])))

    entries = guard.inventory_registry_entries()

    assert entries == [
        guard.RegistryEntry(
            key_path=r'HKLM:\Software\TokenPlace',
            display_name='token.place desktop',
            uninstall_string=r'"C:\Program Files\token.place\uninstall.exe"',
            quiet_uninstall_string=r'"C:\Program Files\token.place\uninstall.exe" /S',
            windows_installer=False,
            product_code='',
        )
    ]
    snapshot = guard.AuthoritySnapshot(
        shortcuts=guard.ShortcutInventory(
            shortcuts=[guard.Shortcut(Path('C:/Users/Public/Desktop/token.place.lnk'), Path('C:/Program Files/token.place/token.place.exe'))],
            existing_targets=[Path('C:/Program Files/TOKEN.PLACE/token.place.exe')],
            missing_targets=[Path('C:/stale/token.place.exe')],
        ),
        registry=entries,
    )
    signature = guard._authority_signature(snapshot)
    assert 'c:/users/public/desktop/token.place.lnk' in signature[0][0][0]
    assert 'c:/stale/token.place.exe' in signature[1][0]
    assert 'hklm:\\software\\tokenplace' in signature[3][0][0]


def test_windows_installer_identity_install_and_run_log_failure_contract(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    nsis = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64-setup.exe', 'nsis', '0.1.3')
    msi = guard.Installer(tmp_path / 'token.place-desktop-0.1.3-x64.msi', 'msi', '0.1.3')
    for installer in (nsis, msi):
        installer.path.write_text('installer', encoding='utf-8')
    calls = []
    original_run = guard._run

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if kwargs.get('log_path') is not None:
            kwargs['log_path'].write_text(f"$ {cmd[0]}\nexit=0\nok", encoding='utf-8')
        return subprocess.CompletedProcess(cmd, 0, 'ok')

    monkeypatch.setattr(guard, '_run', fake_run)

    guard.install(nsis, tmp_path / 'nsis.log')
    guard.install(msi, tmp_path / 'msi.log')

    assert calls[0][0] == [str(nsis.path), '/S']
    assert calls[1][0] == [guard._msiexec(), '/i', str(msi.path), '/qn', '/norestart']
    assert (tmp_path / 'nsis.log').read_text(encoding='utf-8').endswith('ok')

    def failing_subprocess(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 9, 'boom')

    monkeypatch.setattr(guard.subprocess, 'run', failing_subprocess)
    with pytest.raises(guard.InstallerIdentityError, match='command failed'):
        original_run(['bad-command'], log_path=tmp_path / 'failed.log')
    assert 'exit=9' in (tmp_path / 'failed.log').read_text(encoding='utf-8')


def test_windows_installer_identity_platform_helpers_and_noop_paths(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    assert guard._powershell().endswith('Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
    assert guard._msiexec().endswith('Windows/System32/msiexec.exe')

    monkeypatch.setattr(guard.sys, 'platform', 'linux')
    calls: list[object] = []
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: calls.append((args, kwargs)))
    guard._terminate_processes()
    guard.uninstall_best_effort()
    assert calls == []


def test_windows_installer_identity_terminate_processes_runs_stop_and_verify(monkeypatch) -> None:
    guard = _load_windows_installer_identity()
    calls: list[list[str]] = []

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, '_powershell', lambda: 'powershell.exe')
    monkeypatch.setattr(guard.time, 'sleep', lambda seconds: None)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '')

    monkeypatch.setattr(guard, '_run', fake_run)
    guard._terminate_processes()

    assert len(calls) == 2
    assert 'Stop-Process -Force' in calls[0][-1]
    assert 'exit 9' in calls[1][-1]


def test_windows_installer_identity_canonical_path_falls_back_on_resolve_error(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    class BrokenPath(type(Path())):
        def resolve(self, *args, **kwargs):
            raise OSError('synthetic resolve failure')

    broken = BrokenPath(tmp_path / 'Token.Place.EXE')
    assert guard._canonical_path(broken).endswith('token.place.exe')


def test_windows_installer_identity_resolve_authority_zero_and_missing_targets(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    monkeypatch.setattr(guard, 'inventory_shortcuts', lambda: guard.ShortcutInventory([], [], []))
    with pytest.raises(guard.InstallerIdentityError, match='found 0'):
        guard.resolve_authoritative_shortcut()

    missing = tmp_path / 'missing.exe'
    monkeypatch.setattr(
        guard,
        'inventory_shortcuts',
        lambda: guard.ShortcutInventory([guard.Shortcut(tmp_path / 'app.lnk', missing)], [], [missing]),
    )
    with pytest.raises(guard.InstallerIdentityError, match='missing/stale'):
        guard.resolve_authoritative_shortcut()

    monkeypatch.setattr(
        guard,
        'inventory_shortcuts',
        lambda: guard.ShortcutInventory([guard.Shortcut(tmp_path / 'app.lnk', missing)], [], []),
    )
    with pytest.raises(guard.InstallerIdentityError, match='zero existing'):
        guard.resolve_authoritative_shortcut()


def test_windows_installer_identity_uninstall_best_effort_rejects_bad_exit(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    entry = guard.RegistryEntry('key', 'token.place desktop', str(tmp_path / 'uninstall.exe'), '', False, '')
    snapshot = guard.AuthoritySnapshot(guard.ShortcutInventory([], [], []), [entry])
    calls: list[list[str]] = []

    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, 'capture_authority_snapshot', lambda: snapshot)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 99, 'bad uninstall')

    monkeypatch.setattr(guard, '_run', fake_run)
    with pytest.raises(guard.InstallerIdentityError, match='uninstaller exit 99'):
        guard.uninstall_best_effort(tmp_path / 'uninstall.log')
    assert calls[0][-1] == '/S'


def test_windows_installer_identity_verify_process_authority_helper(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    target = tmp_path / 'token.place.exe'
    monkeypatch.setattr(guard, '_processes_running_targets', lambda targets: [str(target)])
    with pytest.raises(guard.InstallerIdentityError, match='process authority remains'):
        guard._verify_no_authority_processes([target])

    monkeypatch.setattr(guard, '_processes_running_targets', lambda targets: [])
    guard._verify_no_authority_processes([target])


def test_windows_installer_identity_runtime_recursive_lookup_and_missing_runtime(tmp_path) -> None:
    guard = _load_windows_installer_identity()
    exe_dir = tmp_path / 'extract'
    nested_runtime = exe_dir / 'deep' / 'resources' / 'python-runtime'
    nested_runtime.mkdir(parents=True)
    exe = exe_dir / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    (nested_runtime / 'python.exe').write_text('python', encoding='utf-8')
    (nested_runtime / guard.RUNTIME_PROVENANCE_NAME).write_text(json.dumps({'build_profile': guard.EXPECTED_RUNTIME_ID}), encoding='utf-8')
    guard._assert_runtime(exe)

    (nested_runtime / 'python.exe').unlink()
    with pytest.raises(guard.InstallerIdentityError, match='python-runtime/python.exe'):
        guard._assert_runtime(exe)


def test_windows_installer_identity_probe_and_launch_failure_edges(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    outputs = iter([
        subprocess.CompletedProcess([str(exe)], 0, 'not json 0.1.3 abcdef123456'),
    ])
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: next(outputs))
    assert guard.probe_identity(exe, {}, '0.1.3', 'abcdef123456')['raw'].startswith('not json')

    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: subprocess.CompletedProcess([str(exe)], 1, 'boom from smoke'))
    with pytest.raises(guard.InstallerIdentityError, match='operator-start preflight launch failed'):
        guard.launch_for_operator_record(exe, {})

    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: subprocess.CompletedProcess([str(exe)], 0, 'wrong version'))
    with pytest.raises(guard.InstallerIdentityError, match='did not report expected'):
        guard.probe_identity(exe, {}, '0.1.3', 'abcdef123456')


def test_windows_installer_identity_operator_record_rejects_readiness_and_runtime_mutation() -> None:
    guard = _load_windows_installer_identity()
    base = {
        'record': 'desktop.compute_node.session.layout',
        'operator_start_preflight': 'ok',
        'resource_context_source': 'tauri_app_handle',
        'bridge_child_spawned': True,
        'bridge_event_received': True,
        'launcher_source': 'bundled',
        'interpreter_basename': 'python.exe',
        'runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
        'bridge_preflight': 'ok',
        'model_artifact_inspect': 'ok',
        'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
        'selected_model_profile': 'qwen3-8b-q4',
        'context_tier': '64k-full',
        'effective_n_ctx': 65536,
        'n_ctx': 65536,
        'fallback_reason': None,
        'backend_fallback': False,
        'model_fallback': False,
        'context_fallback': False,
        'api_v1_readiness_yarn_requested_context_tokens': 65536,
        'api_v1_readiness_yarn_rope_supported': True,
        'startup_phase': 'ready',
        'startup_result': 'ready',
        'startup_deadline_ms': 300000,
    }
    with pytest.raises(guard.InstallerIdentityError, match='bridge-command preflight'):
        guard.assert_operator_record(json.dumps({**base, 'bridge_preflight': 'missing'}), expected_tier='64k-full')
    with pytest.raises(guard.InstallerIdentityError, match='model artifact inspection'):
        guard.assert_operator_record(json.dumps({**base, 'model_artifact_inspect': 'missing'}), expected_tier='64k-full')
    for unsafe_filename in [
        'C:/Users/operator/model.gguf',
        r'C:\Users\operator\model.gguf',
        '/home/operator/model.gguf',
        'qwen3.bin',
        'other-safe-model.gguf',
    ]:
        with pytest.raises(guard.InstallerIdentityError, match='safe model artifact filename'):
            guard.assert_operator_record(
                json.dumps({**base, 'model_artifact_filename': unsafe_filename}),
                expected_tier='64k-full',
            )
    accepted = guard.assert_operator_record(json.dumps(base), expected_tier='64k-full')
    assert accepted['model_artifact_filename'] == 'Qwen3-8B-Q4_K_M.gguf'
    assert 'C:' not in json.dumps(accepted)
    assert '/home/' not in json.dumps(accepted)
    with pytest.raises(guard.InstallerIdentityError, match='Qwen3'):
        guard.assert_operator_record(json.dumps({**base, 'selected_model_profile': 'other'}), expected_tier='64k-full')
    with pytest.raises(guard.InstallerIdentityError, match='bounded ready'):
        guard.assert_operator_record(json.dumps({**base, 'startup_phase': 'provisioning', 'startup_result': 'ready', 'startup_deadline_ms': 1}), expected_tier='64k-full')
    with pytest.raises(guard.InstallerIdentityError, match='controlled ready field|ready or a terminal'):
        guard.assert_operator_record(json.dumps({**base, 'startup_phase': 'starting', 'startup_result': 'unknown', 'startup_deadline_ms': 1}), expected_tier='64k-full')
    with pytest.raises(guard.InstallerIdentityError, match='runtime mutation'):
        guard.assert_operator_record(
            json.dumps({**base, 'startup_phase': 'ready', 'startup_result': 'ready', 'startup_deadline_ms': 1, 'runtime_action': 'failed'}),
            expected_tier='64k-full',
            launch_number=2,
        )


def test_windows_installer_identity_validate_tiers_detects_runtime_and_profile_drift(monkeypatch, tmp_path) -> None:
    guard = _load_windows_installer_identity()
    exe = tmp_path / 'token.place.exe'
    exe.write_text('exe', encoding='utf-8')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setattr(guard, 'capture_installed_resource_manifest', lambda target: guard.InstalledResourceManifest(()))
    monkeypatch.setattr(guard, 'assert_manifest_unchanged', lambda before, after, phase: None)
    monkeypatch.setattr(guard, 'verify_config_preserved', lambda config_path, expected: None)

    launches = 0
    def fake_launch_runtime_drift(path, env, log_path=None):
        nonlocal launches
        launches += 1
        tier = json.loads((tmp_path / 'Roaming' / guard.TAURI_IDENTIFIER / guard.CONFIG_NAME).read_text(encoding='utf-8'))['context_tier']
        n_ctx = 65536 if tier == '64k-full' else 8192
        return json.dumps({
            'record': 'desktop.compute_node.session.layout',
            'operator_start_preflight': 'ok',
            'resource_context_source': 'tauri_app_handle',
            'bridge_child_spawned': True,
            'bridge_event_received': True,
            'launcher_source': 'bundled',
            'interpreter_basename': 'python.exe',
            'runtime_id': 'different-runtime' if launches == 2 else guard.EXPECTED_RUNTIME_ID,
            'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
            'bridge_preflight': 'ok',
            'model_artifact_inspect': 'ok',
            'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
            'startup_result': 'ready',
            'selected_model_profile': 'qwen3-8b-q4',
            'model_profile_identifier': 'qwen3-8b-q4-k-m',
            'context_tier': tier,
            'effective_n_ctx': n_ctx,
            'n_ctx': n_ctx,
            'startup_phase': 'ready',
            'startup_result': 'ready',
            'startup_deadline_ms': 1,
            'fallback_reason': None,
            'backend_fallback': False,
            'model_fallback': False,
            'context_fallback': False,
            'api_v1_readiness_yarn_requested_context_tokens': n_ctx,
            'api_v1_readiness_yarn_rope_supported': True,
        })

    monkeypatch.setattr(guard, 'launch_for_operator_record', fake_launch_runtime_drift)
    with pytest.raises(guard.InstallerIdentityError, match='runtime_id|installed bundled runtime|changed bundled runtime identity'):
        guard.validate_installed_context_tiers(exe, {}, None, 'scenario')

    launches = 0
    def fake_launch_profile_drift(path, env, log_path=None):
        nonlocal launches
        launches += 1
        tier = json.loads((tmp_path / 'Roaming' / guard.TAURI_IDENTIFIER / guard.CONFIG_NAME).read_text(encoding='utf-8'))['context_tier']
        n_ctx = 65536 if tier == '64k-full' else 8192
        return json.dumps({
            'record': 'desktop.compute_node.session.layout',
            'operator_start_preflight': 'ok',
            'resource_context_source': 'tauri_app_handle',
            'bridge_child_spawned': True,
            'bridge_event_received': True,
            'launcher_source': 'bundled',
            'interpreter_basename': 'python.exe',
            'runtime_id': guard.EXPECTED_RUNTIME_ID,
            'bundled_runtime_id': guard.EXPECTED_RUNTIME_ID,
            'bridge_preflight': 'ok',
            'model_artifact_inspect': 'ok',
            'model_artifact_filename': 'Qwen3-8B-Q4_K_M.gguf',
            'startup_result': 'ready',
            'selected_model_profile': 'qwen3-8b-q4',
            'model_profile_identifier': 'changed-profile' if launches == 2 else 'qwen3-8b-q4-k-m',
            'context_tier': tier,
            'effective_n_ctx': n_ctx,
            'n_ctx': n_ctx,
            'startup_phase': 'ready',
            'startup_result': 'ready',
            'startup_deadline_ms': 1,
            'fallback_reason': None,
            'backend_fallback': False,
            'model_fallback': False,
            'context_fallback': False,
            'api_v1_readiness_yarn_requested_context_tokens': n_ctx,
            'api_v1_readiness_yarn_rope_supported': True,
        })

    monkeypatch.setattr(guard, 'launch_for_operator_record', fake_launch_profile_drift)
    with pytest.raises(guard.InstallerIdentityError, match='changed canonical model profile'):
        guard.validate_installed_context_tiers(exe, {}, None, 'scenario')


def test_windows_installer_identity_run_all_and_main_windows_paths(monkeypatch, tmp_path, capsys) -> None:
    guard = _load_windows_installer_identity()
    current_nsis = tmp_path / 'token.place-desktop-0.1.5-x64-setup.exe'
    current_msi = tmp_path / 'token.place-desktop-0.1.5-x64.msi'
    previous_nsis = tmp_path / 'token.place-desktop-0.1.4-x64-setup.exe'
    previous_msi = tmp_path / 'token.place-desktop-0.1.4-x64.msi'
    for path in (current_nsis, current_msi, previous_nsis, previous_msi):
        path.write_text('artifact', encoding='utf-8')

    scenarios = [guard.Scenario('clean-nsis-0.1.5', guard.Installer(current_nsis, 'nsis', '0.1.5'))]
    artifacts_seen = []
    def fake_runner(scenario, build_id):
        artifacts_seen.append((scenario.name, build_id))

    guard.run_all_scenarios(scenarios, 'abcdef123456', runner=fake_runner, artifact_root=tmp_path / 'logs')
    assert artifacts_seen == [('clean-nsis-0.1.5', 'abcdef123456')]

    old_argv = sys.argv
    monkeypatch.setattr(guard.sys, 'platform', 'win32')
    monkeypatch.setattr(guard, 'run_all_scenarios', lambda scenarios, expected_build_id, artifact_root=None: None)
    try:
        sys.argv = [
            'prog',
            '--windows-nsis', str(current_nsis),
            '--windows-msi', str(current_msi),
            '--previous-windows-nsis', str(previous_nsis),
            '--previous-windows-msi', str(previous_msi),
            '--expected-build-id', 'abcdef123456',
            '--artifact-dir', str(tmp_path / 'artifacts'),
        ]
        assert guard.main() == 0
    finally:
        sys.argv = old_argv
    output = capsys.readouterr().out
    assert 'validated 6 clean/upgrade Windows installer scenarios' in output
    assert 'CUDA/GPU execution was not validated' in output
