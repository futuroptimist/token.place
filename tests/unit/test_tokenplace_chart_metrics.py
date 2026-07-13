from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path("charts/tokenplace")


def _helm_or_skip() -> str:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    return helm


def _helm_template(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    helm = _helm_or_skip()
    return subprocess.run(
        [helm, "template", "tokenplace", str(CHART), "--namespace", "tokenplace", *args],
        check=check,
        text=True,
        capture_output=True,
    )


def _render(*args: str) -> list[dict[str, Any]]:
    rendered = _helm_template(*args).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _deployment_env(deployment: dict[str, Any]) -> list[dict[str, Any]]:
    return deployment["spec"]["template"]["spec"]["containers"][0]["env"]


def _env_by_name(deployment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in _deployment_env(deployment)}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping_no_duplicates(loader: yaml.Loader, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:  # type: ignore[attr-defined]
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates)


def test_defaults_render_no_service_monitor_and_no_metrics_token() -> None:
    docs = _render()
    assert _kind(docs, "ServiceMonitor") == []
    deployment = _kind(docs, "Deployment")[0]
    env = _env_by_name(deployment)
    assert "TOKENPLACE_METRICS_TOKEN" not in env
    assert env["TOKENPLACE_METRICS_DISABLED"]["value"] == "1"


def test_service_monitor_requires_metrics_enabled() -> None:
    result = _helm_template(
        "--set", "serviceMonitor.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        check=False,
    )
    assert result.returncode != 0
    assert "metrics.enabled=true is required when serviceMonitor.enabled=true" in result.stderr


def test_metrics_path_override_fails_schema_validation() -> None:
    result = _helm_template("--set", "metrics.path=/custom", check=False)
    assert result.returncode != 0
    assert any(path in result.stderr for path in ("metrics.path", "/metrics/path"))
    assert "/metrics" in result.stderr


def test_enabled_monitor_matches_service_and_uses_secret_authorization() -> None:
    docs = _render(
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "serviceMonitor.enabled=true",
        "--set", "serviceMonitor.relabelings.environment=staging",
        "--set", "serviceMonitor.relabelings.release=main-deadbee",
        "--set", "serviceMonitor.relabelings.cluster=sugarkube-staging",
    )
    service = _kind(docs, "Service")[0]
    monitor = _kind(docs, "ServiceMonitor")[0]
    endpoint = monitor["spec"]["endpoints"][0]

    assert monitor["spec"]["selector"]["matchLabels"] == service["spec"]["selector"]
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"
    assert endpoint["authorization"]["credentials"] == {"name": "tokenplace-metrics", "key": "token"}

    relabelings = endpoint["relabelings"]
    assert {item["targetLabel"] for item in relabelings} == {"app", "environment", "release", "cluster"}
    replacements = {item["targetLabel"]: item["replacement"] for item in relabelings}
    assert replacements == {
        "app": "tokenplace",
        "environment": "staging",
        "release": "main-deadbee",
        "cluster": "sugarkube-staging",
    }


def test_extra_env_cannot_replace_chart_managed_metrics_secret_ref() -> None:
    docs = _render(
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "extraEnv[0].name=TOKENPLACE_METRICS_TOKEN",
        "--set", "extraEnv[0].value=plaintext",
    )
    env = _env_by_name(_kind(docs, "Deployment")[0])
    token_env = env["TOKENPLACE_METRICS_TOKEN"]
    assert "TOKENPLACE_METRICS_DISABLED" not in env
    assert "value" not in token_env
    assert token_env["valueFrom"]["secretKeyRef"] == {"name": "tokenplace-metrics", "key": "token"}


def test_no_public_metrics_ingress_path_is_added() -> None:
    docs = _render("--set", "ingress.enabled=true", "--set", "ingress.host=token.place")
    ingress = _kind(docs, "Ingress")[0]
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert [path["path"] for path in paths] == ["/"]
    assert "/metrics" not in {path["path"] for path in paths}


def test_single_replica_recreate_one_worker_constraints_preserved() -> None:
    docs = _render()
    deployment = _kind(docs, "Deployment")[0]
    env = _env_by_name(deployment)

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert env["RELAY_WORKERS"]["value"] == "1"


def test_service_monitor_accepts_overlapping_additional_labels_without_duplicate_keys() -> None:
    rendered = _helm_template(
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "serviceMonitor.enabled=true",
        "--set", "serviceMonitor.additionalLabels.app\\.kubernetes\\.io/name=overlap",
        "--set", "serviceMonitor.additionalLabels.release=kube-prometheus-stack",
    ).stdout
    docs = [doc for doc in yaml.load_all(rendered, Loader=UniqueKeyLoader) if isinstance(doc, dict)]
    monitor = _kind(docs, "ServiceMonitor")[0]
    labels = monitor["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "tokenplace"
    assert labels["release"] == "kube-prometheus-stack"
