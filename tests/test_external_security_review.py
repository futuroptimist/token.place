"""Regression tests enforcing the external security review process."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _load_review_metadata(repo_root: Path) -> dict:
    report_path = repo_root / "docs" / "security" / "external_security_review.json"
    assert report_path.exists(), (
        "Expected external security review metadata at docs/security/external_security_review.json"
    )
    with report_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def test_external_security_review_bandit_is_clean():
    repo_root = Path(__file__).resolve().parents[1]
    # parents[1] resolves to the repository root because this file lives in tests/
    review = _load_review_metadata(repo_root)

    required_fields = {
        "tool",
        "version",
        "review_date",
        "reviewed_paths",
        "expected_issue_count",
        "expected_maximum_severity",
    }
    missing = sorted(required_fields - review.keys())
    assert not missing, f"External review metadata is missing required fields: {missing}"

    assert review["tool"].lower() == "bandit", "External review must use Bandit static analysis"

    # Ensure the recorded review date is valid and not in the future.
    recorded_date = date.fromisoformat(review["review_date"])
    assert recorded_date <= date.today(), "External review date cannot be in the future"

    version_result = subprocess.run(
        ["bandit", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    actual_version = version_result.stdout.strip().split()[-1]
    assert (
        actual_version == review["version"]
    ), f"Bandit version mismatch: expected {review['version']}, got {actual_version}"

    reviewed_paths = [Path(path) for path in review["reviewed_paths"]]
    assert reviewed_paths, "External review metadata must list reviewed paths"

    bandit_command = [
        "bandit",
        "-q",
        "-f",
        "json",
        "-r",
        *[str(repo_root / path) for path in reviewed_paths],
    ]
    scan = subprocess.run(bandit_command, capture_output=True, text=True)
    assert scan.returncode == 0, f"Bandit reported findings or failed: {scan.stderr}"

    try:
        report = json.loads(scan.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive logging
        pytest.fail(
            "Bandit output was not valid JSON: "
            f"{exc}.\nSTDOUT:\n{scan.stdout}\nSTDERR:\n{scan.stderr}"
        )

    results = report.get("results", [])
    assert (
        len(results) == review["expected_issue_count"]
    ), "Unexpected number of findings in external security review"

    highest_severity = max(
        (_SEVERITY_RANK.get(item.get("issue_severity", "LOW"), 0) for item in results),
        default=0,
    )
    allowed_severity = _SEVERITY_RANK[review["expected_maximum_severity"]]
    assert (
        highest_severity <= allowed_severity
    ), "Bandit reported an issue above the allowed severity threshold"

