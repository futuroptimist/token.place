"""Docker-level regression proof against the canonical incident artifacts."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path("scripts/relay_release_safety_gate.py").resolve()
ARTIFACTS = [
    ("6c39adc64e7bed4f85d07164aa2860e637919ca9", True),
    ("e46277daaeb76beeb9f2a2e9e265181287239b22", False),
]


@pytest.mark.parametrize(("revision", "expected_pass"), ARTIFACTS)
def test_canonical_relay_artifact_behavior(tmp_path: Path, revision: str, expected_pass: bool) -> None:
    """Build immutable historical trees and qualify them through the production gate CLI."""
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for artifact regression proof")
    if subprocess.run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], check=False).returncode:
        pytest.fail(f"required canonical commit is absent from the checkout: {revision}")

    context = tmp_path / revision
    context.mkdir()
    archive = subprocess.Popen(["git", "archive", revision], stdout=subprocess.PIPE)
    assert archive.stdout is not None
    subprocess.run(["tar", "-x", "-C", str(context)], stdin=archive.stdout, check=True)
    assert archive.wait() == 0
    image = f"tokenplace-relay:artifact-{revision[:12]}"
    subprocess.run([
        "docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
        "-t", image, str(context),
    ], check=True)
    evidence = tmp_path / f"{revision}.json"
    completed = subprocess.run([
        sys.executable, str(GATE), "--image", image, "--platform", "linux/amd64",
        "--source-commit", revision, "--release-ref", revision,
        "--release-base", "release/relay-0.1.1", "--evidence", str(evidence),
    ], check=False)
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["passed"] is expected_pass
    assert (completed.returncode == 0) is expected_pass
