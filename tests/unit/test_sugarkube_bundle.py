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
