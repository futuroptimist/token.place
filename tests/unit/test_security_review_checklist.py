"""Ensure the security review checklist documents promised safeguards."""
from pathlib import Path

import pytest

CHECKLIST_PATH = Path(__file__).resolve().parents[2] / "docs" / "SECURITY_REVIEW_CHECKLIST.md"


def test_security_review_checklist_exists():
    assert CHECKLIST_PATH.exists(), "docs/SECURITY_REVIEW_CHECKLIST.md should exist"


def test_security_review_checklist_covers_required_topics():
    content = CHECKLIST_PATH.read_text(encoding="utf-8")
    required_sections = [
        "Relay failovers",
        "Cloudflare fallback",
        "Key management",
        "Secrets boundaries",
        "Logging redaction",
        "Audit steps",
    ]
    for section in required_sections:
        assert section in content, f"Expected `{section}` guidance in the security checklist"


@pytest.mark.parametrize(
    "phrase",
    [
        "verify failover pairs",
        "document fallback owner",
        "rotate operator keys",
    ],
)
def test_security_review_checklist_includes_actionable_prompts(phrase):
    """Checklist should nudge reviewers toward concrete actions."""
    content = CHECKLIST_PATH.read_text(encoding="utf-8")
    assert phrase in content
