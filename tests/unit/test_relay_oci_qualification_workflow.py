from __future__ import annotations

import re
from pathlib import Path

import yaml


WORKFLOW_PATH = Path(".github/workflows/qualify-relay-oci.yml")


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_is_manual_only_and_read_only() -> None:
    text = _workflow_text()
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "packages": "read"}
    assert "environment:" not in text
    assert "deployments:" not in text
    assert not re.search(r"permissions:[\s\S]*?\b(?:write|id-token|issues|pull-requests):", text)


def test_inputs_are_bounded_and_never_form_an_arbitrary_repository() -> None:
    text = _workflow_text()
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]

    assert set(inputs) == {
        "index_digest",
        "source_commit",
        "release_ref",
        "release_base",
        "evidence_label",
    }
    assert all(value["required"] is True for value in inputs.values())
    assert text.count("OCI_REPOSITORY: ghcr.io/futuroptimist/tokenplace-relay") == 1
    assert "INPUT_INDEX_DIGEST: ${{ inputs.index_digest }}" in text
    assert 'r"sha256:[0-9a-f]{64}"' in text
    assert 'r"[0-9a-f]{40}"' in text
    assert "{0,127}" in text and "{0,63}" in text


def test_exact_descriptors_are_inspected_pulled_and_qualified_both_times() -> None:
    text = _workflow_text()

    assert 'docker buildx imagetools inspect --raw "${index_coordinate}"' in text
    assert 'len(digests) != 1' in text
    assert 'unexpected executable platform' in text
    assert '"linux/amd64|${AMD64_DIGEST}|15100"' in text
    assert '"linux/arm64|${ARM64_DIGEST}|15200"' in text
    assert 'docker pull --platform "${platform}" "${platform_coordinate}"' in text
    gate_block = text[text.index("for specification in "):text.index("export QUALIFICATION_STATUS")]
    assert gate_block.count("scripts/relay_release_safety_gate.py") == 1
    assert '--image "${platform_coordinate}"' in gate_block
    assert '--resolved-revision "${INPUT_SOURCE_COMMIT}"' in gate_block
    assert '--registry-coordinate "${platform_coordinate}"' in gate_block
    assert '--index-digest "${INPUT_INDEX_DIGEST}"' in gate_block
    assert '--platform-digest "${platform_digest}"' in gate_block


def test_evidence_fails_closed_and_is_uploaded_even_on_failure() -> None:
    text = _workflow_text()

    assert 'evidence.get("passed") is True' in text
    assert 'evidence.get("cleanup") == "passed"' in text
    assert "list(results) == expected" in text
    assert "len(results) == len(expected)" in text
    assert "object_pairs_hook=unique_object" in text
    assert "source_commit" in text and "registry_coordinate" in text
    upload = text.index("Upload immutable qualification evidence")
    assert "if: always()" in text[upload:upload + 160]
    assert "if-no-files-found: error" in text[upload:]
    assert "SHA256SUMS" in text and "metadata.json" in text
    assert "cleanup-failed" in text


def test_workflow_contains_no_build_publish_promotion_or_external_access() -> None:
    text = _workflow_text().lower()
    forbidden = (
        "docker build ",
        "docker push",
        "imagetools create",
        "kubectl",
        "helm ",
        "cloudflare",
        "prometheus",
        "grafana",
        "staging",
        "production",
        "packages: write",
    )
    assert all(command not in text for command in forbidden)
    assert "docker tag" not in text
    assert "docker/setup-qemu-action@v3" in text
    assert "docker/setup-buildx-action@v3" in text
    assert "persist-credentials: false" in text
