from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_PATH = Path(".github/workflows/qualify-existing-relay-oci.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_dispatch_only_and_read_only_permissions() -> None:
    text = _workflow()
    trigger = text[text.index("on:") : text.index("permissions:")]
    permissions = text[text.index("permissions:") : text.index("jobs:")]

    assert "workflow_dispatch:" in trigger
    assert not re.search(r"^  (push|pull_request|schedule):", trigger, re.MULTILINE)
    assert permissions.strip() == "permissions:\n  contents: read\n  packages: read"
    assert "packages: write" not in text
    assert not re.search(r"(?:deployments|id-token|issues|pull-requests):\s*write", text)


def test_inputs_are_environment_bound_and_strictly_validated() -> None:
    text = _workflow()
    for name in ("index_digest", "source_commit", "release_ref", "release_base", "evidence_label"):
        assert f"      {name}:" in text
        assert f"UNTRUSTED_{name.upper()}: ${{{{ inputs.{name} }}}}" in text
    validate = text[text.index("Validate untrusted dispatch inputs") : text.index("Set up Docker Buildx")]
    assert 'os.environ["UNTRUSTED_INDEX_DIGEST"]' in validate
    assert r'sha256:[0-9a-f]{64}' in validate
    assert r'[0-9a-f]{40}' in validate
    assert "{0,126}" in validate and "{0,46}" in validate
    assert '".." in value' in validate and '"//" in value' in validate
    assert "${{ inputs." not in validate


def test_fixed_repository_and_exact_descriptor_membership() -> None:
    text = _workflow()
    assert "REGISTRY_REPOSITORY: ghcr.io/futuroptimist/tokenplace-relay" in text
    assert "repository:" not in text[text.index("workflow_dispatch:") : text.index("permissions:")]
    resolve = text[text.index("Inspect and resolve the immutable index") : text.index("Pull and qualify linux/amd64")]
    assert 'imagetools inspect --raw "${coordinate}"' in resolve
    assert 'wanted = {("linux", "amd64"): [], ("linux", "arm64"): []}' in resolve
    assert "unexpected executable platform" in resolve
    assert "any(len(digests) != 1" in resolve
    assert "index-manifest.sanitized.json" in resolve


def test_both_platforms_are_pulled_and_gated_by_descriptor() -> None:
    text = _workflow()
    amd64 = text[text.index("Pull and qualify linux/amd64") : text.index("Pull and qualify linux/arm64")]
    arm64 = text[text.index("Pull and qualify linux/arm64") : text.index("Clean local qualification resources")]
    for platform, port, block in (("linux/amd64", "15120", amd64), ("linux/arm64", "15220", arm64)):
        assert f'docker pull --platform {platform} "${{coordinate}}"' in block
        assert f"--platform {platform}" in block
        assert '--resolved-revision "${SOURCE_COMMIT}"' in block
        assert '--registry-coordinate "${coordinate}"' in block
        assert '--index-digest "${INDEX_DIGEST}"' in block
        assert '--platform-digest "${PLATFORM_DIGEST}"' in block
        assert f"--port {port}" in block
        assert "continue-on-error: true" in block


def test_evidence_is_fail_closed_uploaded_on_failure_and_summarized() -> None:
    text = _workflow()
    verify = text[text.index("Verify and package qualification evidence") : text.index("Upload immutable")]
    assert 'evidence.get("passed") is True' in verify
    assert "keys == expected_ids" in verify
    assert 'result.get("state") == "passed"' in verify
    assert 'os.environ.get("CLEANUP_OUTCOME") == "success"' in verify
    for identity in ("source_commit", "release_ref", "release_base", "index_digest", "platform_digest"):
        assert f'evidence.get("{identity}")' in verify
    assert "SHA256SUMS" in verify and 'root / "metadata.json"' in verify

    upload = text[text.index("Upload immutable") : text.index("Write qualification summary")]
    assert "if: always()" in upload
    assert "overwrite: false" in upload
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in upload
    enforce = text[text.index("Enforce complete qualification") :]
    assert "if: always()" in enforce
    assert 'test "${QUALIFICATION_PASSED}" = true' in enforce
    summary = text[text.index("Write qualification summary") : text.index("Enforce complete qualification")]
    for field in ("Source commit", "Index digest", "linux/amd64 descriptor", "linux/arm64 descriptor", "Gate implementation commit", "Evidence artifact"):
        assert field in summary


def test_executor_has_no_publish_build_promotion_or_environment_access() -> None:
    text = _workflow().lower()
    forbidden = (
        "docker build ",
        "docker push",
        "imagetools create",
        "kubectl",
        "helm ",
        "terraform",
        "prometheus",
        "grafana",
        "cloudflare",
        "production url",
    )
    assert not any(command in text for command in forbidden)
    assert "ref: refs/heads/main" in text
    assert "docker/setup-qemu-action@v3" in text
    assert "docker/login-action@v3" in text
