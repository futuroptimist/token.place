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


def test_bundle_env_targets_local_chart():
    """Ensure the sugarkube bundle points Helm at the in-repo chart."""
    env = _load_env(BUNDLE_ENV_PATH)

    assert env["RELEASE"] == "tokenplace-relay"
    assert env["CHART"].endswith("k8s/charts/tokenplace-relay")
    assert env["VALUES_FILE"].endswith("helm-values/token-place-values.yaml")
    assert env["NAMESPACE"] == "tokenplace"
    wait_targets = {target.strip() for target in env["WAIT_TARGETS"].split(",")}
    assert "deployment.apps/tokenplace-relay" in wait_targets


def test_bundle_values_pin_arm64_and_production_env():
    """Sugarkube values should keep the relay production-ready on Raspberry Pi."""
    values = yaml.safe_load(BUNDLE_VALUES_PATH.read_text())

    node_selector = values["nodeSelector"]
    assert node_selector["kubernetes.io/arch"] == "arm64"

    env_vars = values["env"]
    assert env_vars["TOKEN_PLACE_ENV"] == "production"

    relay = values["relay"]
    assert relay["host"] == "0.0.0.0"
    assert relay["port"] == 5010
