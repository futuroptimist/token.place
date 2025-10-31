"""Utilities for validating documentation links."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

LINK_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)|\[[^\]]+\]\(([^)]+)\)")
_SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")


def _iter_markdown_files(targets: Sequence[Path]) -> Iterable[Path]:
    for target in targets:
        path = Path(target)
        if path.is_dir():
            yield from path.rglob("*.md")
        elif path.suffix.lower() == ".md" and path.exists():
            yield path


def find_broken_markdown_links(targets: Sequence[Path]) -> List[Tuple[Path, str]]:
    """Return a list of (markdown_file, link) tuples for missing relative links."""

    repo_root = Path(__file__).resolve().parents[2]
    broken: List[Tuple[Path, str]] = []

    for md_file in _iter_markdown_files(targets):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue

        for match in LINK_PATTERN.finditer(text):
            raw_link = next(filter(None, match.groups()))
            link = raw_link.strip()

            if not link or link.startswith("#"):
                continue

            if link.startswith(_SKIP_SCHEMES):
                continue

            # Strip optional link title (`path "title"`).
            link = link.split()[0]

            link_path, _, _ = link.partition("#")
            if not link_path:
                continue

            if link_path.startswith("//"):
                # Protocol-relative URLs should be skipped as external links.
                continue

            candidate = (md_file.parent / link_path).resolve()

            try:
                candidate.relative_to(repo_root)
            except ValueError:
                # Ignore links that resolve outside the repository.
                continue

            if not candidate.exists():
                broken.append((md_file.relative_to(repo_root), raw_link))

    return broken

