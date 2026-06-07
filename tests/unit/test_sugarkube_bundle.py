"""Tests for the sugarkube Helm bundle integration."""
from pathlib import Path

import yaml


BUNDLE_ENV_PATH = Path("k8s/sugarkube/token-place.env")
BUNDLE_VALUES_PATH = Path("k8s/sugarkube/token-place-values.yaml")
SUGARKUBE_RUNBOOKS = (
    Path("docs/k3s-sugarkube-staging.md"),
    Path("docs/k3s-sugarkube-prod.md"),
    Path("docs/relay_sugarkube_onboarding.md"),
    Path("docs/ops/sugarkube-release.md"),
)
OPERATOR_ENVIRONMENT_RUNBOOKS = (
    Path("docs/k3s-sugarkube-staging.md"),
    Path("docs/k3s-sugarkube-prod.md"),
)


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


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _assert_contains_all(path: Path, phrases: tuple[str, ...]) -> None:
    content = _normalize_whitespace(path.read_text())
    missing = [
        phrase
        for phrase in phrases
        if _normalize_whitespace(phrase) not in content
    ]
    assert not missing, f"{path} missing required phrases: {missing}"


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
    chart_version = chart["version"]
    app_version = Path("docs/apps/tokenplace.version").read_text().strip()
    release_doc = Path("docs/ops/sugarkube-release.md").read_text()

    assert app_version == chart_version
    assert f"current token.place chart source is `{chart_version}`" in release_doc
    assert "Sugarkube `docs/apps/tokenplace.version`" in release_doc
    assert "should pin" in release_doc


def test_sugarkube_environment_runbooks_require_external_e2ee_proof():
    """Each environment runbook must keep real E2EE sign-off gates explicit."""
    shared_required_phrases = (
        "necessary but insufficient",
        "encrypted API v1 relay/desktop-bridge E2EE request/response",
        "Plaintext relay-dispatched API v1 paths are intentionally fail-closed",
        "immutable image tag, chart version and digest where available",
        "rendered or live deployment YAML",
        "relay logs after the compute test",
    )

    for path in OPERATOR_ENVIRONMENT_RUNBOOKS:
        _assert_contains_all(path, shared_required_phrases)

    _assert_contains_all(
        Path("docs/k3s-sugarkube-staging.md"),
        ("real external desktop/compute node registers",),
    )
    _assert_contains_all(
        Path("docs/k3s-sugarkube-prod.md"),
        ("real production desktop/compute node registers", "instead of reusing staging evidence"),
    )


def test_sugarkube_onboarding_and_release_docs_require_external_e2ee_proof():
    """Cross-environment docs must preserve staging and production proof details."""
    release_phrases = (
        "necessary but insufficient",
        "real external desktop/compute node must register to staging",
        "repeat the real external proof against production",
        "encrypted API v1\nrelay/desktop-bridge E2EE request/response",
        "Plaintext relay-dispatched API v1 paths are intentionally fail-closed",
        "immutable image tag,\nchart version and digest where available",
        "rendered or live deployment YAML",
        "relay logs after the\ncompute test",
    )
    onboarding_phrases = (
        "necessary but insufficient",
        "real external relay-compute proof",
        "real external desktop/compute node registers",
        "encrypted API v1 relay/desktop-bridge E2EE request/response",
        "paths are intentionally fail-closed",
        "immutable image tag, chart version and digest where available",
        "rendered\n  or live deployment YAML",
        "relay logs after",
    )

    _assert_contains_all(Path("docs/ops/sugarkube-release.md"), release_phrases)
    _assert_contains_all(Path("docs/relay_sugarkube_onboarding.md"), onboarding_phrases)


def test_sugarkube_docs_keep_cloudflare_gate_explicit_per_runbook():
    """Every operator runbook should keep Cloudflare/TLS/WAF caveats visible."""
    for path in SUGARKUBE_RUNBOOKS:
        _assert_contains_all(
            path,
            (
                "Cloudflare",
                "WAF",
                "external release gate",
                "before",
                "reaches `relay.py`",
            ),
        )

    _assert_contains_all(
        Path("docs/relay_sugarkube_onboarding.md"),
        ("cf-ray", "intentionally invalid", "unregister the unique diagnostic key"),
    )
    _assert_contains_all(Path("docs/ops/sugarkube-release.md"), ("cf-ray",))


def test_sugarkube_docs_keep_stateful_relay_caveats_explicit_per_runbook():
    """Every operator runbook should keep non-HA relay limitations visible."""
    for path in SUGARKUBE_RUNBOOKS:
        _assert_contains_all(path, ("single-pod", "in-memory", "future work"))

    for path in OPERATOR_ENVIRONMENT_RUNBOOKS:
        _assert_contains_all(path, ("one pod", "one Gunicorn worker", "one replica", "State loss"))

    _assert_contains_all(
        Path("docs/ops/sugarkube-release.md"),
        ("one-worker", "state loss", "HA/durable queues are future work"),
    )
    _assert_contains_all(
        Path("docs/relay_sugarkube_onboarding.md"),
        ("state loss", "one Gunicorn worker", "Multi-replica"),
    )


def test_sugarkube_release_docs_reject_mutable_prod_tags_and_legacy_paths():
    """Production docs must require immutable tags and the GHCR + OCI path."""
    release_doc = Path("docs/ops/sugarkube-release.md").read_text()

    for mutable_tag in ("main-latest", "latest", "staging", "prod", "production"):
        assert mutable_tag in release_doc
    assert "docs/apps/tokenplace.prod.tag`\nempty" in release_doc
    assert "production promotion must supply an explicit immutable tag" in release_doc
    assert "root `docker-compose.yml`, and raw `k8s/` manifests" in release_doc
    assert "GHCR relay image, and the OCI Helm chart" in release_doc
