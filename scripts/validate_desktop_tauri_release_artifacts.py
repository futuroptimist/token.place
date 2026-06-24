#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

STALE_BRANDS = ("tokenplace Desktop", "tokenplace Desktop Setup", "desktop/electron-builder")
DMG_PREVIEW_README_NAMES = ("README BEFORE OPENING.txt",)
DMG_PREVIEW_REQUIRED_PHRASES = (
    "not notarized",
    "Apple could not verify",
    "Privacy & Security",
    "Developer ID",
    "notarization",
)
DMG_PREVIEW_SIGNING_PHRASE_OPTIONS = (
    "ad-hoc signed",
    "signed with the configured Apple signing identity",
)
DMG_ATTACH_TRANSIENT_MESSAGES = (
    "Resource temporarily unavailable",
    "Resource busy",
    "busy",
    "already attached",
    "already mounted",
)


def _fail(msg: str) -> None:
    raise SystemExit(msg)


def _format_command_failure(cmd: list[str], result: subprocess.CompletedProcess[str]) -> str:
    return f"Command failed ({' '.join(cmd)}):\n{result.stdout}\n{result.stderr}"


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(_format_command_failure(cmd, result))
    return f"{result.stdout}\n{result.stderr}".strip()


def _run_with_retries(
    cmd: list[str],
    *,
    attempts: int,
    retry_messages: tuple[str, ...],
    delay_seconds: float = 2.0,
    max_delay_seconds: float = 15.0,
) -> str:
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return f"{result.stdout}\n{result.stderr}".strip()

        last_result = result
        combined_output = f"{result.stdout}\n{result.stderr}".lower()
        should_retry = attempt < attempts and any(message.lower() in combined_output for message in retry_messages)
        if not should_retry:
            break

        retry_delay = min(delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
        print(
            f"::warning::Command {' '.join(cmd)} failed with a transient disk image error; "
            f"retrying attempt {attempt + 1}/{attempts} after {retry_delay:g}s."
        )
        time.sleep(retry_delay)

    if last_result is None:
        _fail(f"Command failed ({' '.join(cmd)}): no attempts were run")
    _fail(_format_command_failure(cmd, last_result))


def _is_transient_hdiutil_attach_error(output: str) -> bool:
    output_lower = output.lower()
    return any(message.lower() in output_lower for message in DMG_ATTACH_TRANSIENT_MESSAGES)


def _hdiutil_info_snapshot() -> str:
    result = subprocess.run(["hdiutil", "info"], check=False, capture_output=True, text=True)
    snapshot = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        return f"hdiutil info failed with exit {result.returncode}:\n{snapshot}"
    return snapshot


def _matching_hdiutil_devices(dmg_path: Path) -> list[str]:
    result = subprocess.run(["hdiutil", "info"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    devices: list[str] = []
    current_device: str | None = None
    dmg_resolved = str(dmg_path.resolve())
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("/dev/disk"):
            current_device = line.split()[0]
            continue
        if current_device and dmg_resolved in line:
            devices.append(current_device)
            current_device = None
    return devices


def _detach_hdiutil_target(target: str) -> None:
    result = subprocess.run(["hdiutil", "detach", target], check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return
    # A failed detach during retry cleanup should not hide the original attach
    # error, but logging it makes runner races easier to diagnose.
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"::warning::Best-effort hdiutil detach failed for {target}:\n{combined}")


def _cleanup_dmg_mount_attempt(dmg_path: Path, mount_dir: Path) -> None:
    _detach_hdiutil_target(str(mount_dir))
    for device in _matching_hdiutil_devices(dmg_path):
        _detach_hdiutil_target(device)
    shutil.rmtree(mount_dir, ignore_errors=True)


def _attach_dmg_with_retries(dmg_path: Path, *, attempts: int = 8) -> Path:
    last_result: subprocess.CompletedProcess[str] | None = None
    attempted_mountpoints: list[str] = []

    for attempt in range(1, attempts + 1):
        mount_dir = Path(tempfile.mkdtemp(prefix=f"token-place-dmg-mount-{attempt}-"))
        attempted_mountpoints.append(str(mount_dir))
        cmd = ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount_dir), str(dmg_path)]
        print(f"Attaching DMG for validation (attempt {attempt}/{attempts}) at {mount_dir}")
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return mount_dir

        last_result = result
        combined_output = f"{result.stdout}\n{result.stderr}"
        _cleanup_dmg_mount_attempt(dmg_path, mount_dir)
        if attempt >= attempts or not _is_transient_hdiutil_attach_error(combined_output):
            break

        retry_delay = min(2.0 * (2 ** (attempt - 1)), 15.0)
        print(
            f"::warning::hdiutil attach failed with a transient disk image error for {dmg_path}; "
            f"retrying attempt {attempt + 1}/{attempts} after {retry_delay:g}s.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        time.sleep(retry_delay)

    if last_result is None:
        _fail(f"hdiutil attach failed for {dmg_path}: no attempts were run")
    _fail(
        "hdiutil attach failed during DMG validation.\n"
        f"DMG path: {dmg_path}\n"
        f"Attach attempts: {len(attempted_mountpoints)}/{attempts}\n"
        f"Attempted mountpoints: {attempted_mountpoints}\n"
        f"Last stdout:\n{last_result.stdout}\n"
        f"Last stderr:\n{last_result.stderr}\n"
        f"hdiutil info snapshot:\n{_hdiutil_info_snapshot()}"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--app-path", required=True)
    p.add_argument("--dmg-path", required=True)
    p.add_argument("--tauri-config", required=True)
    p.add_argument("--expected-icon", required=True)
    p.add_argument("--expect-signing", action="store_true")
    p.add_argument("--expect-notarization", action="store_true")
    return p.parse_args()


def _validate_dmg_contents(dmg_path: Path, *, expect_signing: bool) -> None:
    if platform.system() != "Darwin":
        print("::warning::Skipping DMG mounted-content checks outside macOS.")
        return
    mount_dir = _attach_dmg_with_retries(dmg_path)
    try:
        root = mount_dir
        apps = sorted(p for p in root.iterdir() if p.is_dir() and p.suffix == ".app")
        if len(apps) != 1:
            _fail(f"DMG must contain exactly one .app at root; found {len(apps)}")
        readme_path = next((root / name for name in DMG_PREVIEW_README_NAMES if (root / name).is_file()), None)
        if readme_path is None:
            _fail(f"DMG must include one preview README at root: {DMG_PREVIEW_README_NAMES}")
        readme_text = readme_path.read_text(encoding="utf-8")
        missing = [phrase for phrase in DMG_PREVIEW_REQUIRED_PHRASES if phrase not in readme_text]
        if missing:
            _fail(f"DMG preview README missing required phrases: {missing}")
        if expect_signing:
            if DMG_PREVIEW_SIGNING_PHRASE_OPTIONS[1] not in readme_text:
                _fail("DMG preview README must describe configured Apple signing identity when --expect-signing is set")
        elif DMG_PREVIEW_SIGNING_PHRASE_OPTIONS[0] not in readme_text:
            _fail("DMG preview README must include ad-hoc signing guidance for unsigned preview builds")
    finally:
        _detach_hdiutil_target(str(mount_dir))
        shutil.rmtree(mount_dir, ignore_errors=True)


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
    _validate_dmg_contents(dmg_path, expect_signing=args.expect_signing)

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
