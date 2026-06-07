"""Regression checks for token.place Sugarkube release gates."""
from __future__ import annotations

import re
from pathlib import Path

import yaml


CHART_PATH = Path("charts/tokenplace/Chart.yaml")
SUGARKUBE_VERSION_PATH = Path("docs/apps/tokenplace.version")
SUGARKUBE_PROD_TAG_PATH = Path("docs/apps/tokenplace.prod.tag")
STAGING_RUNBOOK = Path("docs/k3s-sugarkube-staging.md")
PROD_RUNBOOK = Path("docs/k3s-sugarkube-prod.md")
ONBOARDING_RUNBOOK = Path("docs/relay_sugarkube_onboarding.md")
RELEASE_RUNBOOK = Path("docs/ops/sugarkube-release.md")


IMMUTABLE_TAG_RE = re.compile(r"^(?:main-[0-9a-f]{7,40}|sha-[0-9a-f]{7,40}|v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
MUTABLE_TAGS = {"latest", "main-latest", "staging", "prod", "production"}


def _is_immutable_release_tag(tag: str) -> bool:
    return bool(IMMUTABLE_TAG_RE.fullmatch(tag)) and tag not in MUTABLE_TAGS


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_sugarkube_chart_pin_matches_current_chart_source() -> None:
    """The documented Sugarkube chart pin must not drift from the chart source."""
    chart = yaml.safe_load(CHART_PATH.read_text(encoding="utf-8"))
    sugarkube_pin = SUGARKUBE_VERSION_PATH.read_text(encoding="utf-8").strip()

    assert chart["version"] == "0.1.1"
    assert sugarkube_pin == chart["version"]

    for runbook in (STAGING_RUNBOOK, PROD_RUNBOOK, ONBOARDING_RUNBOOK, RELEASE_RUNBOOK):
        text = _squash(runbook.read_text(encoding="utf-8"))
        assert "chart version `0.1.1`" in text or "chart package version `0.1.1`" in text


def test_prod_tag_file_is_empty_and_docs_require_explicit_immutable_tag() -> None:
    """An empty prod tag file is allowed only with an explicit immutable promotion tag."""
    assert SUGARKUBE_PROD_TAG_PATH.exists()
    assert SUGARKUBE_PROD_TAG_PATH.read_text(encoding="utf-8").strip() == ""

    release_text = _squash(RELEASE_RUNBOOK.read_text(encoding="utf-8"))
    prod_text = _squash(PROD_RUNBOOK.read_text(encoding="utf-8"))

    assert "explicit immutable tag" in release_text
    assert "explicit immutable tag" in prod_text
    for mutable in MUTABLE_TAGS:
        assert mutable in release_text
        assert mutable in prod_text


def test_documented_prod_tag_policy_rejects_mutable_tags() -> None:
    """Keep the Sugarkube promotion contract aligned with app_config tag policy."""
    for tag in MUTABLE_TAGS:
        assert not _is_immutable_release_tag(tag)

    assert _is_immutable_release_tag("main-deadbee")
    assert _is_immutable_release_tag("v0.1.0")


def test_runbooks_require_external_compute_e2ee_not_generic_health_only() -> None:
    """Staging/prod readiness must require a real compute node and E2EE flow."""
    for runbook in (STAGING_RUNBOOK, PROD_RUNBOOK, ONBOARDING_RUNBOOK, RELEASE_RUNBOOK):
        text = _squash(runbook.read_text(encoding="utf-8"))
        assert "necessary" in text and "insufficient" in text
        assert "real external" in text
        assert "encrypted API v1 relay/desktop-bridge E2EE request/response" in text
        assert "/relay/diagnostics" in text
        assert "relay logs" in text


def test_runbooks_preserve_single_pod_in_memory_relay_caveat_and_canonical_path() -> None:
    """Production docs must not imply HA, durable queue, or local manifest deployment."""
    for runbook in (STAGING_RUNBOOK, PROD_RUNBOOK, ONBOARDING_RUNBOOK, RELEASE_RUNBOOK):
        text = _squash(runbook.read_text(encoding="utf-8"))
        assert "one replica" in text
        assert "one Gunicorn worker" in text
        assert "in-memory" in text
        assert "future work" in text or "no HA/durable queue claim" in text
        assert "GHCR" in text
        assert "OCI Helm chart" in text
        assert "docker-compose.yml" in text
        assert "raw `k8s/` manifests" in text


def test_runbooks_capture_cloudflare_and_fail_closed_api_path_gates() -> None:
    """Cloudflare/WAF is external to Helm and plaintext API v1 relay paths fail closed."""
    onboarding_text = ONBOARDING_RUNBOOK.read_text(encoding="utf-8")
    release_text = RELEASE_RUNBOOK.read_text(encoding="utf-8")

    for text in (_squash(onboarding_text), _squash(release_text)):
        assert "Cloudflare" in text
        assert "external" in text
        assert "cf-ray" in text
        assert "Security Events" in text
        assert "Plaintext relay-dispatched API v1 paths are intentionally fail-closed" in text
