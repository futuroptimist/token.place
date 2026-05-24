#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import subprocess
import tempfile
from pathlib import Path

STALE_BRANDS = ("tokenplace Desktop", "tokenplace Desktop Setup", "desktop/electron-builder")


def _fail(msg: str) -> None:
    raise SystemExit(msg)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(f"Command failed ({' '.join(cmd)}):\n{result.stdout}\n{result.stderr}")
    return f"{result.stdout}\n{result.stderr}".strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_preview_readme_in_dmg(dmg_path: Path) -> None:
    required_phrases = (
        "ad-hoc signed",
        "not notarized",
        "Apple could not verify",
        "Privacy & Security",
        "Developer ID",
        "notarization",
    )
    with tempfile.TemporaryDirectory(prefix="token-place-dmg-") as mount_dir:
        mount_path = Path(mount_dir)
        _run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount_path), str(dmg_path)])
        try:
            apps = sorted(path for path in mount_path.glob("*.app") if path.is_dir())
            if len(apps) != 1:
                _fail(f"DMG must contain exactly one .app at root; found {len(apps)}")
            readme_path = mount_path / "README BEFORE OPENING.txt"
            if not readme_path.exists() or not readme_path.is_file():
                _fail(f"DMG is missing preview README at root: {readme_path.name}")
            readme_text = readme_path.read_text(encoding="utf-8")
            for phrase in required_phrases:
                if phrase not in readme_text:
                    _fail(f"DMG preview README missing required phrase: {phrase}")
        finally:
            _run(["hdiutil", "detach", str(mount_path)])


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--app-path", required=True)
    p.add_argument("--dmg-path", required=True)
    p.add_argument("--tauri-config", required=True)
    p.add_argument("--expected-icon", required=True)
    p.add_argument("--expect-signing", action="store_true")
    p.add_argument("--expect-notarization", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    app_path = Path(args.app_path)
    dmg_path = Path(args.dmg_path)
    tauri_config = Path(args.tauri_config)
    expected_icon = Path(args.expected_icon)

    for candidate in (app_path.name, dmg_path.name, str(dmg_path)):
        for stale in STALE_BRANDS:
            if stale.lower() in candidate.lower():
                _fail(f"stale Electron branding detected: {stale} in {candidate}")

    if not dmg_path.exists() or dmg_path.suffix.lower() != '.dmg' or not dmg_path.is_file():
        _fail(f"dmg artifact missing or invalid: {dmg_path}")
    if not dmg_path.name.startswith("token.place-desktop-") or not dmg_path.name.endswith("-apple-silicon.dmg"):
        _fail(f"DMG filename must match token.place-desktop-<version>-apple-silicon.dmg: {dmg_path.name}")

    if not app_path.exists() or app_path.suffix != ".app":
        _fail(f"app bundle missing or invalid: {app_path}")
    if not expected_icon.exists() or not expected_icon.is_file():
        _fail(f"expected icon missing or invalid: {expected_icon}")

    info_plist = app_path / "Contents" / "Info.plist"
    if not info_plist.exists():
        _fail(f"missing Info.plist: {info_plist}")
    info = plistlib.loads(info_plist.read_bytes())

    product_name = info.get("CFBundleName") or ""
    display_name = info.get("CFBundleDisplayName") or ""
    if "tokenplace desktop" in str(product_name).lower():
        _fail("stale app bundle name tokenplace Desktop detected in CFBundleName")
    if "tokenplace desktop" in str(display_name).lower():
        _fail("stale app display name tokenplace Desktop detected in CFBundleDisplayName")

    config = json.loads(tauri_config.read_text(encoding="utf-8"))
    icons = config.get("bundle", {}).get("icon", [])
    required_icons = {"icons/icon.icns", "icons/icon.ico", "icons/128x128@2x.png"}
    missing = sorted(required_icons - set(icons))
    if missing:
        _fail(f"tauri icon list missing required entries: {missing}")

    icon_key = info.get("CFBundleIconFile", "icon.icns")
    if not str(icon_key).endswith(".icns"):
        icon_key = f"{icon_key}.icns"
    bundled_icon = app_path / "Contents" / "Resources" / icon_key
    if not bundled_icon.exists():
        _fail(f"bundled icon not found: {bundled_icon}")
    if _sha256(bundled_icon) != _sha256(expected_icon):
        _fail("bundled icon hash does not match desktop-tauri/src-tauri/icons/icon.icns")

    macos_dir = app_path / "Contents" / "MacOS"
    if not macos_dir.exists() or not macos_dir.is_dir():
        _fail(f"missing app executable directory: {macos_dir}")
    executable_name = info.get("CFBundleExecutable")
    if not executable_name:
        _fail("CFBundleExecutable is missing from Info.plist")
    executable_path = macos_dir / str(executable_name)
    if not executable_path.exists() or not executable_path.is_file():
        _fail(f"CFBundleExecutable not found in app bundle: {executable_path}")
    arch_out = _run(["lipo", "-archs", str(executable_path)])
    arch_lower = arch_out.lower()
    if "arm64" not in arch_lower and "aarch64" not in arch_lower:
        _fail(f"binary is not Apple Silicon: {arch_out}")
    if "x86_64" in arch_lower and "arm64" not in arch_lower:
        _fail(f"binary is x86_64-only: {arch_out}")
    _validate_preview_readme_in_dmg(dmg_path)

    if args.expect_signing:
        _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_path)])
        if args.expect_notarization:
            _run(["spctl", "-a", "-vv", "--type", "execute", str(app_path)])
        else:
            print("::warning::Signing configured without notarization credentials; skipping strict Gatekeeper assessment.")
    else:
        print("::warning::Signing credentials not configured; macOS artifact is preview/dev-only and may fail Gatekeeper.")


if __name__ == "__main__":
    main()
