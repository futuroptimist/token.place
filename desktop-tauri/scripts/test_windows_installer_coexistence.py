"""Disposable hosted-Windows mixed-ownership regressions; never run on a user device."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import shutil
import queue
import threading
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import test_windows_installer_identity as h
from windows_installer_ui import nsis_ui_action


@contextmanager
def running_fixture(executable: Path, fixture: Path):
    """Replace only a disposable installed binary; restore after normal exit."""
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    original = executable.with_suffix(".fixture-backup")
    assert not original.exists()
    executable.rename(original)
    process = None
    try:
        shutil.copyfile(fixture, executable)
        process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        assert ready.get(timeout=30).strip() == "fixture-ready"
        yield process
        assert process.poll() is None, "installer terminated the disposable app process"
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.stdin.close()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        # Cleanup owns this exact disposable child handle only.
                        process.terminate()
                        process.wait(timeout=30)
                        raise
                assert process.returncode == 0, "fixture did not exit normally"
        finally:
            executable.unlink(missing_ok=True)
            original.rename(executable)


def drive_nsis(package: Path, target: Path, log: Path, *, passive: bool, choose_target: Path | None = None) -> dict:
    """Exercise actual dialog notification handlers on CI, not physical input."""
    import ctypes as c
    from ctypes import wintypes as w
    user = c.WinDLL("user32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.GetWindowLongW.argtypes = [w.HWND, c.c_int]
    user.GetWindowLongW.restype = w.LONG
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, c.POINTER(c.c_size_t)]
    user.SendMessageTimeoutW.restype = w.LPARAM
    user.GetDlgCtrlID.argtypes = [w.HWND]
    user.GetParent.argtypes = [w.HWND]
    user.GetParent.restype = w.HWND
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.IsWindowVisible.argtypes = [w.HWND]
    user.IsWindowEnabled.argtypes = [w.HWND]
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumChildWindows.argtypes = [w.HWND, callback_type, w.LPARAM]
    class Process(c.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD), ("exe", w.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(Process)]
    kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Process)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    observed_uninstaller = False
    owned_children = set()
    selected_uninstall = False
    selected_directory = False
    sent = set()
    command = [str(package)] + (["/P"] if passive else []) + [f"/D={target}"]
    # GetWindowTextW cannot read another process's edit controls. WM_GETTEXT
    # supports cross-process control text; bound every call so diagnostics
    # cannot hang behind an installer waiting for its child uninstaller.
    def control_text(hwnd):
        buffer = c.create_unicode_buffer(2048)
        result = c.c_size_t()
        c.set_last_error(0)
        ok = user.SendMessageTimeoutW(hwnd, 0xD, len(buffer), c.addressof(buffer), 0x22, 250, c.byref(result))
        return buffer.value if ok else f"<text unavailable: {c.get_last_error()}>"

    with log.open("w", encoding="utf-8") as output, log.with_suffix(".ui.jsonl").open("w", encoding="utf-8") as trace:
        def record(event, **values):
            trace.write(json.dumps({"event": event, **values}, ensure_ascii=False) + "\n")
            trace.flush()

        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        record("started", pid=process.pid, command=command)
        previous_state = None
        deadline = time.monotonic() + 300
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    record("timeout", observed_uninstaller=observed_uninstaller, selected_uninstall=selected_uninstall, selected_directory=selected_directory, owned_children=sorted(owned_children))
                    raise AssertionError("NSIS UI/reinstall transition timed out")
                snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
                if snapshot != c.c_void_p(-1).value:
                    try:
                        entry = Process(); entry.size = c.sizeof(entry)
                        found = kernel.Process32FirstW(snapshot, c.byref(entry))
                        while found:
                            if entry.exe.lower() == "uninstall.exe":
                                handle = kernel.OpenProcess(0x1000, False, entry.pid)
                                if handle:
                                    try:
                                        buffer = c.create_unicode_buffer(32768); size = w.DWORD(32768)
                                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(size)):
                                            assert Path(buffer.value).resolve() == (target / "uninstall.exe").resolve(), "selected another owner's uninstaller"
                                            observed_uninstaller = True
                                            if entry.pid not in owned_children:
                                                record("owned-uninstaller", pid=entry.pid, path=buffer.value)
                                            owned_children.add(entry.pid)
                                    finally:
                                        kernel.CloseHandle(handle)
                            found = kernel.Process32NextW(snapshot, c.byref(entry))
                    finally:
                        kernel.CloseHandle(snapshot)
                if not passive:
                    windows = []
                    @callback_type
                    def collect(hwnd, _):
                        pid = w.DWORD()
                        user.GetWindowThreadProcessId(hwnd, c.byref(pid))
                        if (pid.value == process.pid or pid.value in owned_children) and user.IsWindowVisible(hwnd):
                            windows.append((hwnd, pid.value != process.pid))
                        return True
                    user.EnumWindows(collect, 0)
                    state = []
                    for window, is_uninstaller in windows:
                        controls = []
                        @callback_type
                        def children(hwnd, _):
                            nonlocal selected_directory
                            name = c.create_unicode_buffer(256)
                            user.GetClassNameW(hwnd, name, 256)
                            label = control_text(hwnd)
                            controls.append({"hwnd": hwnd, "id": user.GetDlgCtrlID(hwnd), "class": name.value, "text": label, "visible": bool(user.IsWindowVisible(hwnd)), "enabled": bool(user.IsWindowEnabled(hwnd))})
                            if name.value == "Button":
                                controls[-1]["style"] = user.GetWindowLongW(hwnd, -16) & 15
                                checked = c.c_size_t()
                                ok = user.SendMessageTimeoutW(hwnd, 0xF0, 0, 0, 0x22, 250, c.byref(checked))
                                controls[-1]["checked"] = checked.value if ok else None
                            if choose_target is not None and name.value == "Edit" and label.rstrip("\\") == str(target).rstrip("\\"):
                                replacement = c.create_unicode_buffer(str(choose_target))
                                changed = c.c_size_t()
                                ok = user.SendMessageTimeoutW(hwnd, 0xC, 0, c.addressof(replacement), 0x22, 250, c.byref(changed))
                                selected_directory = bool(ok and changed.value and control_text(hwnd) == str(choose_target))
                                record("directory-selection", hwnd=hwnd, confirmed=selected_directory)
                            return True
                        user.EnumChildWindows(window, children, 0)
                        state.append({"window": window, "uninstaller": is_uninstaller, "text": control_text(window), "controls": controls})
                        for control in controls:
                            if control["id"] == 1203 and control["class"] == "Button" and control["visible"]:
                                ignored = c.c_size_t()
                                ok = user.SendMessageTimeoutW(control["hwnd"], 0xF1, 0, 0, 0x22, 250, c.byref(ignored))
                                checked = c.c_size_t()
                                read = user.SendMessageTimeoutW(control["hwnd"], 0xF0, 0, 0, 0x22, 250, c.byref(checked))
                                assert ok and read and checked.value == 0, "could not disable app auto-launch"
                        if "TOKEN_PLACE_UNSAFE_LEGACY_UNINSTALL" in str(state):
                            for control in controls:
                                if control["class"] == "Button" and control["text"] == "OK":
                                    user.PostMessageW(user.GetParent(control["hwnd"]), 0x111, control["id"], control["hwnd"])
                        action = nsis_ui_action(controls, uninstaller=is_uninstaller, fresh=choose_target is not None, directory_selected=selected_directory, owner_observed=observed_uninstaller, selected_uninstall=selected_uninstall, sent=sent)
                        if action:
                            token, button, radios = action
                            if radios:
                                for radio in radios:
                                    ignored = c.c_size_t()
                                    ok = user.SendMessageTimeoutW(radio["hwnd"], 0xF1, int(radio is button), 0, 0x22, 250, c.byref(ignored))
                                    assert ok, "could not set maintenance choice"
                            if token.startswith("maintenance:"):
                                selected_uninstall = True  # Planner verified both radio states.
                            # BN_CLICKED/WM_COMMAND is the normal button notification.
                            # Send once to the immediate parent; BM_CLICK can fail on
                            # inactive dialogs and repeated queued clicks skip pages.
                            c.set_last_error(0)
                            posted = user.PostMessageW(user.GetParent(button["hwnd"]), 0x111, button["id"], button["hwnd"])
                            record("notify-button", token=token, hwnd=button["hwnd"], posted=bool(posted), error=c.get_last_error())
                            assert posted, "could not notify installer control"
                            sent.add(token)
                    if state != previous_state:
                        record("windows", state=state)
                        previous_state = state
                time.sleep(0.05)
            expected = 1638 if not passive and choose_target is None else 0
            assert process.returncode == expected
            if choose_target is not None:
                assert selected_directory and not observed_uninstaller
            else:
                assert not observed_uninstaller, "unsafe external uninstaller was launched"
            if not passive and choose_target is None:
                assert selected_uninstall, "interactive reinstall choice was not exercised"
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
    return {"scenario": "nsis-fresh-gui-select-empty-target" if choose_target else ("nsis-passive-transition" if passive else "nsis-interactive-transition"), "exit": process.returncode, "owned_uninstaller_observed": observed_uninstaller}


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
    assert shortcuts and all(Path(target).is_file() for _, target in shortcuts), (
        f"owner at {root} lacks valid shortcuts: {snapshot.shortcuts.shortcuts!r}"
    )
    return entries, sorted(shortcuts)


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("nsis", "msi", "old-nsis", "old-msi", "version", "old-version", "build-id", "artifacts", "process-fixture"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise SystemExit("Disposable GitHub-hosted Windows runner required; no local-device execution")
    assert not h.inventory_registry_entries(), "runner must start without token.place owners"
    output = Path(args.artifacts) / "mixed-ownership"
    output.mkdir(parents=True, exist_ok=True)
    roots = {kind: h.create_install_directory() for kind in ("nsis", "msi")}
    # Establish healthy legacy owners. The shipped v0.1.24 NSIS seed produced
    # invalid non-ACP shortcut targets in hosted CI before the candidate ran.
    # Unicode transport/path tests remain separate; do not repair seed shortcuts
    # or relax any authority/preservation assertions to manufacture a pass.
    packages = {kind: h.Installer(Path(getattr(args, kind)), kind, args.version) for kind in roots}
    previous = {kind: h.Installer(Path(getattr(args, "old_" + kind)), kind, args.old_version) for kind in roots}
    results = []
    config = None
    unrelated_key = None
    try:
        import winreg
        unrelated_key = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OwnershipFixture-" + roots["msi"].name
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, unrelated_key, 0, winreg.KEY_SET_VALUE) as fixture:
            winreg.SetValueEx(fixture, "DisplayName", 0, winreg.REG_EXPAND_SZ, "Unrelated application %SystemRoot%")
        # Shipped packages establish the existing mixed/disjoint state. The old
        # silent NSIS flow does not display its WiX migration page.
        h.reset_msi_remembered_locations(previous["msi"])
        for kind in ("msi", "nsis"):
            result = h.install(previous[kind], output / f"seed-{kind}.log", install_root=roots[kind])
            assert result.returncode == 0, result.stdout[-1000:]
        assert len(h.inventory_registry_entries()) == 2
        for kind in roots:
            authority(roots[kind] / "app", kind == "msi")
        model = output / "preserved-model.gguf"
        model.write_bytes(b"GGUF preservation fixture; not used for inference")
        values = h.seeded_config_values()
        values["model_path"] = str(model)
        config = h.seed_config(values)
        saved_data = (config.read_bytes(), model.read_bytes())

        def preserved() -> tuple:
            return tree(roots["nsis"] / "app"), tree(roots["msi"] / "app"), h._authority_signature(h.capture_authority_snapshot()), config.read_bytes(), model.read_bytes()

        # The sleeper replaces only a disposable installed executable. These
        # are process-identity/survival fixtures, not full application launches.
        fixture = Path(args.process_fixture)
        for kind in ("msi", "nsis"):
            executable = next((roots[kind] / "app").glob("token-place*.exe"))
            with running_fixture(executable, fixture) as process:
                before = preserved()
                result = h.install(packages[kind], output / f"running-upgrade-{kind}.log", install_root=roots[kind])
                assert result.returncode != 0
                if kind == "msi":
                    assert "TOKEN_PLACE_APP_RUNNING" in (output / f"running-upgrade-{kind}.msi.log").read_text(encoding="utf-16")
                else:
                    assert "TOKEN_PLACE_APP_RUNNING" in result.stdout
                assert process.poll() is None and before == preserved()
                results.append({"scenario": f"running-upgrade-{kind}", "exit": result.returncode, "process_survived": True, "preserved": True})

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

        for passive in (True, False):
            other_root = roots["msi"] / "app"
            before = tree(other_root), authority(other_root, True)
            all_before = preserved()
            results.append(drive_nsis(packages["nsis"].path, roots["nsis"] / "app", output / f"nsis-transition-{passive}.log", passive=passive))
            if not passive:
                assert all_before == preserved()
            assert before == (tree(other_root), authority(other_root, True))
            assert saved_data == (config.read_bytes(), model.read_bytes())
            authority(roots["nsis"] / "app", False)

        for kind in ("nsis", "msi"):
            executable = next((roots[kind] / "app").glob("token-place*.exe"))
            with running_fixture(executable, fixture) as process:
                before = preserved()
                if kind == "nsis":
                    command = subprocess.list2cmdline([str(executable.parent / "uninstall.exe"), "/S"]) + f" _?={executable.parent}"
                else:
                    entry = next(item for item in h.inventory_registry_entries() if item.windows_installer)
                    command = [h._msiexec(), "/x", entry.product_code, "/qn", "/norestart", "TOKEN_PLACE_BINARY=unrelated.exe", "/L*V", str(output / "running-uninstall-msi.msi.log")]
                result = h._run(command, check=False, timeout=300, log_path=output / f"running-uninstall-{kind}.log")
                assert result.returncode != 0 and process.poll() is None
                assert before == preserved()
                results.append({"scenario": f"running-uninstall-{kind}", "exit": result.returncode, "process_survived": True, "preserved": True})
                other = "msi" if kind == "nsis" else "nsis"
                before_other = tree(executable.parent), authority(executable.parent, kind == "msi"), saved_data
                result = h.install(packages[other], output / f"disjoint-running-{other}.log", install_root=roots[other])
                assert result.returncode == 0 and process.poll() is None
                assert before_other == (tree(executable.parent), authority(executable.parent, kind == "msi"), (config.read_bytes(), model.read_bytes()))
                results.append({"scenario": f"disjoint-running-{other}", "process_survived": True, "preserved": True})

        # A stale/contradictory NSIS uninstall authority must not be executed.
        msi_entry = next(entry for entry in h.inventory_registry_entries() if entry.windows_installer)
        duplicate_key = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + msi_entry.product_code
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, duplicate_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
                raise AssertionError("disposable duplicate-authority fixture key already exists")
        except FileNotFoundError:
            pass
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, duplicate_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as original_key:
            copied = [winreg.EnumValue(original_key, i) for i in range(winreg.QueryInfoKey(original_key)[1])]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, duplicate_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as original_key:
            display, display_type = winreg.QueryValueEx(original_key, "DisplayName")
            try:
                winreg.SetValueEx(original_key, "DisplayName", 0, winreg.REG_DWORD, 7)
                before = preserved()
                result = h.install(packages["nsis"], output / "malformed-msi-display.log", install_root=roots["nsis"])
                assert h.is_actionable_competing_installer_rejection(result)
                assert before == preserved()
                results.append({"scenario": "related-msi-malformed-display", "exit": result.returncode, "preserved": True})
            finally:
                winreg.SetValueEx(original_key, "DisplayName", 0, display_type, display)
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
        if unrelated_key:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, unrelated_key)
        h._terminate_processes()
        h.uninstall_best_effort(output / "cleanup.log")
        h.verify_no_authority_remains()
        for root in roots.values():
            h.cleanup_install_directory(root)
        if config:
            config.unlink(missing_ok=True)

    # Fresh interactive installs may select an empty folder when the initial
    # default contains unowned leftovers; those leftovers must remain intact.
    stale, fresh = h.create_install_directory(), h.create_install_directory()
    try:
        (stale / "app").mkdir()
        (stale / "app" / "unowned.txt").write_text("preserve this unrelated data", encoding="utf-8")
        before = tree(stale / "app")
        results.append(drive_nsis(packages["nsis"].path, stale / "app", output / "fresh-gui.log", passive=False, choose_target=fresh / "app"))
        assert before == tree(stale / "app")
        h.verify_installed_authority(packages["nsis"], fresh)
    finally:
        (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        h._terminate_processes()
        h.uninstall_best_effort(output / "fresh-cleanup.log")
        h.verify_no_authority_remains()
        h.cleanup_install_directory(stale)
        h.cleanup_install_directory(fresh)


if __name__ == "__main__":
    main()
