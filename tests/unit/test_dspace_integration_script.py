import os
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "integration_tests" / "run_dspace_integration.sh"


@pytest.mark.unit
def test_run_dspace_integration_script_dry_run():
    assert SCRIPT_PATH.exists(), (
        "Expected integration_tests/run_dspace_integration.sh to exist per testing roadmap"
    )
    assert os.access(SCRIPT_PATH, os.X_OK), "run_dspace_integration.sh should be executable"

    result = subprocess.run(
        ["bash", str(SCRIPT_PATH), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )

    output_lines = result.stdout.splitlines()

    expected_markers = [
        "Cloning token.place repository...",
        "Cloning DSPACE repository...",
        "Setting up Python virtual environment...",
        "Installing DSPACE dependencies...",
        "Creating token.place client package...",
        "Running integration tests...",
    ]

    for marker in expected_markers:
        assert any(marker in line for line in output_lines), (
            f"Expected dry run output to include step marker: {marker}"
        )
