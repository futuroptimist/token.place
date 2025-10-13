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
