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


@pytest.mark.parametrize(
    "args",
    (
        ("--set", "env.RELAY_WORKERS=2"),
        ("--set", "env.RELAY_WORKERS.name=SAFE", "--set", "env.RELAY_WORKERS.value=2"),
        ("--set", "extraEnv[0].name=RELAY_WORKERS", "--set-string", "extraEnv[0].value=2"),
    ),
)
def test_memory_rejects_relay_worker_environment_overrides(args: tuple[str, ...]) -> None:
    result = _helm_template(*args, check=False)
    assert result.returncode != 0
    assert "environment override RELAY_WORKERS is chart-managed" in result.stderr


def test_direct_valkey_renders_runtime_contract_and_secret_refs() -> None:
    docs = _valkey_render(
        "--set", "replicaCount=3",
        "--set", "stateBackend.valkey.direct.host=valkey-primary.data.svc",
        "--set", "stateBackend.valkey.auth.existingSecret=relay-valkey-auth",
        "--set", "podDisruptionBudget.enabled=true",
        "--set", "podAntiAffinity.enabled=true",
        "--set", "podAntiAffinity.type=required",
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


@pytest.mark.parametrize(
    "primary_secret, sentinel_secret, expected, absent",
    (
        (
            "primary-auth",
            "",
            {"USERNAME": ("primary-auth", "username"), "PASSWORD": ("primary-auth", "password")},
            ("SENTINEL_USERNAME", "SENTINEL_PASSWORD"),
        ),
        (
            "",
            "sentinel-auth",
            {
                "SENTINEL_USERNAME": ("sentinel-auth", "sentinel-username"),
                "SENTINEL_PASSWORD": ("sentinel-auth", "sentinel-password"),
            },
            ("USERNAME", "PASSWORD"),
        ),
        (
            "primary-auth",
            "sentinel-auth",
            {
                "USERNAME": ("primary-auth", "username"),
                "PASSWORD": ("primary-auth", "password"),
                "SENTINEL_USERNAME": ("sentinel-auth", "sentinel-username"),
                "SENTINEL_PASSWORD": ("sentinel-auth", "sentinel-password"),
            },
            (),
        ),
    ),
)
def test_sentinel_credentials_use_independent_secrets(
    primary_secret: str,
    sentinel_secret: str,
    expected: dict[str, tuple[str, str]],
    absent: tuple[str, ...],
) -> None:
    docs = _valkey_render(
        "--set", "stateBackend.valkey.discovery=sentinel",
        "--set-json", 'stateBackend.valkey.sentinel.endpoints=[["sentinel-a",26379]]',
        "--set", "stateBackend.valkey.sentinel.service=relay-primary",
        "--set", f"stateBackend.valkey.auth.existingSecret={primary_secret}",
        "--set", f"stateBackend.valkey.auth.sentinelExistingSecret={sentinel_secret}",
    )
    env = _env_by_name(_kind(docs, "Deployment")[0])

    for suffix, (secret_name, key) in expected.items():
        entry = env[f"TOKENPLACE_RELAY_VALKEY_{suffix}"]
        assert "value" not in entry
        assert entry["valueFrom"]["secretKeyRef"] == {"name": secret_name, "key": key}
    for suffix in absent:
        assert f"TOKENPLACE_RELAY_VALKEY_{suffix}" not in env


def test_sentinel_endpoint_host_accepts_runtime_maximum_length() -> None:
    host = "a" * 253
    docs = _valkey_render(
        "--set", "stateBackend.valkey.discovery=sentinel",
        "--set-json", f'stateBackend.valkey.sentinel.endpoints=[["{host}",26379]]',
        "--set", "stateBackend.valkey.sentinel.service=relay-primary",
    )
    env = _env_by_name(_kind(docs, "Deployment")[0])
    assert env["TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON"]["value"] == (
        f'[["{host}",26379]]'
    )


def test_sentinel_endpoint_host_rejects_over_runtime_maximum_length() -> None:
    host = "a" * 254
    result = _helm_template(
        *VALKEY_BASE,
        "--set", "stateBackend.valkey.discovery=sentinel",
        "--set-json", f'stateBackend.valkey.sentinel.endpoints=[["{host}",26379]]',
        "--set", "stateBackend.valkey.sentinel.service=relay-primary",
        check=False,
    )
    assert result.returncode != 0
    assert "/stateBackend/valkey/sentinel/endpoints/0/0" in result.stderr


def test_custom_affinity_preserves_generated_pod_anti_affinity() -> None:
    docs = _valkey_render(
        "--set", "replicaCount=2",
        "--set", "stateBackend.valkey.direct.host=valkey",
        "--set", "podDisruptionBudget.enabled=true",
        "--set", "podAntiAffinity.enabled=true",
        "--set", "podAntiAffinity.type=required",
        "--set", "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/os",
        "--set", "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In",
        "--set", "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=linux",
    )
    affinity = _kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["affinity"]
    assert affinity["nodeAffinity"]
    assert affinity["podAntiAffinity"]


@pytest.mark.parametrize(
    "placement",
    (
        ("--set", "podAntiAffinity.enabled=true"),
        (
            "--set", "topologySpreadConstraints[0].maxSkew=1",
            "--set", "topologySpreadConstraints[0].topologyKey=kubernetes.io/hostname",
            "--set", "topologySpreadConstraints[0].whenUnsatisfiable=ScheduleAnyway",
        ),
    ),
)
def test_multiple_replicas_reject_soft_placement(placement: tuple[str, ...]) -> None:
    result = _helm_template(
        *VALKEY_BASE,
        "--set", "stateBackend.valkey.direct.host=valkey",
        "--set", "replicaCount=2",
        "--set", "podDisruptionBudget.enabled=true",
        *placement,
        check=False,
    )
    assert result.returncode != 0
    assert "required podAntiAffinity on kubernetes.io/hostname" in result.stderr


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


def test_valkey_namespace_accepts_runtime_maximum_length() -> None:
    coordinate = "a" * 128
    docs = _valkey_render(
        "--set", f"stateBackend.valkey.environment={coordinate}",
        "--set", f"stateBackend.valkey.cluster={coordinate}",
        "--set", "stateBackend.valkey.direct.host=valkey.internal",
    )
    env = _env_by_name(_kind(docs, "Deployment")[0])
    assert env["TOKENPLACE_RELAY_VALKEY_ENVIRONMENT"]["value"] == coordinate
    assert env["TOKENPLACE_RELAY_VALKEY_CLUSTER"]["value"] == coordinate


@pytest.mark.parametrize(
    "args, path",
    (
        (
            (
                "--set",
                f"stateBackend.valkey.environment={'a' * 129}",
                "--set",
                "stateBackend.valkey.direct.host=valkey",
            ),
            "/stateBackend/valkey/environment",
        ),
        (
            ("--set", "stateBackend.valkey.direct.host=valkey primary"),
            "/stateBackend/valkey/direct/host",
        ),
        (
            ("--set", "stateBackend.valkey.direct.host=valkey/internal"),
            "/stateBackend/valkey/direct/host",
        ),
        (
            ("--set", "stateBackend.valkey.direct.host=user@valkey"),
            "/stateBackend/valkey/direct/host",
        ),
        (
            (
                "--set",
                "stateBackend.valkey.discovery=sentinel",
                "--set-json",
                'stateBackend.valkey.sentinel.endpoints=[["sentinel/a",26379]]',
                "--set",
                "stateBackend.valkey.sentinel.service=relay-primary",
            ),
            "/stateBackend/valkey/sentinel/endpoints/0/0",
        ),
        (
            (
                "--set",
                "stateBackend.valkey.discovery=sentinel",
                "--set-json",
                'stateBackend.valkey.sentinel.endpoints=[["sentinel-a",26379]]',
                "--set",
                "stateBackend.valkey.sentinel.service=Relay Primary",
            ),
            "/stateBackend/valkey/sentinel/service",
        ),
    ),
)
def test_valkey_runtime_address_rules_fail_schema(
    args: tuple[str, ...], path: str
) -> None:
    result = _helm_template(*VALKEY_BASE, *args, check=False)
    assert result.returncode != 0
    assert path in result.stderr


@pytest.mark.parametrize(
    "args, message",
    (
        (("--set", "stateBackend.type=unknown"), "/stateBackend/type"),
        (("--set", "replicaCount=2"), "stateBackend.type=memory requires"),
        (VALKEY_BASE, "direct.host is required"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "stateBackend.valkey.acknowledgementKey.existingSecret="), "acknowledgementKey.existingSecret is required"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "sharedRateLimit.existingSecret="), "sharedRateLimit.existingSecret is required"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "env.TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES=1"), "chart-managed"),
        (VALKEY_BASE + ("--set", "stateBackend.valkey.direct.host=valkey", "--set", "env.TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES.name=SAFE", "--set-string", "env.TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES.value=1"), "chart-managed"),
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
