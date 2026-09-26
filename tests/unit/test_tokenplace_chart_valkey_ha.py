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
        capture_output=True,
        text=True,
    )


def _docs(*args: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_template(*args).stdout) if isinstance(doc, dict)]


def _kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _env(deployment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return {entry["name"]: entry for entry in entries}


DIRECT = (
    "--set", "stateBackend.type=valkey",
    "--set", "stateBackend.valkey.environment=staging",
    "--set", "stateBackend.valkey.cluster=relay-a",
    "--set", "stateBackend.valkey.direct.host=valkey.example",
    "--set", "stateBackend.valkey.auth.existingSecret=valkey-auth",
    "--set", "stateBackend.valkey.acknowledgementKey.existingSecret=valkey-ack",
)


def test_memory_default_remains_single_process_without_valkey_or_pdb() -> None:
    docs = _docs()
    deployment = _kind(docs, "Deployment")[0]
    env = _env(deployment)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert env["RELAY_WORKERS"]["value"] == "1"
    assert env["TOKENPLACE_RELAY_STATE_BACKEND"]["value"] == "memory"
    assert not any(name.startswith("TOKENPLACE_RELAY_VALKEY_") for name in env)
    assert _kind(docs, "PodDisruptionBudget") == []


def test_direct_valkey_renders_runtime_contract_using_secret_refs() -> None:
    docs = _docs(*DIRECT)
    deployment = _kind(docs, "Deployment")[0]
    env = _env(deployment)
    assert env["TOKENPLACE_RELAY_VALKEY_DISCOVERY"]["value"] == "direct"
    assert env["TOKENPLACE_RELAY_VALKEY_HOST"]["value"] == "valkey.example"
    assert env["TOKENPLACE_RELAY_VALKEY_PORT"]["value"] == "6379"
    assert env["TOKENPLACE_RELAY_VALKEY_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "valkey-auth"
    assert env["TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64"]["valueFrom"]["secretKeyRef"] == {
        "name": "valkey-ack", "key": "acknowledgement-key-base64"
    }
    assert "valkey-auth" not in _template(*DIRECT).stdout.replace("name: valkey-auth", "")


def test_sentinel_ha_owns_rollout_placement_pdb_and_drain_contract() -> None:
    args = (*DIRECT, "--set", "stateBackend.valkey.highAvailability=true", "--set", "replicaCount=3",
            "--set", "stateBackend.valkey.discovery=sentinel", "--set-json",
            'stateBackend.valkey.sentinel.endpoints=[["sentinel-a",26379],["sentinel-b",26379]]',
            "--set", "stateBackend.valkey.sentinel.service=mymaster", "--set",
            "stateBackend.valkey.sentinel.auth.existingSecret=sentinel-auth")
    docs = _docs(*args)
    deployment = _kind(docs, "Deployment")[0]
    pod_spec = deployment["spec"]["template"]["spec"]
    env = _env(deployment)
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}
    }
    assert pod_spec["topologySpreadConstraints"][0]["whenUnsatisfiable"] == "DoNotSchedule"
    assert pod_spec["affinity"]["podAntiAffinity"]
    assert pod_spec["terminationGracePeriodSeconds"] == 30
    assert pod_spec["containers"][0]["lifecycle"]["preStop"]
    assert env["TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON"]["value"] == '[["sentinel-a",26379],["sentinel-b",26379]]'
    assert env["TOKENPLACE_RELAY_VALKEY_SENTINEL_SERVICE"]["value"] == "mymaster"
    assert _kind(docs, "PodDisruptionBudget")[0]["spec"]["maxUnavailable"] == 1
    assert not any(doc.get("kind") in {"StatefulSet", "Redis", "Valkey"} for doc in docs)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--set", "stateBackend.type=other"), "/stateBackend/type"),
        (("--set", "replicaCount=2"), "memory state backend"),
        ((*DIRECT, "--set", "stateBackend.valkey.highAvailability=true", "--set", "replicaCount=2", "--set", "relay.legacyRoutesEnabled=true"), "legacy relay routes"),
        (("--set", "stateBackend.type=valkey"), "environment is required"),
        ((*DIRECT[:-2],), "acknowledgementKey.existingSecret is required"),
    ],
)
def test_invalid_backend_contracts_fail_closed(args: tuple[str, ...], message: str) -> None:
    result = _template(*args, check=False)
    assert result.returncode != 0
    assert message in result.stderr


def test_managed_state_env_cannot_be_overridden() -> None:
    docs = _docs(*DIRECT, "--set", "extraEnv[0].name=TOKENPLACE_RELAY_STATE_BACKEND", "--set", "extraEnv[0].value=memory")
    assert _env(_kind(docs, "Deployment")[0])["TOKENPLACE_RELAY_STATE_BACKEND"]["value"] == "valkey"
