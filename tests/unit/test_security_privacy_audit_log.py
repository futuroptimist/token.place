"""Regression tests for the security audit log documentation."""
from pathlib import Path
import re


AUDIT_DOC = Path('docs/SECURITY_PRIVACY_AUDIT.md')


def _get_audit_section(date: str) -> str:
    """Return the markdown section for the given audit date heading."""
    text = AUDIT_DOC.read_text(encoding='utf-8')
    lines = text.splitlines()
    heading_prefix = f"### [{date}]"

    for index, line in enumerate(lines):
        if line.startswith(heading_prefix):
            end = len(lines)
            for cursor in range(index + 1, len(lines)):
                if lines[cursor].startswith('### '):
                    end = cursor
                    break
            return '\n'.join(lines[index:end])
    return ''


def test_2025_10_10_entry_has_commit_hash():
    """The October 10, 2025 audit entry should include the reviewed commit hash."""
    section = _get_audit_section('2025-10-10')
    assert section, 'Expected to find the 2025-10-10 audit entry in docs/SECURITY_PRIVACY_AUDIT.md.'

    heading_line = section.splitlines()[0]
    match = re.match(r"^### \[2025-10-10\] - commit ([0-9a-f]{7,40})$", heading_line)
    assert match, (
        'The 2025-10-10 audit heading must record a concrete commit hash '
        '(e.g. `commit 0123456789abcdef`).'
    )

    assert 'commit TBD' not in section, 'Stale placeholder `commit TBD` found in audit entry.'


def test_2025_08_09_entry_has_commit_hash():
    """The August 9, 2025 audit entry should include the reviewed commit hash."""
    section = _get_audit_section('2025-08-09')
    assert section, 'Expected to find the 2025-08-09 audit entry in docs/SECURITY_PRIVACY_AUDIT.md.'

    heading_line = section.splitlines()[0]
    match = re.match(r"^### \[2025-08-09\] - commit ([0-9a-f]{7,40})$", heading_line)
    assert match, (
        'The 2025-08-09 audit heading must record a concrete commit hash '
        '(e.g. `commit 0123456789abcdef`).'
    )

    assert 'commit TBD' not in section, 'Stale placeholder `commit TBD` found in audit entry.'
