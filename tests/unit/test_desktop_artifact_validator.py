from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "validate_desktop_tauri_release_artifacts.py"
SPEC = importlib.util.spec_from_file_location("validate_desktop_tauri_release_artifacts", SCRIPT_PATH)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(validator)


def _completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_attach_dmg_retries_transient_errors_with_fresh_mountpoints_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    dmg_path = Path("/tmp/release-artifacts/token.place-desktop-0.1.2-apple-silicon.dmg")
    mountpoints = [Path(f"/tmp/token-place-dmg-mount-{index}") for index in range(1, 4)]
    mkdtemp_calls: list[str] = []
    commands: list[list[str]] = []
    removed: list[Path] = []

    def fake_mkdtemp(*, prefix: str) -> str:
        mkdtemp_calls.append(prefix)
        return str(mountpoints[len(mkdtemp_calls) - 1])

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[:2] == ["hdiutil", "attach"]:
            attempt = len([seen for seen in commands if seen[:2] == ["hdiutil", "attach"]])
            if attempt < 3:
                return _completed(cmd, 1, stderr="hdiutil: attach failed - Resource temporarily unavailable")
            return _completed(cmd, 0, stdout="/dev/disk4")
        if cmd == ["hdiutil", "info"]:
            return _completed(cmd, 0, stdout=f"/dev/disk9\n    image-path      : {dmg_path.resolve()}\n")
        if cmd[:2] == ["hdiutil", "detach"]:
            return _completed(cmd, 0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(validator.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(validator.subprocess, "run", fake_run)
    monkeypatch.setattr(validator.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(validator.shutil, "rmtree", lambda path, **_kwargs: removed.append(Path(path)))

    assert validator._attach_dmg_with_retries(dmg_path) == mountpoints[2]

    attach_commands = [cmd for cmd in commands if cmd[:2] == ["hdiutil", "attach"]]
    assert [cmd[5] for cmd in attach_commands] == [str(mountpoint) for mountpoint in mountpoints]
    assert ["hdiutil", "detach", str(mountpoints[0])] in commands
    assert ["hdiutil", "detach", str(mountpoints[1])] in commands
    assert ["hdiutil", "detach", "/dev/disk9"] in commands
    assert removed == mountpoints[:2]


def test_attach_dmg_does_not_retry_non_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    dmg_path = Path("/tmp/release-artifacts/token.place-desktop-0.1.2-apple-silicon.dmg")
    commands: list[list[str]] = []

    monkeypatch.setattr(validator.tempfile, "mkdtemp", lambda prefix: "/tmp/token-place-dmg-mount-1")
    monkeypatch.setattr(validator.shutil, "rmtree", lambda path, **_kwargs: None)

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[:2] == ["hdiutil", "attach"]:
            return _completed(cmd, 1, stderr="hdiutil: attach failed - image not recognized")
        if cmd == ["hdiutil", "info"]:
            return _completed(cmd, 0, stdout="")
        if cmd[:2] == ["hdiutil", "detach"]:
            return _completed(cmd, 0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(validator.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        validator._attach_dmg_with_retries(dmg_path)

    assert "image not recognized" in str(exc_info.value)
    assert len([cmd for cmd in commands if cmd[:2] == ["hdiutil", "attach"]]) == 1
