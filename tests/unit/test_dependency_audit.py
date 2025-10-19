from pathlib import Path

import pytest

from utils.security import dependency_audit


@pytest.mark.security
@pytest.mark.unit
def test_requirements_file_meets_security_baselines():
    issues = dependency_audit.validate_requirements(Path("requirements.txt"))
    assert issues == [], "Expected requirements.txt to meet minimum security baselines"


@pytest.mark.security
@pytest.mark.unit
def test_validate_requirements_flags_insecure_version(tmp_path):
    sample = tmp_path / "req.txt"
    sample.write_text("urllib3==1.26.0\nrequests==2.32.5\n", encoding="utf-8")

    issues = dependency_audit.validate_requirements(sample)

    assert any("urllib3" in issue for issue in issues), "Expected urllib3 issue to be reported"
    assert all("requests" not in issue for issue in issues), "Requests should remain compliant"


@pytest.mark.security
@pytest.mark.unit
def test_validate_requirements_is_case_insensitive(tmp_path):
    sample = tmp_path / "req.txt"
    sample.write_text("CRYPTOGRAPHY==41.0.3\n", encoding="utf-8")

    issues = dependency_audit.validate_requirements(sample)

    assert issues, "Expected insecure cryptography version to be flagged"
    assert all("CRYPTOGRAPHY" in issue for issue in issues)


@pytest.mark.security
@pytest.mark.unit
def test_validate_requirements_flags_recent_advisories(tmp_path):
    sample = tmp_path / "req.txt"
    sample.write_text("tqdm==4.66.1\nidna==3.6\n", encoding="utf-8")

    issues = dependency_audit.validate_requirements(sample)

    assert any("tqdm" in issue for issue in issues), "Expected tqdm advisory to be enforced"
    assert any("idna" in issue for issue in issues), "Expected idna advisory to be enforced"
