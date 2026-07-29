#!/usr/bin/env python3
"""Artifact-backed Windows RTX gate driven through the installed Tauri UI."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _canonical_model_contract() -> dict[str, object]:
    # Read the repository-owned profile rather than duplicating release metadata.
    from utils.llm.model_profiles import QWEN3_8B_PROFILE_ID, get_model_profile

    profile = get_model_profile(QWEN3_8B_PROFILE_ID)
    if profile is None:
        raise RuntimeError("canonical Qwen3 model profile is missing")
    return profile


def validate_canonical_model(model: Path) -> Path:
    profile = _canonical_model_contract()
    resolved = model.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.name != profile["filename"]:
        raise RuntimeError("model is not the canonical Qwen3 8B Q4_K_M artifact")
    if resolved.stat().st_size != profile["artifact_size_bytes"]:
        raise RuntimeError("canonical Qwen3 model size mismatch")
    hasher = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != profile["artifact_sha256"]:
        raise RuntimeError("canonical Qwen3 model SHA-256 mismatch")
    return resolved


def materialize_nsis(installer: Path, install_root: Path) -> Path:
    if not sys.platform.startswith("win"):
        raise RuntimeError("the packaged RTX gate requires Windows")
    if installer.suffix.lower() != ".exe" or not installer.is_file():
        raise RuntimeError("--installer must be the built NSIS setup executable")
    subprocess.run(
        [str(installer.resolve()), "/S", f"/D={install_root.resolve()}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    candidates = [
        path
        for path in install_root.rglob("*.exe")
        if path.is_file()
        and not path.name.lower().startswith(("unins", "uninstall"))
        and "python-runtime" not in {part.lower() for part in path.parts}
    ]
    if len(candidates) != 1:
        raise RuntimeError("installed package must contain exactly one Tauri executable")
    return candidates[0].resolve(strict=True)


def _find_uninstaller(install_root: Path) -> Path | None:
    """Locate the NSIS-generated uninstaller, top-level only.

    NSIS installers commonly emit either `uninstall.exe` or an `unins*.exe`
    variant depending on template; accept both. Constrained to the
    installation root (not rglob) so a nested `python-runtime` dependency
    cannot be mistaken for the package uninstaller.
    """
    candidates = sorted(
        path
        for pattern in ("unins*.exe", "uninstall*.exe")
        for path in install_root.glob(pattern)
    )
    return candidates[0] if candidates else None


def _uninstall_installed_package(install_root: Path, primary_exc: BaseException | None) -> None:
    uninstaller = _find_uninstaller(install_root)
    if uninstaller is None:
        return
    try:
        subprocess.run([str(uninstaller), "/S"], check=True)
    except Exception as cleanup_exc:
        if primary_exc is not None:
            # The harness failure is the reportable result; note cleanup
            # also failed without masking it.
            print(f"warning: uninstall cleanup also failed: {cleanup_exc}", file=sys.stderr)
            return
        raise


def run_installed_hardware_gate(
    installer: Path, model: Path, context_tier: str
) -> None:
    model = validate_canonical_model(model)
    with tempfile.TemporaryDirectory(prefix="token-place-rtx-installed-") as temp:
        install_root = Path(temp)
        app_binary = materialize_nsis(installer, install_root)
        command = [
            sys.executable,
            str(_repo_root() / "desktop-tauri/scripts/test_desktop_operator_ui_e2e.py"),
            "--packaged-windows-nvidia-hardware",
            "--app-binary",
            str(app_binary),
            "--model",
            str(model),
            "--context-tier",
            context_tier,
        ]
        primary_exc: BaseException | None = None
        try:
            subprocess.run(command, cwd=_repo_root(), check=True)
        except BaseException as exc:
            primary_exc = exc
        finally:
            _uninstall_installed_package(install_root, primary_exc)
        if primary_exc is not None:
            raise primary_exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--mode", choices=["gpu"], default="gpu")
    parser.add_argument("--context-tier", choices=["8k-fast", "64k-full"], required=True)
    parser.add_argument("--artifact-root", type=Path)  # retained for workflow compatibility
    args = parser.parse_args()
    try:
        run_installed_hardware_gate(args.installer, args.model, args.context_tier)
    except Exception as exc:
        print(json.dumps({"result": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"result": "passed", "context_tier": args.context_tier}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
