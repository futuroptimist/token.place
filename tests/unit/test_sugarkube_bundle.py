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


def test_sugarkube_chart_version_docs_track_canonical_chart_source():
    """Runbook chart pins should track the canonical chart package version."""
    chart = yaml.safe_load(Path("charts/tokenplace/Chart.yaml").read_text())
    app_version = Path("docs/apps/tokenplace.version").read_text().strip()
    release_doc = Path("docs/ops/sugarkube-release.md").read_text()

    assert chart["version"] == "0.1.1"
    assert app_version == chart["version"]
    assert "docs/apps/tokenplace.version`\nshould pin `0.1.1`" in release_doc


def test_sugarkube_runbooks_require_real_external_e2ee_proof():
    """Generic HTTP checks must not be documented as sufficient for sign-off."""
    docs = "\n".join(
        Path(path).read_text()
        for path in (
            "docs/k3s-sugarkube-staging.md",
            "docs/k3s-sugarkube-prod.md",
            "docs/relay_sugarkube_onboarding.md",
            "docs/ops/sugarkube-release.md",
        )
    )

    required_phrases = [
        "necessary but insufficient",
        "real external desktop/compute node registers",
        "encrypted API v1 relay/desktop-bridge E2EE request/response",
        "Plaintext relay-dispatched API v1 paths are intentionally fail-closed",
        "immutable image tag, chart version and digest where available",
        "rendered or live deployment YAML",
        "relay logs after the compute test",
    ]
    for phrase in required_phrases:
        assert phrase in docs


def test_sugarkube_docs_keep_cloudflare_and_stateful_caveats_explicit():
    """Cloudflare gates and non-HA relay limitations should stay visible."""
    docs = "\n".join(
        Path(path).read_text()
        for path in (
            "docs/k3s-sugarkube-staging.md",
            "docs/k3s-sugarkube-prod.md",
            "docs/relay_sugarkube_onboarding.md",
            "docs/ops/sugarkube-release.md",
        )
    )

    for phrase in (
        "Helm does **not** create/manage Cloudflare routes, DNS, WAF, or Access policies",
        "cf-ray",
        "before it reaches `relay.py`",
        "single-pod",
        "one-worker",
        "in-memory",
        "state loss on",
        "HA/durable queues are future work",
    ):
        assert phrase in docs


def test_sugarkube_release_docs_reject_mutable_prod_tags_and_legacy_paths():
    """Production docs must require immutable tags and the GHCR + OCI path."""
    release_doc = Path("docs/ops/sugarkube-release.md").read_text()

    for mutable_tag in ("main-latest", "latest", "staging", "prod", "production"):
        assert mutable_tag in release_doc
    assert "docs/apps/tokenplace.prod.tag`\nempty" in release_doc
    assert "production promotion must supply an explicit immutable tag" in release_doc
    assert "root `docker-compose.yml`, and raw `k8s/` manifests" in release_doc
    assert "GHCR relay image, and the OCI Helm chart" in release_doc
