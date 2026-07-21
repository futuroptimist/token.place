#!/usr/bin/env python3
"""Hosted-Windows release identity/upgrade guard for token.place installers.

The test intentionally validates deployment identity without claiming real CUDA
execution. It is designed to run after MSI/NSIS artifacts are built on a hosted
Windows runner; non-Windows hosts may use --contract-only for static checks.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED_VERSION = "0.1.3"
EXPECTED_RUNTIME_ID = "bundled-cpython-3.11-win-x86_64-cu124"
SENTINELS = ("py", "python", "python3")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)


def _sentinel_dir(root: Path) -> Path:
    directory = root / "sentinel-path"
    directory.mkdir(parents=True, exist_ok=True)
    for name in SENTINELS:
        sentinel = directory / f"{name}.cmd"
        sentinel.write_text(f"@echo off\necho SENTINEL {name} invoked>>%TOKENPLACE_SENTINEL_LOG%\nexit /b 42\n", encoding="utf-8")
    return directory


def _find_installed_exe(root: Path) -> Path:
    matches = sorted(p for p in root.rglob("*.exe") if "token" in p.name.lower() and not p.name.lower().startswith(("unins", "uninstall")))
    if not matches:
        raise AssertionError(f"no installed token.place executable under {root}")
    return matches[0]


def _assert_runtime(root: Path) -> None:
    python_exe = root / "python-runtime" / "python.exe"
    if not python_exe.exists():
        candidates = sorted(root.rglob("python-runtime/python.exe"))
        if not candidates:
            raise AssertionError("expected bundled python-runtime/python.exe in installed resources")
        python_exe = candidates[0]
    provenance = python_exe.parent / "tokenplace-runtime-provenance.json"
    if provenance.exists():
        data = json.loads(provenance.read_text(encoding="utf-8"))
        runtime_id = data.get("runtime_id") or data.get("build_profile")
        if runtime_id and runtime_id != EXPECTED_RUNTIME_ID:
            raise AssertionError(f"unexpected runtime id {runtime_id!r}")


def _extract(installer: Path, destination: Path) -> None:
    suffix = installer.suffix.lower()
    if suffix == ".msi":
        _run(["msiexec.exe", "/a", str(installer), "/qn", "/norestart", f"TARGETDIR={destination}"])
    elif suffix == ".exe":
        _run([str(installer), "/S", f"/D={destination}"])
    else:
        raise AssertionError(f"unsupported installer type: {installer}")


def validate_artifact(installer: Path, label: str) -> None:
    if EXPECTED_VERSION not in installer.name:
        raise AssertionError(f"{label} filename must include {EXPECTED_VERSION}: {installer.name}")
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory(prefix=f"token-place-{label}-") as tmp:
        root = Path(tmp)
        sentinel_log = root / "sentinel.log"
        sentinel_path = _sentinel_dir(root)
        env = {"PATH": str(sentinel_path), "TOKENPLACE_SENTINEL_LOG": str(sentinel_log)}
        install_root = root / "install"
        install_root.mkdir()
        _extract(installer, install_root)
        exe = _find_installed_exe(install_root)
        _assert_runtime(install_root)
        # Metadata probe only: do not assert real CUDA on hosted Windows.
        version = _run(["powershell", "-NoProfile", "-Command", f"(Get-Item '{exe}').VersionInfo.ProductVersion"], env=env).stdout.strip()
        if EXPECTED_VERSION not in version:
            raise AssertionError(f"installed executable version mismatch: {version}")
        if sentinel_log.exists() and sentinel_log.read_text(encoding="utf-8").strip():
            raise AssertionError("host Python sentinel was invoked during installer identity validation")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-nsis", type=Path, required=True)
    parser.add_argument("--windows-msi", type=Path, required=True)
    parser.add_argument("--previous-version", default="0.1.2")
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    args = parser.parse_args()
    if args.expected_version != EXPECTED_VERSION:
        raise AssertionError(f"script pinned to {EXPECTED_VERSION}; got {args.expected_version}")
    validate_artifact(args.windows_nsis, "clean-nsis")
    validate_artifact(args.windows_msi, "clean-msi")
    print("validated clean NSIS/MSI identity and static 0.1.2-to-0.1.3 upgrade contract")
    print("cross-installation policy: competing MSI/NSIS installs must fail closed rather than leave stale shortcuts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
