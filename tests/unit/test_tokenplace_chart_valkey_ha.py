from __future__ import annotations

from typing import Any

import pytest

from tests.unit.test_tokenplace_chart_metrics import (
    _env_by_name,
    _helm_template,
    _kind,
    _render,
)


VALKEY_BASE = (
    "--set", "stateBackend.type=valkey",
    "--set", "strategy.type=RollingUpdate",
    "--set", "stateBackend.valkey.environment=staging",
    "--set", "stateBackend.valkey.cluster=relay-a",
    "--set", "stateBackend.valkey.acknowledgementKey.existingSecret=relay-state",
    "--set", "sharedRateLimit.existingSecret=relay-rate-limit",
)


def _valkey_render(*args: str) -> list[dict[str, Any]]:
    return _render(*VALKEY_BASE, *args)


def test_memory_defaults_remain_single_process_and_do_not_render_valkey() -> None:
    docs = _render()
    deployment = _kind(docs, "Deployment")[0]
    env = _env_by_name(deployment)

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert env["RELAY_WORKERS"]["value"] == "1"
    assert env["TOKENPLACE_RELAY_STATE_BACKEND"]["value"] == "memory"
    assert not any(name.startswith("TOKENPLACE_RELAY_VALKEY_") for name in env)
    assert not any(doc.get("kind", "").lower().startswith(("redis", "valkey")) for doc in docs)


def test_direct_valkey_renders_runtime_contract_and_secret_refs() -> None:
    docs = _valkey_render(
        "--set", "replicaCount=3",
        "--set", "stateBackend.valkey.direct.host=valkey-primary.data.svc",
        "--set", "stateBackend.valkey.auth.existingSecret=relay-valkey-auth",
        "--set", "podDisruptionBudget.enabled=true",
        "--set", "podAntiAffinity.enabled=true",
        "--set", "topologySpreadConstraints[0].maxSkew=1",
        "--set", "topologySpreadConstraints[0].topologyKey=kubernetes.io/hostname",
        "--set", "topologySpreadConstraints[0].whenUnsatisfiable=DoNotSchedule",
    )
    deployment = _kind(docs, "Deployment")[0]
    env = _env_by_name(deployment)

    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1},
    }
    assert env["TOKENPLACE_RELAY_VALKEY_DISCOVERY"]["value"] == "direct"
    assert env["TOKENPLACE_RELAY_VALKEY_HOST"]["value"] == "valkey-primary.data.svc"
    assert env["TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64"]["valueFrom"]["secretKeyRef"] == {
        "name": "relay-state", "key": "acknowledgement-key-base64"
    }
    assert env["TOKENPLACE_RATE_LIMIT_STORAGE_URI"]["valueFrom"]["secretKeyRef"] == {
        "name": "relay-rate-limit", "key": "storage-uri"
    }
    assert env["TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES"]["value"] == "0"
    assert _kind(docs, "PodDisruptionBudget")[0]["spec"]["maxUnavailable"] == 1
    assert deployment["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert deployment["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
    assert "value" not in env["TOKENPLACE_RELAY_VALKEY_PASSWORD"]
    assert "optional" not in env["TOKENPLACE_RELAY_VALKEY_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert not any(doc.get("kind", "").lower().startswith(("redis", "valkey")) for doc in docs)


def test_sentinel_tls_renders_endpoints_and_mounted_secret_paths() -> None:
    docs = _valkey_render(
        "--set", "stateBackend.valkey.discovery=sentinel",
        "--set-json", 'stateBackend.valkey.sentinel.endpoints=[["sentinel-a",26379],["sentinel-b",26379],["sentinel-c",26379]]',
        "--set", "stateBackend.valkey.sentinel.service=relay-primary",
        "--set", "stateBackend.valkey.tls.enabled=true",
        "--set", "stateBackend.valkey.tls.existingSecret=relay-valkey-tls",
    )
    deployment = _kind(docs, "Deployment")[0]
    pod = deployment["spec"]["template"]["spec"]
    env = _env_by_name(deployment)
    assert env["TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON"]["value"] == (
        '[["sentinel-a",26379],["sentinel-b",26379],["sentinel-c",26379]]'
    )
    assert env["TOKENPLACE_RELAY_VALKEY_SENTINEL_SERVICE"]["value"] == "relay-primary"
    assert env["TOKENPLACE_RELAY_VALKEY_TLS"]["value"] == "true"
    assert env["TOKENPLACE_RELAY_VALKEY_TLS_CA_CERT"]["value"] == "/var/run/tokenplace-valkey-tls/ca.crt"
    assert "TOKENPLACE_RELAY_VALKEY_TLS_CLIENT_CERT" not in env
    assert "TOKENPLACE_RELAY_VALKEY_TLS_CLIENT_KEY" not in env
    assert {volume["name"]: volume for volume in pod["volumes"]}["valkey-tls"]["secret"] == {
        "secretName": "relay-valkey-tls"
    }


def test_custom_affinity_preserves_generated_pod_anti_affinity() -> None:
    docs = _valkey_render(
        "--set", "replicaCount=2",
        "--set", "stateBackend.valkey.direct.host=valkey",
        "--set", "podDisruptionBudget.enabled=true",
        "--set", "podAntiAffinity.enabled=true",
        "--set", "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/os",
        "--set", "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In",
        "--set", "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=linux",
    )
    affinity = _kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["affinity"]
    assert affinity["nodeAffinity"]
    assert affinity["podAntiAffinity"]


def test_mutual_tls_renders_client_certificate_paths() -> None:
    docs = _valkey_render(
        "--set", "stateBackend.valkey.direct.host=valkey",
        "--set", "stateBackend.valkey.tls.enabled=true",
        "--set", "stateBackend.valkey.tls.existingSecret=relay-valkey-tls",
        "--set", "stateBackend.valkey.tls.clientCertKey=tls.crt",
        "--set", "stateBackend.valkey.tls.clientKeyKey=tls.key",
    )
    env = _env_by_name(_kind(docs, "Deployment")[0])
    assert env["TOKENPLACE_RELAY_VALKEY_TLS_CLIENT_CERT"]["value"].endswith("/tls.crt")
    assert env["TOKENPLACE_RELAY_VALKEY_TLS_CLIENT_KEY"]["value"].endswith("/tls.key")


@pytest.mark.parametrize(
    "args, message",
    (
        (("--set", "stateBackend.type=unknown"), "/stateBackend/type"),
        (("--set", "replicaCount=2"), "stateBackend.type=memory requires"),
        (VALKEY_BASE, "direct.host is required"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "stateBackend.valkey.acknowledgementKey.existingSecret="), "acknowledgementKey.existingSecret is required"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "sharedRateLimit.existingSecret="), "sharedRateLimit.existingSecret is required"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "env.TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES=1"), "chart-managed"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "relay.workers=2", "--set", "env.RELAY_WORKERS=1"), "chart-managed"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "stateBackend.valkey.environment=Staging"), "/stateBackend/valkey/environment"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "stateBackend.valkey.timeouts.connectSeconds=31"), "/stateBackend/valkey/timeouts/connectSeconds"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "stateBackend.valkey.retryAttempts=6"), "/stateBackend/valkey/retryAttempts"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "stateBackend.valkey.tls.clientCertKey=tls.crt"), "clientCertKey and clientKeyKey"),
    ),
)
def test_invalid_deployment_contract_fails_render(args: tuple[str, ...], message: str) -> None:
    result = _helm_template(*args, check=False)
    assert result.returncode != 0
    assert message in result.stderr
