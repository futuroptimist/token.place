"""Lightweight dependency audit helpers used in security-focused tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from pkg_resources import parse_version

# Minimum safe versions derived from recent CVE disclosures.
# Each entry documents the corresponding advisory to aid future updates.
MINIMUM_SECURE_VERSIONS: Dict[str, Tuple[str, str]] = {
    # CVE-2023-45803: request smuggling when < 2.31.0
    "requests": ("2.31.0", "CVE-2023-45803"),
    # CVE-2023-43804: HTTP request smuggling via urllib3 < 1.26.18
    "urllib3": ("1.26.18", "CVE-2023-43804"),
    # GHSA-2227-5r88-5ww9: denial of service in Flask < 2.2.5
    "Flask": ("2.2.5", "GHSA-2227-5r88-5ww9"),
    # GHSA-288c-mx3g-f745: httpx < 0.27.0
    "httpx": ("0.27.0", "GHSA-288c-mx3g-f745"),
    # GHSA-hggm-jpg3-v476: cryptography < 41.0.4
    "cryptography": ("41.0.4", "GHSA-hggm-jpg3-v476"),
}

_REQUIREMENT_PATTERN = re.compile(
    r"^\s*"  # optional leading whitespace
    r"(?P<name>[A-Za-z0-9_.-]+)"  # package name
    r"\s*==\s*"  # strict pin operator
    r"(?P<version>[A-Za-z0-9_.+-]+)"  # version literal
)


def _iter_pinned_requirements(lines: Iterable[str]) -> Iterable[Tuple[str, str]]:
    """Yield ``(name, version)`` pairs for strict ``package==version`` entries."""

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _REQUIREMENT_PATTERN.match(raw_line)
        if not match:
            continue

        yield match.group("name"), match.group("version")


def validate_requirements(path: Path) -> List[str]:
    """Validate ``requirements.txt`` style files against security baselines."""

    issues: List[str] = []
    contents = path.read_text(encoding="utf-8").splitlines()

    for package, current_version in _iter_pinned_requirements(contents):
        metadata = MINIMUM_SECURE_VERSIONS.get(package)
        if not metadata:
            continue

        minimum_version, advisory = metadata
        if parse_version(current_version) < parse_version(minimum_version):
            issues.append(
                (
                    f"{package}=={current_version} is below secure baseline "
                    f"{minimum_version} ({advisory})"
                )
            )

    return issues


__all__ = ["MINIMUM_SECURE_VERSIONS", "validate_requirements"]
