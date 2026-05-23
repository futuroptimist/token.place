#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import plistlib
import subprocess
from pathlib import Path

FORBIDDEN_MARKERS = (
    "tokenplace Desktop",
    "tokenplace Desktop Setup",
    "desktop/electron-builder",
)


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"Command failed ({' '.join(cmd)}):\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_no_forbidden_text(*values: str) -> None:
    joined = "\n".join(values)
    for marker in FORBIDDEN_MARKERS:
        if marker in joined:
            raise SystemExit(f"Forbidden stale Electron marker found: {marker}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--app-path", type=Path, required=True)
    parser.add_argument("--source-icon", type=Path, required=True)
    parser.add_argument("--expect-unsigned-preview", action="store_true")
    args = parser.parse_args()

    staging_files = [p.name for p in args.staging_dir.glob("*") if p.is_file()]
    if not staging_files:
        raise SystemExit("No release artifacts staged")
    _assert_no_forbidden_text(*staging_files)

    app_name = args.app_path.stem
    _assert_no_forbidden_text(app_name)

    info_plist = args.app_path / "Contents" / "Info.plist"
    if not info_plist.exists():
        raise SystemExit(f"Missing Info.plist at {info_plist}")
    with info_plist.open("rb") as fh:
        info = plistlib.load(fh)

    bundle_name = str(info.get("CFBundleName", ""))
    display_name = str(info.get("CFBundleDisplayName", ""))
    icon_file = str(info.get("CFBundleIconFile", "icon.icns"))
    _assert_no_forbidden_text(bundle_name, display_name, icon_file)

    icon_path = args.app_path / "Contents" / "Resources" / icon_file
    if icon_path.suffix != ".icns":
        icon_path = icon_path.with_suffix(".icns")
    if not icon_path.exists():
        raise SystemExit(f"Missing bundled macOS icon file: {icon_path}")
    if _sha256(icon_path) != _sha256(args.source_icon):
        raise SystemExit("Bundled icon.icns does not match desktop-tauri/src-tauri/icons/icon.icns")

    binary_name = str(info.get("CFBundleExecutable") or app_name)
    binary_path = args.app_path / "Contents" / "MacOS" / binary_name
    if not binary_path.exists():
        raise SystemExit(f"Missing app executable at {binary_path}")

    arch_output = _run(["file", str(binary_path)])
    arch_lower = arch_output.lower()
    if "arm64" not in arch_lower and "aarch64" not in arch_lower:
        raise SystemExit(f"Executable is not Apple Silicon: {arch_output}")
    if "x86_64" in arch_lower and "arm64" not in arch_lower:
        raise SystemExit(f"Executable is x86_64-only: {arch_output}")

    if args.expect_unsigned_preview:
        print("::warning::Desktop macOS build may be unsigned/preview-only unless Developer ID secrets are configured.")

    print("Desktop Tauri release artifact validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
