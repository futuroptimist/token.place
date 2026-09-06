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
    assert "candidate-index.json" in qualification
    assert '[[ "${advertised[*]}" == "linux/amd64 linux/arm64" ]]' in qualification
    assert 'docker pull --platform "${platform}" "${platform_coordinate}"' in qualification
    assert '--platform "${platform}"' in qualification
    assert '--platform-digest "${platform_digest}"' in qualification
    assert '--index-digest "${{ steps.push.outputs.digest }}"' in qualification
    assert "relay-release-safety-publish-${platform_tag}-evidence.json" in qualification
    promotion_block = workflow_text[promotion:]
    assert 'CANDIDATE: ${{ needs.build-and-smoke.outputs.repository }}@${{ steps.push.outputs.digest }}' in promotion_block
    assert "docker buildx imagetools create" in promotion_block


def test_gate_failure_blocks_all_promotions_and_uses_selected_provenance() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    qualify = text.index("Pull and qualify every candidate platform")
    promote = text.index("Promote qualified candidate to release tags")
    block = text[qualify:promote]
    assert "set -euo pipefail" in block
    assert "|| true" not in block
    assert "continue-on-error" not in block
    assert "org.opencontainers.image.revision=${{ needs.build-and-smoke.outputs.full_sha }}" in text
    assert '--release-ref "${{ needs.build-and-smoke.outputs.selected_ref }}"' in block
    assert '--release-base "${{ needs.build-and-smoke.outputs.release_base }}"' in block
    assert "inputs.ref" in text and "inputs.base" in text
