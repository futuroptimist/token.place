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
    assert '"PYTHONPATH": str(app_path / "Contents" / "Resources" / "python")' in text
    assert 'xcode-select' in text
    assert 'otool' in text
    assert 'embedded runtime probe did not report Metal GPU offload' in text


def test_validator_uses_packaged_python_resources_for_runtime_probe() -> None:
    text = Path('scripts/validate_desktop_tauri_release_artifacts.py').read_text(encoding='utf-8')
    assert 'PYTHONPATH": str(app_path / "Contents" / "Resources" / "python")' in text
    assert "Path.cwd() / 'src-tauri' / 'python'" not in text
    assert "qwen_64k_yarn_support" in text
    assert "model_bridge.py" in text
    assert "'inspect'" in text


def test_validator_sanitized_python_env_unsets_override_variables(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = tmp_path / 'python3'
    py.write_text('#!/bin/sh\n', encoding='utf-8')
    captured = {}

    def fake_run(cmd, *, check, capture_output, text, env):
        captured['env'] = env
        return subprocess.CompletedProcess(cmd, 0, 'ok', '')

    monkeypatch.setenv('TOKEN_PLACE_PYTHON', '/usr/bin/python3')
    monkeypatch.setenv('TOKEN_PLACE_SIDECAR_PYTHON', '/usr/bin/python3')
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    assert validator._run_python_sanitized(py, 'print(1)', app) == 'ok'
    assert captured['env']['PYTHONNOUSERSITE'] == '1'
    assert captured['env']['PATH'] == '/usr/bin:/bin'
    assert captured['env']['PYTHONPATH'] == str(app / 'Contents' / 'Resources' / 'python')
    assert 'TOKEN_PLACE_PYTHON' not in captured['env']
    assert 'TOKEN_PLACE_SIDECAR_PYTHON' not in captured['env']


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

    def fake_run(cmd, *, check, capture_output, text, env):
        assert env['HOME'] == str(created_home['path'])
        return subprocess.CompletedProcess(cmd, 0, '/usr/bin/python3 leaked', '')

    monkeypatch.setattr(validator.tempfile, 'mkdtemp', fake_mkdtemp)
    monkeypatch.setattr(validator.subprocess, 'run', fake_run)

    try:
        validator._run_python_sanitized(py, 'print(1)', app)
        assert False
    except SystemExit as exc:
        assert 'forbidden marker' in str(exc)
    assert not created_home['path'].exists()


def test_run_python_sanitized_formats_probe_failures_without_raw_code(monkeypatch, tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources' / 'python').mkdir(parents=True)
    py = tmp_path / 'python3'

    def fake_run(cmd, *, check, capture_output, text, env):
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
    monkeypatch.setattr(validator, '_run', lambda cmd: f'{binary}:\n/opt/homebrew/lib/libbad.dylib (compatibility version 1.0.0, current version 1.0.0)')

    try:
        validator._validate_macho_linkage(binary, app)
        assert False
    except SystemExit as exc:
        assert 'forbidden external Mach-O linkage' in str(exc)


def test_validate_embedded_python_runtime_fails_incomplete_app_before_publication(tmp_path) -> None:
    validator = _load_release_artifact_validator()
    app = tmp_path / 'token.place desktop.app'
    (app / 'Contents' / 'Resources').mkdir(parents=True)
    try:
        validator._validate_embedded_python_runtime(app)
        assert False
    except SystemExit as exc:
        assert 'embedded Python interpreter missing' in str(exc)
