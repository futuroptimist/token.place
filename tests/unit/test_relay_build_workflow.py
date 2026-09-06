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


def test_every_relay_image_publish_is_blocked_on_artifact_safety_gate() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    gate = workflow_text.index("python scripts/relay_release_safety_gate.py")
    publish_job = workflow_text.index("\n  publish:")
    push = workflow_text.index("push: true", publish_job)
    exact_gate = workflow_text.index("Pull and qualify every candidate platform", push)
    promotion = workflow_text.index("Promote qualified candidate to release tags", exact_gate)

    assert gate < publish_job < push < exact_gate < promotion
    assert "needs: build-and-smoke" in workflow_text[publish_job:push]
    assert "if-no-files-found: error" in workflow_text
    assert "steps.push.outputs.digest" in workflow_text
    qualification = workflow_text[exact_gate:promotion]
    assert "for platform in linux/amd64 linux/arm64" in qualification
    assert 'docker pull --platform "${platform}" "${candidate}"' in qualification
    assert "relay-release-safety-publish-${platform_tag}-evidence.json" in qualification
    assert "docker buildx imagetools create" in workflow_text[promotion:]
