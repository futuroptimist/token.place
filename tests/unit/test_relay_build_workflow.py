from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/build.yml")


def _extract_block(text: str, header: str) -> str:
    pattern = rf"^[ \t]*{header}:\s*\|\n((?:[ \t]+.+\n)+)"
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise AssertionError(f"Could not find block for {header!r}")
    return match.group(1)


def test_relay_build_workflow_targets_multi_arch_and_ghcr_metadata() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    platforms_match = re.search(r"platforms:\s*([^\n]+)", workflow_text)
    assert platforms_match is not None, "Build step should declare target platforms"
    platforms = {part.strip() for part in platforms_match.group(1).split(",") if part.strip()}
    assert {"linux/amd64", "linux/arm64"}.issubset(platforms)

    tags_block = _extract_block(workflow_text, "tags")
    assert any(
        line.strip().startswith("type=sha")
        for line in tags_block.splitlines()
    ), "Metadata step should publish immutable sha-* tags"

    labels_block = _extract_block(workflow_text, "labels")
    assert any(
        line.strip().startswith("org.opencontainers.image.licenses=")
        for line in labels_block.splitlines()
    ), "OCI metadata should declare the image license"
