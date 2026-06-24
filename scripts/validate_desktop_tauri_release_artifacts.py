#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import plistlib
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


def _is_transient_attach_error(output: str) -> bool:
    lower_output = output.lower()
    return any(message.lower() in lower_output for message in DMG_ATTACH_TRANSIENT_MESSAGES)


def _hdiutil_info_snapshot() -> str:
    result = subprocess.run(["hdiutil", "info"], check=False, capture_output=True, text=True)
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        return f"hdiutil info failed with exit {result.returncode}:\n{output}"
    return output


def _redact_hdiutil_info(info: str) -> str:
    # Keep diagnostics useful while avoiding runner-local usernames or temp directory details.
    redacted = re.sub(r"/Users/[^/\s]+", "/Users/<redacted>", info)
    redacted = re.sub(r"/private/var/folders/[^\s]+", "/private/var/folders/<redacted>", redacted)
    return redacted


def _matching_hdiutil_devices(info: str, dmg_path: Path) -> list[str]:
    devices: list[str] = []
    current_device: str | None = None
    dmg_names = {str(dmg_path), str(dmg_path.resolve()), dmg_path.name}
    for line in info.splitlines():
        device_match = re.match(r"^(/dev/disk\S+)", line.strip())
        if device_match:
            current_device = device_match.group(1)
        if current_device and any(name in line for name in dmg_names):
            devices.append(current_device)
    return sorted(set(devices))


def _cleanup_dmg_attach_state(dmg_path: Path, mountpoint: Path) -> str:
    diagnostics: list[str] = []
    if mountpoint.exists():
        result = subprocess.run(
            ["hdiutil", "detach", str(mountpoint)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            diagnostics.append(_format_command_failure(["hdiutil", "detach", str(mountpoint)], result))

    info = _hdiutil_info_snapshot()
    for device in _matching_hdiutil_devices(info, dmg_path):
        result = subprocess.run(["hdiutil", "detach", device], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            diagnostics.append(_format_command_failure(["hdiutil", "detach", device], result))

    return "\n".join(diagnostics)


def _inspect_mounted_dmg(root: Path, *, expect_signing: bool) -> None:
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
            _fail(
                "DMG preview README must describe configured Apple signing identity when --expect-signing is set"
            )
    elif DMG_PREVIEW_SIGNING_PHRASE_OPTIONS[0] not in readme_text:
        _fail("DMG preview README must include ad-hoc signing guidance for unsigned preview builds")


def _attach_and_validate_dmg_with_retries(
    dmg_path: Path,
    *,
    expect_signing: bool,
    attempts: int = 8,
    delay_seconds: float = 2.0,
    max_delay_seconds: float = 15.0,
) -> None:
    last_result: subprocess.CompletedProcess[str] | None = None
    mountpoints: list[str] = []
    cleanup_messages: list[str] = []
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory(prefix="token-place-dmg-mount-") as mount_dir:
            mountpoint = Path(mount_dir)
            mountpoints.append(str(mountpoint))
            cleanup_output = _cleanup_dmg_attach_state(dmg_path, mountpoint)
            if cleanup_output:
                cleanup_messages.append(cleanup_output)

            cmd = ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mountpoint), str(dmg_path)]
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            last_result = result
            if result.returncode == 0:
                try:
                    _inspect_mounted_dmg(mountpoint, expect_signing=expect_signing)
                    return
                finally:
                    _run(["hdiutil", "detach", str(mountpoint)])

            combined_output = f"{result.stdout}\n{result.stderr}"
            if attempt >= attempts or not _is_transient_attach_error(combined_output):
                break

            retry_delay = min(delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
            print(
                f"::warning::hdiutil attach failed for {dmg_path} at {mountpoint} with a transient "
                f"disk image error; retrying attempt {attempt + 1}/{attempts} after {retry_delay:g}s.\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            cleanup_output = _cleanup_dmg_attach_state(dmg_path, mountpoint)
            if cleanup_output:
                cleanup_messages.append(cleanup_output)
            time.sleep(retry_delay)

    if last_result is None:
        _fail(f"hdiutil attach failed for {dmg_path}: no attempts were run")

    info = _redact_hdiutil_info(_hdiutil_info_snapshot())
    _fail(
        "DMG attach failed during macOS artifact validation.\n"
        f"DMG path: {dmg_path}\n"
        f"Mountpoints attempted: {mountpoints}\n"
        f"Attach attempts: {attempts}\n"
        f"Last stdout:\n{last_result.stdout}\n"
        f"Last stderr:\n{last_result.stderr}\n"
        f"Cleanup diagnostics:\n{chr(10).join(cleanup_messages) if cleanup_messages else '<none>'}\n"
        f"Redacted hdiutil info:\n{info}"
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
    _attach_and_validate_dmg_with_retries(dmg_path, expect_signing=expect_signing)


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
