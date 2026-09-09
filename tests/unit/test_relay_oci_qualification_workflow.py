from __future__ import annotations

import re
from pathlib import Path


WORKFLOW = Path(".github/workflows/qualify-relay-oci-index.yml")


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_manual_only_with_read_only_permissions_and_fixed_repository() -> None:
    text = _text()
    assert re.search(r"^on:\n  workflow_dispatch:\n", text, re.MULTILINE)
    assert "pull_request:" not in text and "push:" not in text
    permissions = text[text.index("permissions:") : text.index("jobs:")]
    assert permissions.strip() == "permissions:\n  contents: read\n  packages: read"
    assert "OCI_REPOSITORY: ghcr.io/futuroptimist/tokenplace-relay" in text
    assert "environment:" not in text


def test_inputs_are_environment_bound_and_strictly_validated() -> None:
    text = _text()
    for name in ("index_digest", "source_commit", "release_ref", "release_base", "evidence_label"):
        assert f"{name}:" in text
        assert f"INPUT_{name.upper()}: ${{{{ inputs.{name} }}}}" in text
    validation = text[text.index("Validate untrusted dispatch inputs") : text.index("Check out main-branch")]
    assert r'sha256:[0-9a-f]{64}' in validation
    assert r'[0-9a-f]{40}' in validation
    assert "{0,126}" in validation and "{0,47}" in validation
    assert "ref: main" in text
    assert "git rev-parse HEAD" in text


def test_exact_two_descriptors_are_pulled_and_gated_by_digest() -> None:
    text = _text()
    qualify = text[text.index("Inspect and qualify immutable") : text.index("Validate and package")]
    assert 'imagetools inspect --raw "${coordinate}"' in qualify
    assert 'set(selected) != {"linux/amd64", "linux/arm64"}' in qualify
    assert "unexpected or duplicate executable platform" in qualify
    assert "for platform in linux/amd64 linux/arm64" in qualify
    assert 'docker pull --platform "${platform}" "${platform_coordinate}"' in qualify
    assert qualify.count("python scripts/relay_release_safety_gate.py") == 1
    for argument in ("--source-commit", "--release-ref", "--release-base", "--resolved-revision", "--registry-coordinate", "--index-digest", "--platform-digest", "--port", "--evidence"):
        assert argument in qualify
    assert "port=15100" in qualify and "port=16100" in qualify


def test_evidence_fails_closed_and_uploads_even_on_failure() -> None:
    text = _text()
    package = text[text.index("Validate and package") : text.index("Upload immutable")]
    assert "object_pairs_hook=unique" in package
    assert 'list(checks) != required' in package
    assert 'report.get("passed") is not True' in package
    assert 'report.get("cleanup") == "failed"' in package
    assert "evidence identity binding mismatch" in package
    upload = text[text.index("Upload immutable") : text.index("Enforce qualification")]
    assert "if: always()" in upload
    assert "actions/upload-artifact@v4" in upload
    assert "metadata.json" in upload and "SHA256SUMS" in upload
    assert "if-no-files-found: warn" in upload
    assert text.index("Enforce qualification result") > text.index("Upload immutable")


def test_executor_contains_no_registry_or_environment_mutation() -> None:
    text = _text().lower()
    forbidden = (
        "docker build ", "docker push", "imagetools create", "kubectl", "helm ",
        "deployment", "packages: write", "contents: write", "id-token: write",
        "issues: write", "pull-requests: write",
    )
    for command in forbidden:
        assert command not in text
