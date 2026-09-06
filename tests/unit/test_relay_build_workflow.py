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
    assert any(
        {"linux/amd64", "linux/arm64"}.issubset(platforms)
        for platforms in platform_sets
    )

    assert (
        "sha-${short_sha}" in workflow_text
    ), "Workflow should publish immutable sha-* tags"
    assert (
        "main-${short_sha}" in workflow_text
    ), "Workflow should publish immutable main-* tags"
    assert (
        "main-latest" in workflow_text
    ), "Workflow should publish the documented convenience tag"

    labels_block = _extract_block(workflow_text, "labels")
    assert any(
        line.strip().startswith("org.opencontainers.image.licenses=")
        for line in labels_block.splitlines()
    ), "OCI metadata should declare the image license"


def test_every_release_tag_is_created_only_after_artifact_safety_gate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    build = workflow.index("Build and push quarantined multi-arch relay candidate")
    gate = workflow.index("Gate pushed candidate before release-tag publication")
    publish = workflow.index("Publish qualified candidate under release tags")

    assert build < gate < publish
    assert "scripts/relay_release_safety_gate.py" in workflow[gate:publish]
    assert "tags: ${{ needs.build-and-smoke.outputs.publish_tags }}" not in workflow
    assert (
        'docker buildx imagetools create --tag "${tag}" "${CANDIDATE_IMAGE}"'
        in workflow[publish:]
    )


def test_release_gate_records_provenance_and_requires_evidence() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for value in ("--source-commit", "--release-ref", "--release-base", "--evidence"):
        assert workflow.count(value) == 2
    assert "if-no-files-found: error" in workflow
    assert "candidate_digest" in workflow
    assert "full_sha: ${{ steps.tags.outputs.full_sha }}" in workflow
