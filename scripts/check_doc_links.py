#!/usr/bin/env python3
"""Check Markdown files in docs/ for broken relative links."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

BROKEN: list[tuple[Path, str]] = []

for markdown_path in sorted(DOCS_DIR.rglob("*.md")):
    text = markdown_path.read_text(encoding="utf-8")
    for match in LINK_PATTERN.finditer(text):
        target = match.group(1).strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            continue
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        anchor_split = target.split("#", 1)
        relative_target = anchor_split[0]
        resolved = (markdown_path.parent / relative_target).resolve()
        if relative_target and not resolved.exists():
            BROKEN.append((markdown_path.relative_to(ROOT), target))

if BROKEN:
    for rel_path, link in BROKEN:
        print(f"Broken link in {rel_path}: {link}")
    sys.exit(1)

sys.exit(0)
