#!/usr/bin/env python3
"""Validate an extracted .deb without installing or activating a desktop app."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

import prepare_linux_embedded_python_runtime as linux


REQUIRED_DEB_DEPENDENCIES = {"libwebkit2gtk-4.1-0", "libgtk-3-0", "libgomp1"}


def validate_dependencies(deb: Path) -> None:
    result = subprocess.run(
        ["dpkg-deb", "--field", str(deb.resolve()), "Depends"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    dependencies = set()
    for clause in result.stdout.split(","):
        match = re.fullmatch(r"([a-z0-9+.-]+)(?:\s+\([^()]+\))?", clause.strip())
        if match:
            dependencies.add(match.group(1))
    missing = REQUIRED_DEB_DEPENDENCIES - dependencies
    if missing:
        raise RuntimeError(f"missing required Debian dependencies: {', '.join(sorted(missing))}")


def validate_tree(root: Path, expected_version: str, expected_build_id: str) -> None:
    runtimes = list(root.glob("usr/lib/*/python-runtime"))
    if len(runtimes) != 1:
        raise RuntimeError("expected exactly one app-private Linux runtime")
    runtime = runtimes[0]
    manifest = linux.load_manifest()
    provenance = json.loads((runtime / linux.shared.PROVENANCE).read_text())
    for key, expected in {
        "runtime_id": linux.RUNTIME_ID,
        "expected_backend": "cpu",
        "build_profile": linux.PROFILE,
        "source_archive_sha256": manifest["sha256"],
        "target_triple": manifest["target_triple"],
    }.items():
        if provenance.get(key) != expected:
            raise RuntimeError(f"invalid Linux runtime provenance: {key}")
    if provenance.get("files") != linux.file_hashes(runtime):
        raise RuntimeError("Linux runtime file integrity mismatch")
    linux.validate_runtime_notices(runtime, manifest)
    linux.validate_runtime(runtime, manifest)
    binaries = list((root / "usr/bin").iterdir())
    if len(binaries) != 1:
        raise RuntimeError("expected exactly one desktop executable")
    result = linux.shared.run([str(binaries[0]), "--build-identity-json"], timeout=30)
    identity = json.loads(result.stdout)
    for key, value in {
        "app_version": expected_version,
        "build_id": expected_build_id,
        "target_triple": manifest["target_triple"],
        "bundled_runtime_id": linux.RUNTIME_ID,
    }.items():
        if identity.get(key) != value:
            raise RuntimeError(f"Linux desktop identity mismatch: {key}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-build-id", required=True)
    args = parser.parse_args(argv)
    validate_dependencies(args.deb)
    with tempfile.TemporaryDirectory(prefix="token-place-linux-package-") as directory:
        root = Path(directory)
        subprocess.run(["dpkg-deb", "--extract", str(args.deb.resolve()), str(root)], check=True)
        validate_tree(root, args.expected_version, args.expected_build_id)


if __name__ == "__main__":
    main()
