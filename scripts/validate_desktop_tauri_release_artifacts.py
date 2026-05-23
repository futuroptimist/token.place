#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STALE_BRANDING_PATTERNS = (
    "tokenplace Desktop",
    "tokenplace Desktop Setup",
    "desktop/electron-builder",
)
EXPECTED_ICON_REL = Path("desktop-tauri/src-tauri/icons/icon.icns")


def _fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if proc.returncode != 0:
        _fail(f"Command failed ({' '.join(cmd)}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _contains_stale_branding(text: str) -> str | None:
    for pattern in STALE_BRANDING_PATTERNS:
        if pattern in text:
            return pattern
    return None


def validate(staging_dir: Path, source_icon: Path, tauri_conf: Path, require_signing: bool) -> None:
    files = sorted(p for p in staging_dir.iterdir() if p.is_file())
    if not files:
        _fail(f"No files found in staging directory: {staging_dir}")

    for path in files:
        match = _contains_stale_branding(path.name)
        if match:
            _fail(f"Staged artifact {path.name} contains stale branding pattern: {match}")

    dmg_files = [p for p in files if p.suffix == ".dmg"]
    if len(dmg_files) != 1:
        _fail(f"Expected exactly one staged DMG, found {len(dmg_files)}")
    dmg = dmg_files[0]
    if "apple-silicon" not in dmg.name:
        _fail(f"DMG must include apple-silicon marker: {dmg.name}")

    with tempfile.TemporaryDirectory() as td:
        mount_dir = Path(td) / "mount"
        mount_dir.mkdir(parents=True, exist_ok=True)
        _run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount_dir), str(dmg)])
        try:
            app_candidates = sorted(mount_dir.glob("*.app"))
            if not app_candidates:
                _fail(f"No .app found in mounted DMG: {dmg}")
            app = app_candidates[0]
            if _contains_stale_branding(app.name):
                _fail(f"App bundle contains stale branding: {app.name}")

            info_plist = app / "Contents/Info.plist"
            if not info_plist.exists():
                _fail(f"Missing Info.plist in app: {app}")
            with info_plist.open("rb") as fh:
                plist = plistlib.load(fh)
            for key in ("CFBundleDisplayName", "CFBundleName"):
                value = plist.get(key, "")
                match = _contains_stale_branding(str(value))
                if match:
                    _fail(f"{key} contains stale branding: {match}")

            icon_name = plist.get("CFBundleIconFile", "icon.icns")
            if not str(icon_name).endswith(".icns"):
                icon_name = f"{icon_name}.icns"
            bundled_icon = app / "Contents/Resources" / icon_name
            if not bundled_icon.exists():
                _fail(f"Bundled icon missing: {bundled_icon}")

            if hashlib.sha256(source_icon.read_bytes()).hexdigest() != hashlib.sha256(
                bundled_icon.read_bytes()
            ).hexdigest():
                _fail("Bundled app icon does not match desktop-tauri/src-tauri/icons/icon.icns")

            binary = app / "Contents/MacOS" / app.stem
            if not binary.exists():
                _fail(f"Expected app binary missing: {binary}")

            archs = _run(["lipo", "-archs", str(binary)])
            if "arm64" not in archs and "aarch64" not in archs:
                _fail(f"App binary is not Apple Silicon (arm64/aarch64): {archs}")
            if "x86_64" in archs and "arm64" not in archs:
                _fail(f"App binary is x86_64-only, expected Apple Silicon: {archs}")

            if require_signing:
                _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
                _run(["spctl", "-a", "-vv", "--type", "execute", str(app)])
            else:
                print("WARNING: Signing identity/secrets unavailable. Build is preview/dev-only.")
        finally:
            subprocess.run(["hdiutil", "detach", str(mount_dir)], check=False, capture_output=True, text=True)

    conf_text = tauri_conf.read_text(encoding="utf-8")
    if "icons/icon.icns" not in conf_text or "icons/128x128@2x.png" not in conf_text:
        _fail("tauri.conf.json icon list is missing expected token.place icon entries")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--source-icon", type=Path, default=EXPECTED_ICON_REL)
    parser.add_argument("--tauri-conf", type=Path, default=Path("desktop-tauri/src-tauri/tauri.conf.json"))
    parser.add_argument("--require-signing", action="store_true")
    args = parser.parse_args()
    validate(args.staging_dir, args.source_icon, args.tauri_conf, args.require_signing)


if __name__ == "__main__":
    main()
