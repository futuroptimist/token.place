"""Automated static security scanning guardrail."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

SCAN_TARGETS = (
    "server",
    "utils",
    "encrypt.py",
    "server.py",
    "relay.py",
)


@pytest.mark.crypto
@pytest.mark.security
@pytest.mark.slow
def test_codebase_is_free_from_medium_bandit_issues() -> None:
    """Run Bandit and ensure medium/high severity findings are absent."""
    cmd = [
        sys.executable,
        "-m",
        "bandit",
        "-q",
        "-r",
        *SCAN_TARGETS,
        "--severity-level",
        "medium",
        "--format",
        "json",
    ]

    result = subprocess.run(cmd, check=False, capture_output=True, text=True)

    assert result.stdout, (
        "Bandit did not produce JSON output. stderr was: "
        f"{result.stderr.strip()}"
    )

    payload = json.loads(result.stdout)

    offending = [
        issue
        for issue in payload.get("results", [])
        if issue.get("issue_severity", "").lower() in {"medium", "high"}
    ]

    formatted = "\n".join(
        f"{item['filename']}:{item['line_number']} {item['issue_text']}"
        for item in offending
    )

    assert not offending, (
        "Bandit found medium/high severity issues:\n" + formatted
    )
