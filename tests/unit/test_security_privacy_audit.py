from __future__ import annotations

import re
from pathlib import Path


AUDIT_LOG = Path("docs/SECURITY_PRIVACY_AUDIT.md")


def test_oct_30_2025_audit_has_commit_hash() -> None:
    content = AUDIT_LOG.read_text(encoding="utf-8")
    match = re.search(r"^### \[2025-10-30\] - commit (?P<hash>.+)$", content, flags=re.MULTILINE)
    assert match is not None, "Audit log entry for 2025-10-30 is missing"
    commit_hash = match.group("hash")
    assert re.fullmatch(r"[0-9a-f]{40}", commit_hash), (
        "Expected 2025-10-30 audit entry to record a 40-character commit hash, "
        f"found '{commit_hash}'."
    )
