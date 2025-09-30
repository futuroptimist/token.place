"""Tests for the Raspberry Pi k3s relay manifest."""
from pathlib import Path

import yaml


RASPI_MANIFEST_PATH = Path("k8s/relay-raspi-pod.yaml")


def test_raspi_manifest_binds_public_interface():
    """The Raspberry Pi manifest should expose relay.py on all interfaces for Service access."""
    manifest = yaml.safe_load(RASPI_MANIFEST_PATH.read_text())
    container = manifest["spec"]["containers"][0]

    args = container.get("args", [])
    assert "--host" in args, "relay-raspi-pod.yaml must pass --host to relay.py"
    host_value = args[args.index("--host") + 1]
    assert host_value == "0.0.0.0", "relay-raspi-pod.yaml should bind relay.py to 0.0.0.0"


def test_raspi_manifest_sets_production_env():
    """Ensure the manifest marks the pod as production for telemetry and config."""
    manifest = yaml.safe_load(RASPI_MANIFEST_PATH.read_text())
    container = manifest["spec"]["containers"][0]

    env = {entry["name"]: entry["value"] for entry in container.get("env", [])}
    assert env.get("TOKEN_PLACE_ENV") == "production", "Set TOKEN_PLACE_ENV=production for k3s pod"
