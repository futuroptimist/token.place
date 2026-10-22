"""Contracts for the shipped, early cross-format installer refusal."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "desktop-tauri/src-tauri/installers"
MESSAGE = "TOKEN_PLACE_COMPETING_INSTALLER: token.place desktop. Uninstall the existing version first"


def load_guard():
    spec = importlib.util.spec_from_file_location("installer_refusal_guard", ROOT / "desktop-tauri/scripts/test_windows_installer_identity.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_nsis_ui_notifications_wait_for_maintenance_and_owned_uninstaller():
    spec = importlib.util.spec_from_file_location("coexistence_guard", ROOT / "desktop-tauri/scripts/windows_installer_ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    def control(identifier, text, kind="Button", **extra):
        return {"id": identifier, "hwnd": identifier, "text": text, "class": kind, "visible": True, "enabled": True, **extra}

    sent = set()
    options = dict(uninstaller=False, fresh=False, directory_selected=False, owner_observed=False, selected_uninstall=False, sent=sent)
    next_button = control(1, "&Next >")
    welcome = [control(1201, "Welcome to token.place desktop Setup", "Static"), next_button]
    action = ui.nsis_ui_action(welcome, **options)
    assert action[0] == "welcome:Next >"
    sent.add(action[0])
    assert ui.nsis_ui_action(welcome, **options) is None
    maintenance = [control(1201, "nested control with colliding ID"), control(1037, "Already Installed", "Static"), next_button]
    assert ui.nsis_ui_action(maintenance, **options) is None
    radios = [control(10, "Reinstall", style=9, checked=1), control(11, "Uninstall", style=9, checked=0)]
    maintenance += radios
    action = ui.nsis_ui_action(maintenance, **options)
    assert action[0] == "select-uninstall" and action[1] is radios[1]
    sent.add(action[0])
    assert ui.nsis_ui_action(maintenance, **options) is None
    radios[0]["checked"], radios[1]["checked"] = 0, 1
    action = ui.nsis_ui_action(maintenance, **options)
    assert action[0] == "maintenance:Next >"
    sent.add(action[0])
    assert ui.nsis_ui_action(maintenance, **options) is None
    finish = [control(1201, "Completing Setup", "Static"), control(1, "&Finish")]
    assert ui.nsis_ui_action(finish, **options) is None
    options["selected_uninstall"] = True
    assert ui.nsis_ui_action(finish, **options) is None
    options["owner_observed"] = True
    action = ui.nsis_ui_action(finish, **options)
    assert action[0] == "Completing Setup:Finish"
    sent.add(action[0])
    assert ui.nsis_ui_action(finish, **options) is None
    options.update(fresh=True, owner_observed=False, selected_uninstall=False, sent=set())
    assert ui.nsis_ui_action(finish, **options) is None
    options["directory_selected"] = True
    assert ui.nsis_ui_action(finish, **options)[0] == "Completing Setup:Finish"
    finish[1]["enabled"] = False
    assert ui.nsis_ui_action(finish, **options) is None
    options.update(uninstaller=True, sent=set())
    uninstall = control(1, "&Uninstall")
    assert ui.nsis_ui_action([uninstall], **options)[0] == "uninstaller:Uninstall"
    options["sent"].add("uninstaller:Uninstall")
    assert ui.nsis_ui_action([uninstall], **options) is None
    assert ui.nsis_ui_action([control(1, "Close")], **options)[0] == "uninstaller:Close"


def test_static_extraction_waits_for_real_nsis_uninstaller(monkeypatch, tmp_path):
    from scripts import validate_windows_desktop_release_artifacts as validator
    root = tmp_path / "extraction with spaces"
    root.mkdir()
    uninstaller = root / "uninstall.exe"
    uninstaller.touch()
    calls = []

    def run(command, **kwargs):
        assert root.is_dir()
        assert command == subprocess.list2cmdline([str(uninstaller.resolve()), '/S']) + f' _?={root.resolve()}'
        assert not kwargs.get('shell', False)
        assert kwargs['check'] is True
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '')

    monkeypatch.setattr(validator.subprocess, 'run', run)
    validator._cleanup_nsis_install(root)
    assert len(calls) == 1
    # Caller owns the temporary tree and removes it only after synchronous exit.
    assert root.is_dir()

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(7, command, output='cleanup failed')

    monkeypatch.setattr(validator.subprocess, 'run', fail)
    with pytest.raises(validator.ValidationError, match='failed to clean NSIS install'):
        validator._cleanup_nsis_install(root)
    assert root.is_dir()


def test_custom_templates_preserve_pinned_upstream_outside_refusal_blocks():
    metadata = json.loads((TEMPLATES / "upstream.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "desktop-tauri/package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/@tauri-apps/cli"]["version"] == metadata["tauriCliVersion"], "Rebase the custom installer templates when upgrading Tauri CLI"
    for name, digest in metadata["templates"].items():
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        # Reverse the reviewed patch in memory and verify the pinned upstream
        # digest. Every local template change must have a corresponding hunk.
        patch = (TEMPLATES / "template-changes.patch").read_text(encoding="utf-8")
        section = patch.split(f"+++ b/{name}\n", 1)[1].split("--- a/", 1)[0]
        lines = text.splitlines(keepends=True)
        original, cursor = [], 0
        for hunk in re.split(r"(?m)^@@ ", section)[1:]:
            header, body = hunk.split("\n", 1)
            match = re.match(r"-\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", header)
            start = int(match.group(1)) - (0 if match.group(2) == "0" else 1)
            original.extend(lines[cursor:start])
            cursor = start
            for line in body.splitlines(keepends=True):
                if line.startswith((" ", "+")):
                    assert lines[cursor] == line[1:], (name, cursor)
                    cursor += 1
                if line.startswith((" ", "-")):
                    original.append(line[1:])
        original.extend(lines[cursor:])
        text = "".join(original)
        assert hashlib.sha256(text.encode()).hexdigest() == digest, name
    config = json.loads((ROOT / "desktop-tauri/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["version"] == "0.1.25"  # Explicitly approved unreleased release train.
    assert config["bundle"]["windows"]["nsis"]["template"] == "installers/windows.nsi"
    assert config["bundle"]["windows"]["wix"]["template"] == "installers/windows.wxs"


@pytest.mark.parametrize("name", ["windows.nsi", "windows.wxs"])
@pytest.mark.parametrize("header_line", [0, 1])
def test_template_provenance_rejects_changed_headers(monkeypatch, name, header_line):
    read_text = Path.read_text

    def altered_header(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path == TEMPLATES / name:
            lines = text.splitlines(keepends=True)
            lines[header_line] = "unaudited header change\n"
            return "".join(lines)
        return text

    monkeypatch.setattr(Path, "read_text", altered_header)
    with pytest.raises(AssertionError):
        test_custom_templates_preserve_pinned_upstream_outside_refusal_blocks()


def test_nsis_checks_ownership_before_reinstall_and_copy():
    text = (TEMPLATES / "windows.nsi").read_text(encoding="utf-8")
    for section, end in (("Function .onInit", "FunctionEnd"), ("Function PageReinstall", "FunctionEnd"), ("Section Install", "SectionEnd")):
        body = text.split(section + "\n", 1)[1].split(end, 1)[0]
        assert ("Call TokenPlaceVerifyOwnership" if section == "Section Install" else "Call TokenPlacePreviewOwnership") in body
        if "SetOutPath" in body:
            assert body.index("Call TokenPlaceVerifyOwnership") < body.index("SetOutPath")
    assert "$WixMode" not in text
    assert "msiexec" not in text
    assert '''StrCpy $R1 '"$INSTDIR\\uninstall.exe" /UPDATE' '''.strip() in text
    assert "TOKEN_PLACE_INTERRUPTED_UPGRADE" in text
    assert "DeleteAppDataCheckbox" not in text
    assert 'RmDir /r "$APPDATA' not in text
    assert 'RmDir /r "$LOCALAPPDATA' not in text


def test_msi_verifies_final_target_before_major_upgrade():
    text = (TEMPLATES / "windows.wxs").read_text(encoding="utf-8")
    block = text.split("<!-- TOKEN_PLACE_GUARD_BEGIN -->", 1)[1].split("<!-- TOKEN_PLACE_GUARD_END -->", 1)[0]
    root = ET.fromstring("<Product>" + block + "</Product>")
    sequence = root.find("InstallExecuteSequence")
    assert sequence.find("Custom[@Action='TokenPlaceVerify']").attrib["Before"] == "InstallInitialize"
    assert sequence.find("Custom[@Action='TokenPlaceResolve']").attrib["Before"] == "CostInitialize"
    assert "PrevInstallDirNoName" not in text
    assert "PrevInstallDirWithName" not in text
    assert '<MajorUpgrade Schedule="afterInstallInitialize"' in text
    source = (TEMPLATES / "ownership.cpp").read_text(encoding="utf-8")
    assert "MsiGetTargetPathW" in source
    assert "MsiEnumRelatedProductsW" in source
    resolve = source.split('UINT __stdcall TokenPlaceResolve', 1)[1].split('UINT __stdcall TokenPlaceVerify', 1)[0]
    assert resolve.index('verify_requested_upgrade_target(requested, own.root)') < resolve.index('MsiSetPropertyW')
    assert "GetFinalPathNameByHandleW" in source
    assert "HKEY_USERS" not in source
    assert "RegLoad" not in source
    config = json.loads((ROOT / "desktop-tauri/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    expected = config["bundle"]["windows"]["wix"].get("upgradeCode") or str(uuid.uuid5(uuid.NAMESPACE_DNS, config["productName"] + ".exe.app.x64"))
    assert "{" + expected.strip("{}").upper() + "}" in source


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le"])
def test_msi_silent_diagnostic_only_exposes_bounded_refusal_message(encoding):
    guard = load_guard()
    data = ("private irrelevant diagnostic\n" + MESSAGE + "\n").encode(encoding)
    diagnostic = guard.msi_rejection_diagnostic(data)
    assert MESSAGE in diagnostic
    assert "private" not in diagnostic
    assert guard.is_actionable_competing_installer_rejection(subprocess.CompletedProcess([], 1603, diagnostic))
    assert not guard.is_actionable_competing_installer_rejection(subprocess.CompletedProcess([], 0, diagnostic))
    assert not guard.is_actionable_competing_installer_rejection(subprocess.CompletedProcess([], 1603, "remove token.place existing installation"))


@pytest.mark.parametrize("persistent_log", [False, True])
def test_msi_install_retains_native_failure_and_guidance(monkeypatch, tmp_path, persistent_log):
    guard = load_guard()
    def run(command, **kwargs):
        native_log = Path(command[command.index("/L*V") + 1])
        native_log.write_text(MESSAGE, encoding="utf-16")
        if kwargs["log_path"]:
            kwargs["log_path"].write_text("exit=1603\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1603, "")
    monkeypatch.setattr(guard, "_run", run)
    log = tmp_path / "install.log" if persistent_log else None
    result = guard.install(guard.Installer(tmp_path / "candidate.msi", "msi", "0.1.25"), log, install_root=tmp_path)
    assert result.returncode == 1603
    assert MESSAGE in result.stdout
    if log:
        assert MESSAGE in log.read_text(encoding="utf-8")
        assert log.with_suffix(".msi.log").is_file()


@pytest.mark.parametrize("payload", [{"ownershipPreflight": True, "rows": []}, [], {}])
def test_msi_location_searches_require_verified_guard_or_actual_searches(monkeypatch, tmp_path, payload):
    guard = load_guard()
    monkeypatch.setattr(guard, "_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(payload)))
    package = guard.Installer(tmp_path / "candidate.msi", "msi", "0.1.25")
    if payload == {"ownershipPreflight": True, "rows": []}:
        assert guard.msi_location_searches(package) == []
    else:
        with pytest.raises(guard.InstallerIdentityError, match="no supported INSTALLDIR registry searches"):
            guard.msi_location_searches(package)


@pytest.mark.parametrize("target_count", [0, 2])
def test_refusal_preservation_rejects_ambiguous_authority(tmp_path, target_count):
    guard = load_guard()
    targets = [tmp_path / str(index) / "app.exe" for index in range(target_count)]
    authority = guard.AuthoritySnapshot(guard.ShortcutInventory([], targets, []), [])
    with pytest.raises(guard.InstallerIdentityError, match="ambiguous installation"):
        guard.capture_rejection_preservation(authority, tmp_path / "config", tmp_path / "model")


@pytest.mark.parametrize("missing", ["installed", "settings", "models"])
def test_refusal_preservation_rejects_missing_root(tmp_path, missing):
    guard = load_guard()
    roots = {label: tmp_path / label for label in ("installed", "settings", "models")}
    for label, root in roots.items():
        if label != missing:
            root.mkdir()
    exe = roots["installed"] / "app.exe"
    authority = guard.AuthoritySnapshot(guard.ShortcutInventory([], [exe], []), [])
    with pytest.raises(guard.InstallerIdentityError, match=f"root is missing: {missing}"):
        guard.capture_rejection_preservation(authority, roots["settings"] / "config", roots["models"] / "model")


@pytest.mark.parametrize("changed", ["installed", "settings", "models", "added", "deleted", "directory"])
def test_refusal_preservation_detects_content_mutation_even_with_same_size_and_time(tmp_path, changed):
    guard = load_guard()
    roots = {label: tmp_path / label for label in ("installed", "settings", "models")}
    files = {}
    for label, root in roots.items():
        root.mkdir()
        files[label] = root / "preserve.bin"
        files[label].write_bytes(b"original")
    exe = files["installed"]
    (roots["settings"] / "empty-folder").mkdir()
    authority = guard.AuthoritySnapshot(guard.ShortcutInventory([guard.Shortcut(tmp_path / "app.lnk", exe)], [exe], []), [])
    before = guard.capture_rejection_preservation(authority, files["settings"], files["models"])
    assert before == guard.capture_rejection_preservation(authority, files["settings"], files["models"])
    if changed == "added":
        (roots["installed"] / "new.bin").touch()
    elif changed == "deleted":
        files["models"].unlink()
    elif changed == "directory":
        (roots["settings"] / "empty-folder").rmdir()
    else:
        path = files[changed]
        stat = path.stat()
        path.write_bytes(b"modified")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = guard.capture_rejection_preservation(authority, files["settings"], files["models"])
    assert before != after


def test_hosted_ci_checks_refusals_and_same_format_upgrades():
    text = (ROOT / ".github/workflows/desktop-operator-e2e.yml").read_text(encoding="utf-8")
    assert text.count("'desktop-tauri/src-tauri/installers/**'") == 2
    assert text.count("'tests/unit/test_windows_installer_refusal.py'") == 2
    assert "python -m pytest -q -p no:cacheprovider tests/unit/test_windows_installer_refusal.py" in text
    assert "@('upgrade-nsis-to-nsis', 'upgrade-msi-to-msi', 'cross-nsis-to-msi', 'cross-msi-to-nsis')" in text
    assert 'Upload current-package validation and preservation evidence\n        if: always()' in text



@pytest.mark.parametrize("outcome", ["preserved", "mutated", "success"])
def test_scenario_accepts_only_refusal_with_preserved_state(monkeypatch, tmp_path, outcome):
    guard = load_guard()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(guard, "durable_install_parent", lambda: tmp_path.resolve())
    monkeypatch.setattr(guard, "_terminate_processes", lambda: None)
    monkeypatch.setattr(guard, "uninstall_best_effort", lambda *args: None)
    monkeypatch.setattr(guard, "verify_no_authority_remains", lambda: None)
    monkeypatch.setattr(guard, "reset_msi_remembered_locations", lambda *args: None)
    exe = tmp_path / "existing.exe"
    exe.touch()
    shortcut = guard.Shortcut(tmp_path / "existing.lnk", exe)
    authority = guard.AuthoritySnapshot(guard.ShortcutInventory([shortcut], [exe], []), [])
    monkeypatch.setattr(guard, "capture_authority_snapshot", lambda: authority)
    monkeypatch.setattr(guard, "verify_installed_authority", lambda *args, **kwargs: shortcut)
    snapshots = iter([{"owned": "original"}, {"owned": "changed" if outcome == "mutated" else "original"}])
    monkeypatch.setattr(guard, "capture_rejection_preservation", lambda *args: next(snapshots))
    def install(installer, *args, **kwargs):
        code = 1603 if installer.kind == "msi" and outcome != "success" else 0
        return subprocess.CompletedProcess([], code, MESSAGE)
    monkeypatch.setattr(guard, "install", install)
    scenario = guard.Scenario("cross-nsis-to-msi", guard.Installer(tmp_path / "new.msi", "msi", "0.1.25"), guard.Installer(tmp_path / "old.exe", "nsis", "0.1.24"))
    artifacts = guard.ScenarioArtifactDir(tmp_path / "logs")
    if outcome == "preserved":
        guard.run_scenario(scenario, "abcdef123456", artifacts)
        before = json.loads(artifacts.path(scenario.name, "preservation-before").with_suffix(".json").read_text(encoding="utf-8"))
        after = json.loads(artifacts.path(scenario.name, "preservation-after").with_suffix(".json").read_text(encoding="utf-8"))
        assert before == after == {"owned": "original"}
    else:
        message = "changed installed files" if outcome == "mutated" else "must refuse"
        with pytest.raises(guard.InstallerIdentityError, match=message):
            guard.run_scenario(scenario, "abcdef123456", artifacts)
