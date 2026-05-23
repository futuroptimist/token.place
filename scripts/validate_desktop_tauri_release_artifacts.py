#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import plistlib
import subprocess
import sys
from pathlib import Path

STALE_BRANDS = ("tokenplace Desktop", "tokenplace Desktop Setup", "desktop/electron-builder")


def _fail(message: str) -> None:
    raise SystemExit(message)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(f"Command failed ({' '.join(cmd)}): {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _validate_names(paths: list[Path]) -> None:
    for path in paths:
        for stale in STALE_BRANDS:
            if stale.lower() in path.name.lower():
                _fail(f"Staged artifact contains stale Electron branding '{stale}': {path.name}")


def _main_binary(app_path: Path) -> Path:
    info = app_path / "Contents" / "Info.plist"
    if not info.exists():
        _fail(f"Missing Info.plist at {info}")
    data = plistlib.loads(info.read_bytes())
    for stale in STALE_BRANDS:
        for key in ("CFBundleName", "CFBundleDisplayName"):
            value = str(data.get(key, ""))
            if stale.lower() in value.lower():
                _fail(f"App metadata contains stale branding '{stale}' in {key}: {value}")

    executable = data.get("CFBundleExecutable")
    if not executable:
        _fail("CFBundleExecutable missing in Info.plist")
    binary = app_path / "Contents" / "MacOS" / executable
    if not binary.exists():
        _fail(f"Expected app binary missing: {binary}")
    return binary


def _validate_arch(binary: Path) -> None:
    archs = _run(["lipo", "-archs", str(binary)])
    archs_lower = archs.lower()
    if "arm64" not in archs_lower and "aarch64" not in archs_lower:
        _fail(f"Expected Apple Silicon architecture, got: {archs}")
    if "x86_64" in archs_lower and "arm64" not in archs_lower:
        _fail(f"Binary is x86_64-only: {archs}")


def _validate_icon(app_path: Path, source_icon: Path) -> None:
    if not source_icon.exists():
        _fail(f"Expected source icon missing: {source_icon}")

    info = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
    icon_file = str(info.get("CFBundleIconFile", "icon.icns"))
    icon_file = icon_file if icon_file.endswith(".icns") else f"{icon_file}.icns"
    bundled_icon = app_path / "Contents" / "Resources" / icon_file
    if not bundled_icon.exists():
        fallback = app_path / "Contents" / "Resources" / "icon.icns"
        if not fallback.exists():
            _fail(f"Bundled icon missing: {bundled_icon} (and no fallback icon.icns)")
        bundled_icon = fallback

    src_hash = hashlib.sha256(source_icon.read_bytes()).hexdigest()
    bundled_hash = hashlib.sha256(bundled_icon.read_bytes()).hexdigest()
    if src_hash != bundled_hash:
        _fail(
            "Bundled icon does not match desktop-tauri/src-tauri/icons/icon.icns "
            f"({bundled_icon})"
        )


def _validate_signing(app_path: Path, require_signed: bool) -> None:
    sign_check = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if sign_check.returncode != 0:
        if require_signed:
            _fail(f"codesign verification failed: {sign_check.stderr.strip() or sign_check.stdout.strip()}")
        print("::warning::Unsigned/adhoc app build detected. This artifact is preview/dev-only unless notarized.")
        return

    spctl_check = subprocess.run(
        ["spctl", "-a", "-vv", "--type", "execute", str(app_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if require_signed and spctl_check.returncode != 0:
        _fail(f"spctl validation failed: {spctl_check.stderr.strip() or spctl_check.stdout.strip()}")
    if not require_signed and spctl_check.returncode != 0:
        print("::warning::spctl rejected app. Expected for unsigned preview builds.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-path", type=Path, required=True)
    parser.add_argument("--staged-artifact", type=Path, action="append", default=[])
    parser.add_argument("--source-icon", type=Path, required=True)
    parser.add_argument("--require-signed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _validate_names(args.staged_artifact)
    binary = _main_binary(args.app_path)
    _validate_arch(binary)
    _validate_icon(args.app_path, args.source_icon)
    _validate_signing(args.app_path, require_signed=args.require_signed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
