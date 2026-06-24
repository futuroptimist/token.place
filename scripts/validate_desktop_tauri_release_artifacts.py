#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import subprocess
import shutil
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



def _run_best_effort(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def _is_transient_hdiutil_attach_error(output: str) -> bool:
    transient_markers = (
        "resource temporarily unavailable",
        "resource busy",
        "device busy",
        "busy",
        "already attached",
        "already mounted",
        "couldn't open",
        "could not open",
    )
    lowered = output.lower()
    return any(marker in lowered for marker in transient_markers)


def _hdiutil_info_snapshot() -> str:
    result = _run_best_effort(["hdiutil", "info"])
    snapshot = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
    return snapshot or "<empty hdiutil info output>"


def _matching_dmg_devices(dmg_path: Path) -> list[str]:
    result = _run_best_effort(["hdiutil", "info", "-plist"])
    if result.returncode != 0:
        return []
    try:
        info = plistlib.loads(result.stdout.encode("utf-8"))
    except Exception:
        return []

    resolved_dmg = str(dmg_path.resolve())
    devices: list[str] = []
    for image in info.get("images", []):
        image_path = image.get("image-path") or image.get("imagePath")
        if not image_path:
            continue
        try:
            image_matches = str(Path(image_path).resolve()) == resolved_dmg
        except OSError:
            image_matches = str(image_path) == str(dmg_path)
        if not image_matches:
            continue
        for entity in image.get("system-entities", []):
            dev_entry = entity.get("dev-entry")
            if dev_entry:
                devices.append(str(dev_entry))
    return devices


def _detach_dmg_mounts(dmg_path: Path, mountpoint: Path | None = None) -> list[str]:
    cleanup_messages: list[str] = []
    detach_targets: list[str] = []
    if mountpoint is not None:
        detach_targets.append(str(mountpoint))
    detach_targets.extend(_matching_dmg_devices(dmg_path))

    seen: set[str] = set()
    for target in detach_targets:
        if target in seen:
            continue
        seen.add(target)
        result = _run_best_effort(["hdiutil", "detach", target])
        if result.returncode != 0:
            result = _run_best_effort(["hdiutil", "detach", "-force", target])
        cleanup_messages.append(
            f"detach {target}: exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
        )
    return cleanup_messages


def _attach_dmg_with_retries(
    dmg_path: Path,
    *,
    attempts: int = 8,
    delay_seconds: float = 2.0,
    max_delay_seconds: float = 15.0,
) -> tempfile.TemporaryDirectory[str]:
    mount_root = tempfile.TemporaryDirectory(prefix="token-place-dmg-mount-root-")
    attempted_mountpoints: list[str] = []
    cleanup_log: list[str] = []
    last_result: subprocess.CompletedProcess[str] | None = None

    for attempt in range(1, attempts + 1):
        mountpoint = Path(mount_root.name) / f"attempt-{attempt}"
        if mountpoint.exists():
            shutil.rmtree(mountpoint, ignore_errors=True)
        mountpoint.mkdir(parents=True, exist_ok=False)
        attempted_mountpoints.append(str(mountpoint))

        cmd = ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mountpoint), str(dmg_path)]
        print(f"Attaching DMG for validation (attempt {attempt}/{attempts}): {dmg_path} -> {mountpoint}")
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return mount_root

        last_result = result
        combined_output = f"{result.stdout}\n{result.stderr}"
        if attempt >= attempts or not _is_transient_hdiutil_attach_error(combined_output):
            break

        retry_delay = min(delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
        print(
            f"::warning::hdiutil attach failed with a transient disk image error on attempt "
            f"{attempt}/{attempts}; cleaning up and retrying after {retry_delay:g}s.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        cleanup_log.extend(_detach_dmg_mounts(dmg_path, mountpoint))
        shutil.rmtree(mountpoint, ignore_errors=True)
        time.sleep(retry_delay)

    info_snapshot = _hdiutil_info_snapshot()
    mount_root.cleanup()
    if last_result is None:
        _fail(f"hdiutil attach failed for {dmg_path}: no attempts were run")
    _fail(
        "Failed to attach DMG for validation after bounded retries.\n"
        f"DMG: {dmg_path}\n"
        f"Attach attempts: {len(attempted_mountpoints)}/{attempts}\n"
        f"Mountpoints attempted: {attempted_mountpoints}\n"
        f"Last stdout:\n{last_result.stdout}\n"
        f"Last stderr:\n{last_result.stderr}\n"
        f"Cleanup log:\n" + "\n".join(cleanup_log[-8:]) + "\n"
        f"hdiutil info snapshot:\n{info_snapshot}"
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
    mount_root = _attach_dmg_with_retries(dmg_path)
    mount_dir = Path(mount_root.name) / "attempt-1"
    for candidate in sorted(Path(mount_root.name).iterdir()):
        if candidate.is_dir():
            mount_dir = candidate
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
        _detach_dmg_mounts(dmg_path, mount_dir)
        mount_root.cleanup()


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
