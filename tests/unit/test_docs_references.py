"""Regression tests for documentation file references."""
from __future__ import annotations

import pathlib
import re
from typing import List

ROOT = pathlib.Path(__file__).resolve().parents[2]
MD_GLOB = "*.md"
PIP_INSTALL_PATTERN = re.compile(r"pip\s+install\s+-r\s+([^\s`]+)")


def _candidate_paths(md_path: pathlib.Path, reference: str) -> List[pathlib.Path]:
    """Return possible filesystem targets for a documented file reference."""
    cleaned = reference.strip().strip("'\"")
    cleaned = cleaned.rstrip(',')
    if cleaned.startswith(('http://', 'https://', '#')):
        return []
    if '$' in cleaned:
        return []
    candidates: List[pathlib.Path] = []
    relative = pathlib.Path(cleaned)
    candidates.append(md_path.parent / relative)
    if cleaned.startswith(('./', '../')):
        normalized = (md_path.parent / relative).resolve()
        if normalized.is_relative_to(ROOT):
            candidates.append(normalized)
    else:
        candidates.append(ROOT / cleaned.lstrip('/'))
    return candidates


def test_pip_install_references_point_to_existing_files() -> None:
    """Every documented pip install command should point to an existing file."""
    missing: list[tuple[pathlib.Path, str]] = []
    for md_path in ROOT.rglob(MD_GLOB):
        if "node_modules" in md_path.parts:
            continue
        text = md_path.read_text(encoding="utf-8")
        for match in PIP_INSTALL_PATTERN.finditer(text):
            reference = match.group(1)
            candidates = _candidate_paths(md_path, reference)
            if not candidates:
                continue
            if any(candidate.exists() for candidate in candidates):
                continue
            missing.append((md_path.relative_to(ROOT), reference))
    assert not missing, (
        "Documentation references nonexistent requirement files: "
        + ", ".join(f"{path} -> {ref}" for path, ref in missing)
    )
