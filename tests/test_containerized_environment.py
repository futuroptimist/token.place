"""Tests for the containerized testing helper tooling."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.unit
def test_containerized_test_runner_dry_run_outputs_spec(tmp_path):
    """The dry-run flag should describe the containerized test execution plan."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_tests_in_container.py"

    assert script_path.exists(), "Expected container test runner script to be present"

    result = subprocess.run(
        [sys.executable, str(script_path), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "Dry-run output should include a JSON payload"

    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:  # pragma: no cover - ensures helpful error message
        raise AssertionError(f"Dry-run output was not valid JSON: {result.stdout}") from exc

    assert payload["dockerfile"].endswith("docker/test-runner.Dockerfile")
    assert payload["image"].startswith("token.place-test-runner")
    assert payload["workdir"].endswith("/workspace")
    assert payload["runtime"] in {"docker", "podman"}
    assert any("run_all_tests.sh" in token for token in payload["command"])
