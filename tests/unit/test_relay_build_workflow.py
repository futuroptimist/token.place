from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/ci-image.yml")


def _extract_block(text: str, header: str) -> str:
    pattern = rf"^[ \t]*{header}:\s*\|\n((?:[ \t]+.+\n)+)"
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise AssertionError(f"Could not find block for {header!r}")
    return match.group(1)


def test_relay_image_workflow_targets_multi_arch_and_ghcr_metadata() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    platform_sets = [
        {part.strip() for part in match.split(",") if part.strip()}
        for match in re.findall(r"platforms:\s*([^\n]+)", workflow_text)
    ]
    assert platform_sets, "Build steps should declare target platforms"
    assert any({"linux/amd64", "linux/arm64"}.issubset(platforms) for platforms in platform_sets)

    assert "sha-${short_sha}" in workflow_text, "Workflow should publish immutable sha-* tags"
    assert "main-${short_sha}" in workflow_text, "Workflow should publish immutable main-* tags"
    assert "main-latest" in workflow_text, "Workflow should publish the documented convenience tag"

    labels_block = _extract_block(workflow_text, "labels")
    assert any(
        line.strip().startswith("org.opencontainers.image.licenses=")
        for line in labels_block.splitlines()
    ), "OCI metadata should declare the image license"


def test_release_safety_gate_precedes_every_relay_image_publish() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    gate_index = workflow_text.index("scripts/relay_release_safety_gate.py")
    publish_job_index = workflow_text.index("\n  publish:")
    push_index = workflow_text.index("push: true")

    assert gate_index < publish_job_index < push_index
    assert "needs: build-and-smoke" in workflow_text
    assert "relay-release-safety-evidence" in workflow_text
    assert "steps.publish-image.outputs.digest" in workflow_text
