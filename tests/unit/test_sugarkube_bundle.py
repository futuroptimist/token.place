"""Tests for the sugarkube Helm bundle integration."""
from pathlib import Path

import yaml


BUNDLE_ENV_PATH = Path("k8s/sugarkube/token-place.env")
BUNDLE_VALUES_PATH = Path("k8s/sugarkube/token-place-values.yaml")


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Malformed env line: {raw}")
        key, value = line.split("=", 1)
        env[key] = value
    return env


def test_bundle_env_targets_canonical_oci_chart():
    """Ensure the legacy bundle points Helm at the canonical OCI chart."""
    env = _load_env(BUNDLE_ENV_PATH)

    assert env["RELEASE"] == "tokenplace"
    assert env["CHART"] == "oci://ghcr.io/futuroptimist/charts/tokenplace"
    assert env["VALUES_FILE"].endswith("helm-values/token-place-values.yaml")
    assert env["NAMESPACE"] == "tokenplace"
    wait_targets = {target.strip() for target in env["WAIT_TARGETS"].split(",")}
    assert "deployment.apps/tokenplace" in wait_targets


def test_bundle_values_use_canonical_image_and_tmp_xdg_defaults():
    """Compatibility values should mirror the canonical chart defaults."""
    values = yaml.safe_load(BUNDLE_VALUES_PATH.read_text())

    image = values["image"]
    assert image["repository"] == "ghcr.io/futuroptimist/tokenplace-relay"
    assert image["tag"] == "main-latest"
    assert image["pullPolicy"] == "Always"

    env_vars = values["env"]
    assert env_vars["XDG_CONFIG_HOME"] == "/tmp/.config"
    assert env_vars["XDG_DATA_HOME"] == "/tmp/.local/share"
    assert env_vars["XDG_CACHE_HOME"] == "/tmp/.cache"
    assert env_vars["XDG_STATE_HOME"] == "/tmp/.local/state"


def test_bundle_values_do_not_pin_redis_storage_backend():
    """Sugarkube values must not require Redis-specific limiter env defaults."""
    content = BUNDLE_VALUES_PATH.read_text()
    assert "redis://" not in content
    assert "TOKENPLACE_RATE_LIMIT_STORAGE_URI" not in content


def test_sugarkube_chart_pin_matches_current_canonical_chart():
    """Sugarkube version pin docs should match the canonical OCI chart package."""
    chart = yaml.safe_load(Path("charts/tokenplace/Chart.yaml").read_text())
    version_pin = Path("docs/apps/tokenplace.version").read_text().strip()

    assert chart["version"] == "0.1.1"
    assert chart["appVersion"] == "0.1.0"
    assert version_pin == chart["version"]

    onboarding = Path("docs/relay_sugarkube_onboarding.md").read_text()
    assert "Sugarkube must pin chart package `0.1.1`" in onboarding
    assert (
        "Do not treat stale\n  GHCR chart package `0.1.0` content "
        "as the current deployable chart"
        in onboarding
    )


def test_sugarkube_runbooks_require_real_e2ee_compute_signoff():
    """Runbooks must not equate generic HTTP health with production readiness."""
    for runbook in (
        Path("docs/k3s-sugarkube-staging.md"),
        Path("docs/k3s-sugarkube-prod.md"),
        Path("docs/relay_sugarkube_onboarding.md"),
    ):
        text = runbook.read_text()
        assert "necessary but insufficient" in text
        assert (
            "generic HTTP checks alone" in text
            or "do **not** prove production readiness" in text
        )
        assert "real external" in text
        assert "encrypted API v1 relay/desktop-bridge E2EE" in text
        assert "Plaintext relay-dispatched API v1" in text
        assert "intentionally fail-closed" in text
        assert "chart version/digest" in text
        assert "/relay/diagnostics" in text
        assert "relay logs" in text


def test_sugarkube_runbooks_document_cloudflare_as_external_gate():
    """Cloudflare route/TLS/WAF validation is outside Helm and must remain explicit."""
    onboarding = Path("docs/relay_sugarkube_onboarding.md").read_text()
    for expected in (
        (
            "Helm does not manage Cloudflare routes, DNS, TLS edge policy, "
            "Access policy, or\nWAF/skip rules"
        ),
        "dig +short staging.token.place",
        "dig +short token.place",
        "curl -fsS \"https://${host}/relay/diagnostics\"",
        "X-Relay-Server-Token: REPLACE_WITH_STAGING_TEST_TOKEN",
        "diagnostic-public-key-placeholder",
        "Cloudflare Security > Events",
        "cf-ray",
        "non-JSON `403`",
    ):
        assert expected in onboarding

    for runbook in (
        Path("docs/k3s-sugarkube-staging.md"),
        Path("docs/k3s-sugarkube-prod.md"),
    ):
        text = runbook.read_text()
        assert "Cloudflare/TLS/WAF routing is outside Helm" in text
        assert "check Cloudflare Security Events for that Ray ID" in text


def test_sugarkube_runbooks_preserve_single_pod_in_memory_scope():
    """Production docs must keep the relay-only, non-HA state caveat visible."""
    for runbook in (
        Path("docs/k3s-sugarkube-staging.md"),
        Path("docs/k3s-sugarkube-prod.md"),
        Path("docs/relay_sugarkube_onboarding.md"),
    ):
        text = runbook.read_text()
        assert "relay.py only" in text or "`relay.py` only" in text
        assert "server.py" in text
        assert "one pod" in text
        assert "one Gunicorn worker" in text
        assert "one replica" in text
        assert "in-memory" in text
        assert "State loss" in text or "accepted state loss" in text
        assert "future work" in text


def test_sugarkube_runbooks_keep_canonical_ghcr_oci_path_clear():
    """Sugarkube production docs should not promote Compose or local charts."""
    for runbook in (
        Path("docs/k3s-sugarkube-staging.md"),
        Path("docs/k3s-sugarkube-prod.md"),
        Path("docs/relay_sugarkube_onboarding.md"),
        Path("docs/ops/sugarkube-release.md"),
    ):
        text = runbook.read_text()
        assert "ghcr.io/futuroptimist/tokenplace-relay" in text
        assert "oci://ghcr.io/futuroptimist/charts/tokenplace" in text

    release = Path("docs/ops/sugarkube-release.md").read_text()
    assert "Use GitHub Actions and GHCR for deployable Sugarkube artifacts" in release
    assert "not the staging or\nproduction release path" in release
