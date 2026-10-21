"""Contracts for the shipped, early cross-format installer refusal."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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


def test_custom_templates_preserve_pinned_upstream_outside_refusal_blocks():
    metadata = json.loads((TEMPLATES / "upstream.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "desktop-tauri/package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/@tauri-apps/cli"]["version"] == metadata["tauriCliVersion"], "Rebase the custom installer templates when upgrading Tauri CLI"
    for name, digest in metadata["templates"].items():
        text = "".join((TEMPLATES / name).read_text(encoding="utf-8").splitlines(keepends=True)[2:])
        if name.endswith(".nsi"):
            text = re.sub(r"; TOKEN_PLACE_GUARD_BEGIN.*?; TOKEN_PLACE_GUARD_END\n\n", "", text, flags=re.S)
            text = re.sub(r"; TOKEN_PLACE_GUARD_CALL_BEGIN.*?; TOKEN_PLACE_GUARD_CALL_END\n", "", text, flags=re.S)
        else:
            text = re.sub(r"        <!-- TOKEN_PLACE_GUARD_BEGIN -->.*?        <!-- TOKEN_PLACE_GUARD_END -->\n\n", "", text, flags=re.S)
        assert hashlib.sha256(text.encode()).hexdigest() == digest, name
    config = json.loads((ROOT / "desktop-tauri/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["version"] == "0.1.25"  # Explicitly approved unreleased release train.
    assert config["bundle"]["windows"]["nsis"]["template"] == "installers/windows.nsi"
    assert config["bundle"]["windows"]["wix"]["template"] == "installers/windows.wxs"


def test_nsis_refuses_before_pages_sections_and_preserves_same_format_path():
    text = (TEMPLATES / "windows.nsi").read_text(encoding="utf-8")
    init = text.split("Function .onInit\n", 1)[1].split("FunctionEnd", 1)[0]
    assert init.index("Call TokenPlaceRefuseCompetingMsi") < init.index("${GetOptions}")
    block = text.split("; TOKEN_PLACE_GUARD_BEGIN", 1)[1].split("; TOKEN_PLACE_GUARD_END", 1)[0]
    for hive in ("HKCU", "HKLM"):
        for view in (32, 64):
            assert f"!insertmacro TokenPlaceFindMsi {hive} {view}" in block
    for discriminator in ('"DisplayName"', '"Publisher"', '"WindowsInstaller"'):
        assert discriminator in block
    assert 'IntCmp $R4 1 ' in block
    assert 'IfSilent tp_refusal_exit' in block
    assert 'MessageBox MB_OK|MB_ICONSTOP' in block
    assert 'SetErrorLevel 1638\n  Quit' in block
    assert 'Uninstall the existing version first' in block
    for mutation in ('WriteReg', 'Delete ', 'SetOutPath', 'ExecWait', 'NSISdl::'):
        assert mutation not in block


def test_msi_refusal_covers_scopes_views_and_allows_uninstall():
    text = (TEMPLATES / "windows.wxs").read_text(encoding="utf-8")
    block = text.split("<!-- TOKEN_PLACE_GUARD_BEGIN -->", 1)[1].split("<!-- TOKEN_PLACE_GUARD_END -->", 1)[0]
    root = ET.fromstring("<Product>" + block + "</Product>")
    properties = root.findall("Property")
    assert len(properties) == 4
    found = set()
    for prop in properties:
        assert prop.attrib["Secure"] == "yes"
        search = prop.find("RegistrySearch")
        found.add((search.attrib["Root"], search.attrib["Win64"]))
        assert search.attrib["Name"] == "UninstallString"
        assert search.attrib["Key"].endswith(r"Uninstall\{{product_name}}")
        assert f"NOT {prop.attrib['Id']}" in root.find("Condition").text
    assert found == {(hive, view) for hive in ("HKCU", "HKLM") for view in ("yes", "no")}
    condition = root.find("Condition")
    assert condition.text.startswith('REMOVE~="ALL" OR (')
    assert 'Uninstall the existing version first' in condition.attrib["Message"]
    assert text.index("TOKEN_PLACE_GUARD_BEGIN") < text.index("<MajorUpgrade")


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


@pytest.mark.parametrize("changed", ["installed", "settings", "models", "added", "deleted"])
def test_refusal_preservation_detects_content_mutation_even_with_same_size_and_time(tmp_path, changed):
    guard = load_guard()
    roots = {label: tmp_path / label for label in ("installed", "settings", "models")}
    files = {}
    for label, root in roots.items():
        root.mkdir()
        files[label] = root / "preserve.bin"
        files[label].write_bytes(b"original")
    exe = files["installed"]
    authority = guard.AuthoritySnapshot(guard.ShortcutInventory([guard.Shortcut(tmp_path / "app.lnk", exe)], [exe], []), [])
    before = guard.capture_rejection_preservation(authority, files["settings"], files["models"])
    assert before == guard.capture_rejection_preservation(authority, files["settings"], files["models"])
    if changed == "added":
        (roots["installed"] / "new.bin").touch()
    elif changed == "deleted":
        files["models"].unlink()
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
