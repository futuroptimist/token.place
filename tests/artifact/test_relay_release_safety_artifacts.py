"""Docker-level regression proof against the canonical incident artifacts."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path("scripts/relay_release_safety_gate.py").resolve()
RECOVERY = "6c39adc64e7bed4f85d07164aa2860e637919ca9"
HISTORICAL = "e46277daaeb76beeb9f2a2e9e265181287239b22"
METRICS_IDS = {
    "metrics.valid_instrumentation",
    "metrics.no_flask_defaults",
    "metrics.no_raw_paths",
    "metrics.bounded_unmatched_paths",
}
QUOTA_IDS = {
    "quota.public_information_exempt",
    "quota.protected_rate_limited",
    "quota.protected_daily_limited",
    "quota.mutating_rate_limited",
    "quota.mutating_daily_limited",
}


def _require_artifact_runtime() -> None:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required; artifact regression proof must not be skipped")
    result = subprocess.run(["docker", "info"], check=False, capture_output=True)
    if result.returncode:
        pytest.fail("a running Docker daemon is required for artifact regression proof")


def _export(revision: str, context: Path) -> None:
    if subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"], check=False
    ).returncode:
        pytest.fail(f"required canonical commit is absent from the checkout: {revision}")
    context.mkdir()
    archive = subprocess.Popen(["git", "archive", revision], stdout=subprocess.PIPE)
    assert archive.stdout is not None
    subprocess.run(["tar", "-x", "-C", str(context)], stdin=archive.stdout, check=True)
    assert archive.wait() == 0


def _replace_once(path: Path, old: str | re.Pattern[str], new: str) -> None:
    original = path.read_text(encoding="utf-8")
    changed, count = re.subn(old, new, original, count=1)
    assert count == 1, f"expected exactly one safety mutation target in {path}"
    path.write_text(changed, encoding="utf-8")


def _build_and_qualify(tmp_path: Path, revision: str, variant: str = "original") -> dict:
    context = tmp_path / f"{revision}-{variant}"
    _export(revision, context)
    if variant == "flask-defaults":
        _replace_once(context / "relay.py", "metrics_export_defaults=False", "metrics_export_defaults=True")
    elif variant == "quota-exemptions-removed":
        _replace_once(
            context / "api" / "__init__.py",
            re.compile(r"PUBLIC_INFORMATION_RATE_LIMIT_EXEMPT_PATHS = frozenset\(\s*\{.*?\}\s*\)", re.DOTALL),
            "PUBLIC_INFORMATION_RATE_LIMIT_EXEMPT_PATHS = frozenset()",
        )

    image = f"tokenplace-relay:artifact-{revision[:12]}-{variant}"
    subprocess.run(
        ["docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
         "-t", image, str(context)],
        check=True,
    )
    evidence = tmp_path / f"{revision}-{variant}.json"
    completed = subprocess.run(
        [sys.executable, str(GATE), "--image", image, "--platform", "linux/amd64",
         "--source-commit", revision, "--release-ref", revision,
         "--release-base", "release/relay-0.1.1", "--evidence", str(evidence)],
        check=False,
    )
    report = json.loads(evidence.read_text(encoding="utf-8"))
    report["returncode"] = completed.returncode
    return report


def _assert_executed(report: dict) -> None:
    assert report.get("image_id", "").startswith("sha256:")
    assert set(report["results"]) == METRICS_IDS | QUOTA_IDS
    assert all(result["state"] != "not_run" for result in report["results"].values())
    assert report.get("error_category") in {None, "mandatory_check_failed"}


def test_6c39adc_recovery_passes_every_contract(tmp_path: Path) -> None:
    _require_artifact_runtime()
    report = _build_and_qualify(tmp_path, RECOVERY)
    _assert_executed(report)
    assert report["returncode"] == 0
    assert report["passed"] is True
    assert all(result["passed"] for result in report["results"].values())


def test_historical_artifact_fails_both_incident_contracts(tmp_path: Path) -> None:
    _require_artifact_runtime()
    report = _build_and_qualify(tmp_path, HISTORICAL)
    _assert_executed(report)
    assert report["returncode"] != 0
    assert report["passed"] is False
    assert report["results"]["metrics.valid_instrumentation"]["passed"] is False
    assert report["results"]["quota.public_information_exempt"]["passed"] is False
    assert report["results"]["quota.protected_rate_limited"]["passed"] is True
    assert report["results"]["quota.protected_daily_limited"]["passed"] is True


def test_reenabled_flask_defaults_fails_only_metrics_incident(tmp_path: Path) -> None:
    _require_artifact_runtime()
    report = _build_and_qualify(tmp_path, RECOVERY, "flask-defaults")
    _assert_executed(report)
    assert report["returncode"] != 0
    assert report["passed"] is False
    # Flask defaults expose raw paths and keep adding path-specific series, so
    # this single mutation intentionally violates all three related contracts.
    for key in (
        "metrics.no_flask_defaults",
        "metrics.no_raw_paths",
        "metrics.bounded_unmatched_paths",
    ):
        assert report["results"][key]["passed"] is False
    assert report["results"]["metrics.valid_instrumentation"]["passed"] is True
    assert all(report["results"][key]["passed"] for key in QUOTA_IDS)


def test_removed_public_exemptions_fails_only_quota_incident(tmp_path: Path) -> None:
    _require_artifact_runtime()
    report = _build_and_qualify(tmp_path, RECOVERY, "quota-exemptions-removed")
    _assert_executed(report)
    assert report["returncode"] != 0
    assert report["results"]["quota.public_information_exempt"]["passed"] is False
    assert all(report["results"][key]["passed"] for key in METRICS_IDS | (QUOTA_IDS - {"quota.public_information_exempt"}))
