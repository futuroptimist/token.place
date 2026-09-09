from __future__ import annotations

import re
from pathlib import Path


WORKFLOW = Path(".github/workflows/qualify-relay-oci.yml")


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_manual_only_and_read_only_permissions() -> None:
    text = _text()
    trigger = text[text.index("on:") : text.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger and "pull_request:" not in trigger and "schedule:" not in trigger
    permissions = text[text.index("permissions:") : text.index("jobs:")]
    assert permissions.strip() == "permissions:\n  contents: read\n  packages: read"
    assert not re.search(r"(?m)^\s+(?:contents|packages|deployments|id-token|issues|pull-requests): write$", text)


def test_inputs_are_bounded_before_use_and_repository_is_fixed() -> None:
    text = _text()
    assert text.count("ghcr.io/futuroptimist/tokenplace-relay") == 1
    assert "registry/repository" not in text
    for name in ("index_digest", "source_commit", "release_ref", "release_base", "evidence_label"):
        assert f"      {name}:" in text
        assert f"INPUT_{name.upper()}: ${{{{ inputs.{name} }}}}" in text
    validate = text[text.index("Record gate commit and validate") : text.index("Set up QEMU")]
    assert r"^sha256:[0-9a-f]{64}$" in validate
    assert r"^[0-9a-f]{40}$" in validate
    assert "{0,127}" in validate and "{0,62}" in validate
    assert '[[ "${value}" != *..*' in validate
    # Inputs occur in executable shell only through quoted environment variables.
    assert "${{ inputs." not in text[text.index("run: |") :]


def test_exact_descriptors_are_inspected_pulled_and_qualified_without_mutation() -> None:
    text = _text()
    assert 'docker buildx imagetools inspect --raw "${coordinate}"' in text
    assert 'docker pull --platform "${platform}" "${coordinate}"' in text
    assert 'coordinate="${OCI_REPOSITORY}@${platform_digest}"' in text
    assert "duplicate executable platform" in text
    assert "unexpected executable platform" in text
    assert "application/vnd.oci.image.manifest.v1+json" in text
    assert "application/vnd.docker.distribution.manifest.v2+json" in text
    assert "platform descriptor is not an executable image manifest" in text
    assert 'set(selected) != {"linux/amd64", "linux/arm64"}' in text
    gate = text[text.index("Pull and qualify both immutable") : text.index("Verify evidence")]
    assert "for platform in linux/amd64 linux/arm64" in gate
    for argument in (
        "--platform", "--source-commit", "--release-ref", "--release-base",
        "--resolved-revision", "--registry-coordinate", "--index-digest", "--platform-digest",
    ):
        assert argument in gate
    assert "port=55100" in gate and "port=55200" in gate
    forbidden = (
        "docker build ", "docker push", "imagetools create", "docker tag", "kubectl ",
        "helm ", "gh workflow run", "gh api ", "curl ",
    )
    assert not any(command in text for command in forbidden)


def test_evidence_fails_closed_and_uploads_even_on_failure() -> None:
    text = _text()
    verify = text[text.index("Verify evidence") : text.index("Upload the immutable")]
    assert 'evidence.get("passed")' not in verify  # Exact identity map includes literal True.
    assert '"passed": True, "cleanup": "passed"' in verify
    assert "set(results) != set(required)" in verify
    assert "mandatory result did not pass" in verify
    assert "source_commit" in verify and "index_digest" in verify and "platform_digest" in verify
    assert "forbidden" in verify and "field_names(evidence)" in verify
    assert "path.unlink(missing_ok=True)" in verify
    assert 'names = ["index-manifest.json", "relay-release-safety-amd64-evidence.json", "relay-release-safety-arm64-evidence.json", "qualification-metadata.json"]' in verify
    assert 'out / "SHA256SUMS"' in verify
    upload = text[text.index("Upload the immutable") : text.index("Write qualification summary")]
    assert "if: always()" in upload
    assert "if-no-files-found: error" in upload
    final = text[text.index("Enforce qualification result") :]
    assert "steps.gates.outcome" in final and "steps.evidence.outcome" in final


def test_main_gate_commit_cleanup_and_safe_summary_are_explicit() -> None:
    text = _text()
    checkout = text[text.index("Check out the main-branch") : text.index("Record gate commit")]
    assert "ref: main" in checkout and "persist-credentials: false" in checkout
    action_uses = re.findall(r"(?m)^\s+uses:\s+([^\s#]+)", text)
    assert action_uses and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in action_uses)
    assert 'gate_commit="$(git rev-parse HEAD)"' in text
    assert "docker rm -f" in text and 'cleanup="failed"' in text
    assert "--filter 'name=relay-safety-'" in text
    assert 'value.setdefault("cleanup", detected_cleanup)' in text
    assert "rm -f validated-inputs.json" in text and "rm -rf qualification-artifacts" in text
    assert "steps.cleanup.outcome" in text
    assert "Source:" in text and "AMD64 descriptor:" in text and "ARM64 descriptor:" in text
    assert "Gate implementation commit:" in text and "Evidence artifact:" in text
