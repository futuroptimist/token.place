#!/usr/bin/env python3
"""Lightweight secret scanner for staged diffs."""

from __future__ import annotations

import re
import sys
from typing import Iterable

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Generic secret", re.compile(r"(?i)secret\s*[:=]\s*['\"]?[A-Za-z0-9\+/]{12,}")),
    ("Private key header", re.compile(r"-----BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY-----")),
)


def scan(lines: Iterable[str]) -> int:
    flagged = []
    for line_no, line in enumerate(lines, start=1):
        for name, pattern in PATTERNS:
            if pattern.search(line):
                flagged.append((line_no, name, line.rstrip()))
                break
    if not flagged:
        return 0

    for line_no, name, content in flagged:
        print(f"[secret-scan] line {line_no}: possible {name}: {content}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    exit_code = scan(sys.stdin)
    sys.exit(exit_code)
