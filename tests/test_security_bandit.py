from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.security
@pytest.mark.crypto
def test_bandit_reports_no_medium_or_high_findings():
    """Ensure the Bandit security scan passes without medium/high issues."""
    repo_root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable,
        "-m",
        "bandit",
        "-q",
        "-r",
        str(repo_root),
        "-f",
        "json",
        "--severity-level",
        "medium",
        "--confidence-level",
        "medium",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_root))

    assert result.returncode == 0, (
        "Bandit returned a non-zero exit code.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    try:
        report = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        pytest.fail(
            "Failed to parse Bandit JSON output:\n"
            f"Error: {exc}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    offending = [
        issue
        for issue in report.get("results", [])
        if issue.get("issue_severity", "").upper() in {"MEDIUM", "HIGH"}
    ]

    assert not offending, "Bandit found medium/high severity findings: {offending}".format(
        offending=[issue.get("test_id") for issue in offending]
    )
