from pathlib import Path

import yaml


RUNBOOKS = [
    Path("docs/ops/sugarkube-release.md"),
    Path("docs/relay_sugarkube_onboarding.md"),
    Path("docs/k3s-sugarkube-staging.md"),
    Path("docs/k3s-sugarkube-prod.md"),
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_tokenplace_chart_pin_documents_current_oci_version() -> None:
    version_file = _text(Path("docs/apps/tokenplace.version")).strip()
    chart = yaml.safe_load(Path("charts/tokenplace/Chart.yaml").read_text(encoding="utf-8"))

    assert version_file == "0.1.1"
    assert chart["version"] == version_file

    for path in RUNBOOKS:
        text = _text(path)
        assert "Current deployable chart package version" in text or "current deployable chart package version" in text
        assert "0.1.1" in text


def test_sugarkube_runbooks_require_immutable_prod_tags() -> None:
    release = _text(Path("docs/ops/sugarkube-release.md"))

    assert "tokenplace.prod.tag" in release
    assert "explicit immutable" in release
    for mutable_tag in ["latest", "main-latest", "staging", "prod", "production"]:
        assert mutable_tag in release
    for immutable_tag in ["main-<shortsha>", "vX.Y.Z"]:
        assert immutable_tag in release


def test_sugarkube_runbooks_distinguish_http_health_from_e2ee_signoff() -> None:
    for path in RUNBOOKS:
        text = _text(path)
        assert "necessary" in text
        assert "insufficient" in text or "not sufficient" in text
        assert "/livez" in text
        assert "/healthz" in text
        assert "/relay/diagnostics" in text
        assert "real external" in text
        assert "encrypted" in text
        assert "E2EE" in text
        assert "relay/desktop-bridge" in text


def test_environment_runbooks_capture_cloudflare_external_gate_and_evidence() -> None:
    for path in [Path("docs/k3s-sugarkube-staging.md"), Path("docs/k3s-sugarkube-prod.md")]:
        text = _text(path)
        assert "Helm cannot prove public routing" in text
        assert "Cloudflare Security Events" in text
        assert "CF_RAY=REPLACE_CF_RAY" in text
        assert "REPLACE_WITH_ENVIRONMENT_TEST_TOKEN" in text
        assert "redacted-placeholder-public-key" in text
        assert "immutable image tag" in text
        assert "chart version" in text
        assert "live Deployment YAML" in text
        assert "relay logs captured after" in text


def test_sugarkube_runbooks_preserve_single_pod_in_memory_caveat() -> None:
    for path in [Path("docs/relay_sugarkube_onboarding.md"), Path("docs/k3s-sugarkube-staging.md"), Path("docs/k3s-sugarkube-prod.md")]:
        text = _text(path)
        assert "relay.py only" in text or "`relay.py` only" in text
        assert "server.py" in text
        assert "one pod" in text
        assert "one Gunicorn worker" in text
        assert "one replica" in text
        assert "in-memory" in text
        assert "State loss" in text or "state loss" in text
        assert "future work" in text
