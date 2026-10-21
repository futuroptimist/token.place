"""Disposable hosted-Windows mixed-ownership regressions; never run on a user device."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import test_windows_installer_identity as h


def tree(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                result[str(path.relative_to(root))] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def authority(root: Path, msi: bool) -> tuple:
    snapshot = h.capture_authority_snapshot()
    entries = [entry for entry in snapshot.registry if entry.windows_installer == msi]
    assert len(entries) == 1, "mixed scenario requires exactly one owner of each format"
    shortcuts = [(str(s.path), str(s.target)) for s in snapshot.shortcuts.shortcuts if s.target.parent.resolve() == root.resolve()]
    assert shortcuts and all(Path(target).is_file() for _, target in shortcuts)
    return entries, sorted(shortcuts)


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("nsis", "msi", "old-nsis", "old-msi", "version", "old-version", "build-id", "artifacts"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise SystemExit("Disposable GitHub-hosted Windows runner required; no local-device execution")
    assert not h.inventory_registry_entries(), "runner must start without token.place owners"
    output = Path(args.artifacts) / "mixed-ownership"
    output.mkdir(parents=True, exist_ok=True)
    roots = {kind: h.create_install_directory() for kind in ("nsis", "msi")}
    packages = {kind: h.Installer(Path(getattr(args, kind)), kind, args.version) for kind in roots}
    previous = {kind: h.Installer(Path(getattr(args, "old_" + kind)), kind, args.old_version) for kind in roots}
    results = []
    config = None
    try:
        # Shipped packages establish the existing mixed/disjoint state. The old
        # silent NSIS flow does not display its WiX migration page.
        h.reset_msi_remembered_locations(previous["msi"])
        for kind in ("msi", "nsis"):
            result = h.install(previous[kind], output / f"seed-{kind}.log", install_root=roots[kind])
            assert result.returncode == 0, result.stdout[-1000:]
        assert len(h.inventory_registry_entries()) == 2
        model = output / "preserved-model.gguf"
        model.write_bytes(b"GGUF preservation fixture; not used for inference")
        values = h.seeded_config_values()
        values["model_path"] = str(model)
        config = h.seed_config(values)
        saved_data = (config.read_bytes(), model.read_bytes())

        def preserved() -> tuple:
            return tree(roots["nsis"] / "app"), tree(roots["msi"] / "app"), h._authority_signature(h.capture_authority_snapshot()), config.read_bytes(), model.read_bytes()

        for kind, other in (("msi", "nsis"), ("nsis", "msi")):
            other_root = roots[other] / "app"
            before = tree(other_root), authority(other_root, other == "msi")
            result = h.install(packages[kind], output / f"upgrade-{kind}.log", install_root=roots[kind])
            assert result.returncode == 0, result.stdout[-1000:]
            assert before == (tree(other_root), authority(other_root, other == "msi")), "upgrade changed the competing owner"
            assert saved_data == (config.read_bytes(), model.read_bytes())
            authority(roots[kind] / "app", kind == "msi")
            exe = next((roots[kind] / "app").glob("token-place*.exe"))
            h.probe_identity(exe, dict(os.environ), args.version, args.build_id)
            results.append({"scenario": f"mixed-upgrade-{kind}", "exit": result.returncode, "other_owner_preserved": True})

            # Same-format ownership cannot authorize another target, even when
            # nested inside the old owner or aliasing the competing owner.
            for label, candidate_root in (("other", roots[other]), ("nested", roots[kind] / "app" / "child")):
                before = preserved()
                result = h.install(packages[kind], output / f"reject-{kind}-{label}.log", install_root=candidate_root)
                assert h.is_actionable_competing_installer_rejection(result)
                assert before == preserved(), "refusal changed installed files, authority or user data"
                results.append({"scenario": f"reject-{kind}-{label}", "exit": result.returncode, "preserved": True})
            alias = roots[kind] / "disposable-junction"
            subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(roots[other])], check=True, capture_output=True)
            try:
                before = preserved()
                result = h.install(packages[kind], output / f"reject-{kind}-alias.log", install_root=alias)
                assert h.is_actionable_competing_installer_rejection(result)
                assert before == preserved()
                results.append({"scenario": f"reject-{kind}-alias", "exit": result.returncode, "preserved": True})
            finally:
                os.rmdir(alias)  # Remove only the disposable junction, never its target.

        # No INSTALLDIR argument: the registered MSI owner must win over the
        # still-present NSIS default value and the older HKCU MSI remembered path.
        before = tree(roots["nsis"] / "app"), authority(roots["nsis"] / "app", False)
        result = h._run([h._msiexec(), "/i", str(packages["msi"].path), "/qn", "/norestart", "/L*V", str(output / "msi-own-path.log")], check=False, timeout=300)
        assert result.returncode == 0
        assert before == (tree(roots["nsis"] / "app"), authority(roots["nsis"] / "app", False))
        authority(roots["msi"] / "app", True)
        results.append({"scenario": "msi-does-not-adopt-nsis-remembered-path", "exit": 0})

        # A stale/contradictory NSIS uninstall authority must not be executed.
        import winreg
        msi_entry = next(entry for entry in h.inventory_registry_entries() if entry.windows_installer)
        duplicate_key = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + msi_entry.product_code
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, duplicate_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
                raise AssertionError("disposable duplicate-authority fixture key already exists")
        except FileNotFoundError:
            pass
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, duplicate_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as original_key:
            copied = [winreg.EnumValue(original_key, i) for i in range(winreg.QueryInfoKey(original_key)[1])]
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, duplicate_key, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_32KEY) as duplicate:
                for name, value, value_type in copied:
                    winreg.SetValueEx(duplicate, name, 0, value_type, value)
            before = preserved()
            result = h.install(packages["msi"], output / "duplicate-authority.log", install_root=roots["msi"])
            assert h.is_actionable_competing_installer_rejection(result)
            assert before == preserved()
            results.append({"scenario": "duplicate-authority-coincident-executable", "exit": result.returncode, "preserved": True})
        finally:
            winreg.DeleteKeyEx(winreg.HKEY_LOCAL_MACHINE, duplicate_key, winreg.KEY_WOW64_32KEY)

        key_name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\token.place desktop"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            original, value_type = winreg.QueryValueEx(key, "UninstallString")
            try:
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, 'msiexec /x {00000000-0000-0000-0000-000000000000}')
                before = preserved()
                result = h._run([str(packages["nsis"].path), "/S", "/UPDATE", f"/D={roots['nsis'] / 'app'}"], check=False, timeout=300, log_path=output / "forged-update.log")
                assert h.is_actionable_competing_installer_rejection(result)
                assert before == preserved()
                results.append({"scenario": "stale-authority-update-flag", "exit": result.returncode, "preserved": True})
            finally:
                winreg.SetValueEx(key, "UninstallString", 0, value_type, original)

        # Normal uninstall (without /UPDATE) retains the shared namespace and the
        # surviving MSI owner, then the survivor actually reads the saved config.
        before = tree(roots["msi"] / "app"), authority(roots["msi"] / "app", True)
        command = subprocess.list2cmdline([str(roots["nsis"] / "app" / "uninstall.exe"), "/S"]) + f" _?={roots['nsis'] / 'app'}"
        result = h._run(command, check=False, timeout=300, log_path=output / "uninstall-nsis.log")
        assert result.returncode == 0
        assert before == (tree(roots["msi"] / "app"), authority(roots["msi"] / "app", True))
        assert saved_data == (config.read_bytes(), model.read_bytes())
        exe = next((roots["msi"] / "app").glob("token-place*.exe"))
        record = h.assert_operator_record(h.launch_for_operator_record(exe, dict(os.environ), output / "survivor-config-readback.log"))
        for key in ("context_tier", "preferred_mode"):
            assert str(record.get(key)) == str(values[key])
        h.verify_config_preserved(config, values)
        results.append({"scenario": "uninstall-shared-data-survivor-readback", "preserved": True})
    finally:
        (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        h._terminate_processes()
        h.uninstall_best_effort(output / "cleanup.log")
        h.verify_no_authority_remains()
        for root in roots.values():
            h.cleanup_install_directory(root)
        if config:
            config.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
