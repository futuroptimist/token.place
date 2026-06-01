from __future__ import annotations

from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/ci-image.yml")


def test_ci_image_workflow_targets_multi_arch_and_ghcr_metadata() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "file: Dockerfile" in workflow_text
    assert "ghcr.io/futuroptimist/tokenplace-relay" in workflow_text
    assert "linux/amd64,linux/arm64" in workflow_text
    assert "${repository}:main-${short_sha}" in workflow_text
    assert "${repository}:main-latest" in workflow_text
    assert "${repository}:sha-${short_sha}" in workflow_text
    assert "${repository}:${semver_tag}" in workflow_text
    assert "org.opencontainers.image.source=" in workflow_text
    assert "org.opencontainers.image.revision=" in workflow_text
    assert "org.opencontainers.image.created=" in workflow_text
    assert "push: true" in workflow_text
    assert "push: false" in workflow_text
