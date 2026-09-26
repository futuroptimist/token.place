from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path("charts/tokenplace")


def _template(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    return subprocess.run(
        [helm, "template", "tokenplace", str(CHART), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def _docs(*args: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_template(*args).stdout) if isinstance(doc, dict)]


def _deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(doc for doc in docs if doc.get("kind") == "Deployment")


def _env(deployment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return {entry["name"]: entry for entry in entries}


DIRECT = (
    "--set", "stateBackend.type=valkey",
    "--set", "stateBackend.valkey.environment=staging",
    "--set", "stateBackend.valkey.cluster=relay-a",
    "--set", "stateBackend.valkey.direct.host=valkey.example.internal",
    "--set", "stateBackend.valkey.acknowledgementKey.existingSecret=relay-state",
)


def test_memory_default_remains_single_process_and_creates_no_valkey() -> None:
    docs = _docs()
    deployment = _deployment(docs)
    env = _env(deployment)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert env["RELAY_WORKERS"]["value"] == "1"
    assert env["TOKENPLACE_RELAY_STATE_BACKEND"]["value"] == "memory"
    assert not any("valkey" in str(doc.get("kind", "")).lower() for doc in docs)


def test_direct_valkey_uses_exact_runtime_names_and_secret_refs() -> None:
    deployment = _deployment(_docs(*DIRECT, "--set", "stateBackend.valkey.auth.existingSecret=valkey-auth"))
    env = _env(deployment)
    assert env["TOKENPLACE_RELAY_VALKEY_DISCOVERY"]["value"] == "direct"
    assert env["TOKENPLACE_RELAY_VALKEY_HOST"]["value"] == "valkey.example.internal"
    assert env["TOKENPLACE_RELAY_VALKEY_PORT"]["value"] == "6379"
    assert env["TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64"]["valueFrom"]["secretKeyRef"] == {
        "name": "relay-state", "key": "acknowledgement-key-base64"
    }
    assert env["TOKENPLACE_RELAY_VALKEY_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "valkey-auth"


def test_sentinel_ha_renders_rollout_distribution_pdb_and_tls_secret() -> None:
    args = (
        "--set", "stateBackend.type=valkey", "--set", "stateBackend.valkey.discovery=sentinel",
        "--set", "stateBackend.valkey.environment=staging", "--set", "stateBackend.valkey.cluster=relay-a",
        "--set", "stateBackend.valkey.sentinel.service=relay-primary",
        "--set", "stateBackend.valkey.sentinel.endpoints[0].host=sentinel-a.internal",
        "--set", "stateBackend.valkey.sentinel.endpoints[0].port=26379",
        "--set", "stateBackend.valkey.acknowledgementKey.existingSecret=relay-state",
        "--set", "stateBackend.valkey.tls.enabled=true", "--set", "stateBackend.valkey.tls.existingSecret=valkey-tls",
        "--set", "replicaCount=3", "--set", "strategy.type=RollingUpdate",
        "--set", "podDisruptionBudget.enabled=true",
        "--set", "topologySpreadConstraints[0].maxSkew=1",
        "--set", "topologySpreadConstraints[0].topologyKey=kubernetes.io/hostname",
        "--set", "topologySpreadConstraints[0].whenUnsatisfiable=DoNotSchedule",
    )
    docs = _docs(*args)
    deployment = _deployment(docs)
    env = _env(deployment)
    assert env["TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON"]["value"] == '[["sentinel-a.internal",26379]]'
    assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["topologySpreadConstraints"]
    assert pod_spec["affinity"]["podAntiAffinity"]
    assert next(doc for doc in docs if doc.get("kind") == "PodDisruptionBudget")["spec"]["maxUnavailable"] == 1
    assert {volume["secret"]["secretName"] for volume in pod_spec["volumes"] if "secret" in volume} == {"valkey-tls"}
    assert "value" not in env["TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--set", "stateBackend.type=other"), "stateBackend/type"),
        (("--set", "replicaCount=2"), "stateBackend.type=memory requires"),
        (DIRECT[:-2], "acknowledgementKey.existingSecret is required"),
        (DIRECT + ("--set", "replicaCount=2"), "strategy.type=RollingUpdate"),
        (DIRECT + ("--set", "replicaCount=2", "--set", "strategy.type=RollingUpdate", "--set", "relay.legacyRoutesEnabled=true"), "legacy relay routes are forbidden"),
        (DIRECT + ("--set", "stateBackend.valkey.tls.enabled=true"), "tls.existingSecret is required"),
    ],
)
def test_invalid_backend_combinations_fail_closed(args: tuple[str, ...], message: str) -> None:
    result = _template(*args, check=False)
    assert result.returncode != 0
    assert message in result.stderr


def test_chart_managed_state_env_cannot_be_overridden_with_plaintext() -> None:
    deployment = _deployment(_docs(*DIRECT, "--set", "extraEnv[0].name=TOKENPLACE_RELAY_VALKEY_PASSWORD", "--set", "extraEnv[0].value=plaintext"))
    assert "TOKENPLACE_RELAY_VALKEY_PASSWORD" not in _env(deployment)
    assert "plaintext" not in yaml.safe_dump(deployment)
