#!/usr/bin/env python3
"""Build a local token.place desktop installer, mirroring desktop-release.yml.

One command produces a real packaged app/installer (not `tauri dev`), so
packaging-only bugs (embedded runtime PATH handling, packaged import-root
resolution, resource bundling) reproduce locally instead of only surfacing
after a ~45 minute GitHub Actions run.

Usage:
    python3 desktop-tauri/scripts/build_local.py [--dry-run] [--skip-install]
        [--skip-validate] [--fresh-runtime]

Must run on the target OS itself (macOS for the .app/.dmg, Windows for the
NSIS/MSI installers); this does not attempt cross-compilation.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DESKTOP_TAURI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DESKTOP_TAURI_DIR.parent


class BuildLocalError(RuntimeError):
    pass


@dataclass
class Step:
    description: str
    argv: list[str]
    cwd: Path


def _exe(name: str) -> str:
    """Resolve an executable via PATH (handles npm.cmd/python.exe on Windows,
    where subprocess without shell=True won't find a bare "npm")."""
    return shutil.which(name) or name


MIN_PYTHON = (3, 11)


def _python_version(exe: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [exe, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = (int(part) for part in result.stdout.split())
    except ValueError:
        return None
    return (major, minor)


def find_python(system: str) -> str | None:
    """Prefer a versioned interpreter (python3.12, python3.11, ...): a bare
    `python3`/`python` on PATH may resolve to a stale system stub (e.g. macOS's
    Xcode Command Line Tools Python) older than the 3.11+ these scripts require
    (they use tarfile.extractall(filter=...), added in Python 3.12/PEP 706)."""
    candidates = ["python3.13", "python3.12", "python3.11"]
    candidates += ["python"] if system == "Windows" else ["python3"]
    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        version = _python_version(exe)
        if version is not None and version >= MIN_PYTHON:
            return exe
    return None


def read_package_version(desktop_tauri_dir: Path = DESKTOP_TAURI_DIR) -> str:
    data = json.loads((desktop_tauri_dir / "package.json").read_text())
    return data["version"]


def git_short_sha(cwd: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        [_exe("git"), "rev-parse", "--short", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _macos_build_script() -> str:
    # Ports the "Build Tauri bundles" macOS branch of
    # .github/workflows/desktop-release.yml almost verbatim. Real Developer ID
    # signing only activates if APPLE_SIGNING_IDENTITY / APPLE_CERTIFICATE_P12_BASE64
    # / APPLE_CERTIFICATE_PASSWORD are already set in the environment; otherwise
    # falls back to ad-hoc signing, same as an unsigned CI preview build.
    return r"""
set -euo pipefail
CONFIGURED_APPLE_SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
APPLE_CERTIFICATE_P12_BASE64="${APPLE_CERTIFICATE_P12_BASE64:-}"
APPLE_CERTIFICATE_PASSWORD="${APPLE_CERTIFICATE_PASSWORD:-}"
APPLE_KEYCHAIN_PASSWORD="${APPLE_KEYCHAIN_PASSWORD:-}"
unset APPLE_SIGNING_IDENTITY
if [ -n "${CONFIGURED_APPLE_SIGNING_IDENTITY}" ] && [ -n "${APPLE_CERTIFICATE_P12_BASE64}" ] && [ -n "${APPLE_CERTIFICATE_PASSWORD}" ]; then
  TMP_DIR="$(mktemp -d)"
  KEYCHAIN_PATH="${TMP_DIR}/codesign.keychain-db"
  KEYCHAIN_PASSWORD="${APPLE_KEYCHAIN_PASSWORD:-temp_keychain_password}"
  CERT_PATH="${TMP_DIR}/apple-signing-cert.p12"
  echo "${APPLE_CERTIFICATE_P12_BASE64}" | base64 --decode > "${CERT_PATH}"
  security create-keychain -p "${KEYCHAIN_PASSWORD}" "${KEYCHAIN_PATH}"
  security set-keychain-settings -lut 21600 "${KEYCHAIN_PATH}"
  security unlock-keychain -p "${KEYCHAIN_PASSWORD}" "${KEYCHAIN_PATH}"
  security import "${CERT_PATH}" -k "${KEYCHAIN_PATH}" -P "${APPLE_CERTIFICATE_PASSWORD}" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple: -s -k "${KEYCHAIN_PASSWORD}" "${KEYCHAIN_PATH}"
  security list-keychains -d user -s "${KEYCHAIN_PATH}" $(security list-keychains -d user | tr -d '"')
  export TAURI_BUNDLE_MACOS_SIGNING_IDENTITY="${CONFIGURED_APPLE_SIGNING_IDENTITY}"
  export APPLE_SIGNING_IDENTITY="${CONFIGURED_APPLE_SIGNING_IDENTITY}"
else
  echo "No Apple signing identity configured; using ad-hoc signing for local preview build." >&2
  export TAURI_BUNDLE_MACOS_SIGNING_IDENTITY='-'
  export APPLE_SIGNING_IDENTITY='-'
fi
npm run tauri build -- --target aarch64-apple-darwin --bundles app
""".strip()


def _macos_stage_script(dmg_name: str, skip_validate: bool, python_exe: str) -> str:
    validate_block = "" if skip_validate else (
        f'"{python_exe}" ../scripts/validate_desktop_tauri_release_artifacts.py '
        '--app-only --require-embedded-python-runtime --app-path "${app_path}" '
        '--tauri-config src-tauri/tauri.conf.json '
        '--expected-icon src-tauri/icons/icon.icns ${signing_flag}\n'
    )
    # Re-derives signing_flag from the *original* env (not anything exported by
    # the earlier build step's own subshell), matching how the CI staging job
    # step independently re-reads secrets.APPLE_SIGNING_IDENTITY rather than
    # inheriting state across job steps.
    return rf"""
set -euo pipefail
mkdir -p release-artifacts
bundle_root="src-tauri/target/aarch64-apple-darwin/release/bundle"
app_dir="${{bundle_root}}/macos"
app_files=("${{app_dir}}"/*.app)
if [ ! -e "${{app_files[0]}}" ]; then
  echo "No .app bundle found in ${{app_dir}}" >&2
  exit 1
fi
app_path="${{app_files[0]}}"
codesign --verify --deep --strict --verbose=4 "${{app_path}}"
signing_flag=""
if [ -n "${{APPLE_SIGNING_IDENTITY:-}}" ] && [ "${{APPLE_SIGNING_IDENTITY:-}}" != "-" ]; then
  signing_flag="--expect-signing"
fi
{validate_block}dmg_stage_dir="$(mktemp -d)"
cp -R "${{app_path}}" "${{dmg_stage_dir}}/"
ln -s /Applications "${{dmg_stage_dir}}/Applications"
dmg_path="release-artifacts/{dmg_name}"
rm -f "${{dmg_path}}"
hdiutil create -volname "token.place desktop" -srcfolder "${{dmg_stage_dir}}" -ov -format UDZO "${{dmg_path}}"
echo "Built: ${{app_path}}"
echo "Built: ${{dmg_path}}"
""".strip()


def plan_macos_steps(
    desktop_tauri_dir: Path = DESKTOP_TAURI_DIR,
    *,
    skip_install: bool = False,
    skip_validate: bool = False,
) -> list[Step]:
    steps = [
        Step(
            "Add aarch64-apple-darwin Rust target",
            [_exe("rustup"), "target", "add", "aarch64-apple-darwin"],
            desktop_tauri_dir,
        ),
    ]
    if not skip_install:
        steps.append(Step("Install frontend dependencies (npm ci)", [_exe("npm"), "ci"], desktop_tauri_dir))
    python_exe = find_python("Darwin") or _exe("python3")
    steps.append(
        Step(
            "Prepare embedded macOS Python runtime",
            [python_exe, "scripts/prepare_embedded_python_runtime.py"],
            desktop_tauri_dir,
        )
    )
    steps.append(Step("Build frontend assets", [_exe("npm"), "run", "build"], desktop_tauri_dir))
    steps.append(
        Step(
            "Build Tauri bundles (.app, ad-hoc signed unless Apple identity is set)",
            [_exe("bash"), "-c", _macos_build_script()],
            desktop_tauri_dir,
        )
    )
    version = read_package_version(desktop_tauri_dir)
    try:
        short_sha = git_short_sha(desktop_tauri_dir)
    except (subprocess.CalledProcessError, FileNotFoundError):
        short_sha = "local"
    dmg_name = f"token.place-desktop-local-{version}-{short_sha}-apple-silicon.dmg"
    steps.append(
        Step(
            "Stage .app and build local .dmg",
            [_exe("bash"), "-c", _macos_stage_script(dmg_name, skip_validate, python_exe)],
            desktop_tauri_dir,
        )
    )
    return steps


def plan_windows_steps(
    desktop_tauri_dir: Path = DESKTOP_TAURI_DIR,
    *,
    skip_install: bool = False,
    fresh_runtime: bool = False,
) -> list[Step]:
    steps = [
        Step(
            "Add x86_64-pc-windows-msvc Rust target",
            [_exe("rustup"), "target", "add", "x86_64-pc-windows-msvc"],
            desktop_tauri_dir,
        ),
    ]
    if not skip_install:
        steps.append(Step("Install frontend dependencies (npm ci)", [_exe("npm"), "ci"], desktop_tauri_dir))

    python_runtime_exe = desktop_tauri_dir / "src-tauri" / "python-runtime" / "python.exe"
    if fresh_runtime or not python_runtime_exe.exists():
        python_exe = find_python("Windows") or _exe("python")
        steps.append(
            Step(
                "Prepare embedded Windows CUDA Python runtime",
                [python_exe, "scripts/prepare_windows_embedded_python_runtime.py"],
                desktop_tauri_dir,
            )
        )
    else:
        # Unlike the macOS prep script, this one has no internal "already valid"
        # short-circuit, so the wrapper skips it here instead of re-downloading
        # the pinned CUDA wheel + native DLLs on every local iteration.
        steps.append(Step(f"Skip embedded Windows runtime prep ({python_runtime_exe} already present; pass --fresh-runtime to force)", [], desktop_tauri_dir))

    steps.append(Step("Build frontend assets", [_exe("npm"), "run", "build"], desktop_tauri_dir))
    steps.append(
        Step(
            "Build Tauri bundles (NSIS + MSI)",
            [_exe("npm"), "run", "tauri", "build", "--", "--target", "x86_64-pc-windows-msvc"],
            desktop_tauri_dir,
        )
    )
    return steps


def stage_windows_artifacts(
    desktop_tauri_dir: Path = DESKTOP_TAURI_DIR,
    *,
    skip_validate: bool = False,
    dry_run: bool = False,
) -> list[Path]:
    bundle_root = desktop_tauri_dir / "src-tauri" / "target" / "x86_64-pc-windows-msvc" / "release" / "bundle"
    nsis_dir = bundle_root / "nsis"
    msi_dir = bundle_root / "msi"
    release_dir = desktop_tauri_dir / "release-artifacts"

    if dry_run:
        print(f"[dry-run] Stage installers: {nsis_dir}/*-setup.exe and {msi_dir}/*.msi -> {release_dir}")
        return []

    release_dir.mkdir(parents=True, exist_ok=True)
    setup_files = sorted(nsis_dir.glob("*-setup.exe"))
    if not setup_files:
        raise BuildLocalError(f"No setup EXE found in {nsis_dir}")
    msi_files = sorted(msi_dir.glob("*.msi"))
    if not msi_files:
        raise BuildLocalError(f"No MSI found in {msi_dir}")

    staged = []
    for src in (*setup_files, *msi_files):
        dest = release_dir / src.name
        shutil.copy2(src, dest)
        staged.append(dest)
        print(f"Built: {dest}")

    if not skip_validate:
        version = read_package_version(desktop_tauri_dir)
        nsis_dest = next(p for p in staged if p.name.endswith("-setup.exe"))
        msi_dest = next(p for p in staged if p.suffix == ".msi")
        cmd = [
            find_python("Windows") or _exe("python"),
            str(REPO_ROOT / "scripts" / "validate_windows_desktop_release_artifacts.py"),
            "--windows-nsis", str(nsis_dest),
            "--windows-msi", str(msi_dest),
            "--expected-version", version,
        ]
        print(f"==> Validate Windows installer artifacts")
        result = subprocess.run(cmd, cwd=desktop_tauri_dir)
        if result.returncode != 0:
            raise BuildLocalError("Windows artifact validation failed")

    return staged


def run_steps(steps: list[Step], *, dry_run: bool) -> None:
    for step in steps:
        if not step.argv:
            print(f"==> {step.description}")
            continue
        if dry_run:
            print(f"[dry-run] {step.description}: {' '.join(step.argv)}")
            continue
        print(f"==> {step.description}")
        started = time.monotonic()
        try:
            result = subprocess.run(step.argv, cwd=step.cwd)
        except FileNotFoundError as exc:
            raise BuildLocalError(f"'{step.argv[0]}' not found on PATH ({step.description})") from exc
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            raise BuildLocalError(f"step failed ({elapsed:.1f}s): {step.description}")
        print(f"    done in {elapsed:.1f}s")


PREREQUISITE_HINTS = {
    "rustup": "https://rustup.rs (installs both rustup and cargo)",
    "npm": "https://nodejs.org (Node 20, matching desktop-release.yml's pin)",
    "python3": (
        "no python3.11+ found on PATH (checked python3.13/python3.12/python3.11/python3; "
        "a bare `python3` that resolves to an older stub doesn't count). Install via "
        "https://www.python.org/downloads/ or `brew install python@3.12`"
    ),
    "python": (
        "no python3.11+ found on PATH (checked python3.13/python3.12/python3.11/python). "
        "Install via https://www.python.org/downloads/"
    ),
}


def check_prerequisites(system: str) -> list[str]:
    missing = [tool for tool in ("rustup", "npm") if shutil.which(tool) is None]
    if find_python(system) is None:
        missing.append("python3" if system == "Darwin" else "python")
    return missing


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-install", action="store_true", help="skip `npm ci`")
    parser.add_argument(
        "--fresh-runtime",
        action="store_true",
        help="force re-running the embedded Python runtime prep script (Windows: always re-downloads; macOS: already fast/idempotent, this has no extra effect there)",
    )
    parser.add_argument("--skip-validate", action="store_true", help="skip post-build artifact validation")
    parser.add_argument("--dry-run", action="store_true", help="print the planned commands without running them")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    system = platform.system()

    if system == "Darwin":
        steps = plan_macos_steps(skip_install=args.skip_install, skip_validate=args.skip_validate)
    elif system == "Windows":
        steps = plan_windows_steps(skip_install=args.skip_install, fresh_runtime=args.fresh_runtime)
    else:
        print(f"Unsupported platform for a packaged desktop build: {system}", file=sys.stderr)
        return 1

    if not args.dry_run:
        missing = check_prerequisites(system)
        if missing:
            print("error: missing required tools:", file=sys.stderr)
            for tool in missing:
                print(f"  - {tool}: install from {PREREQUISITE_HINTS.get(tool, '(see desktop-tauri/README.md)')}", file=sys.stderr)
            return 1

    try:
        run_steps(steps, dry_run=args.dry_run)
        if system == "Windows":
            stage_windows_artifacts(skip_validate=args.skip_validate, dry_run=args.dry_run)
    except BuildLocalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
