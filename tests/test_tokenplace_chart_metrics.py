"""Regression tests for the canonical token.place Helm metrics contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "tokenplace"


def _helm_template(*args: str) -> list[dict]:
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    result = subprocess.run(
        [helm, "template", "tokenplace", str(CHART), "--namespace", "tokenplace", *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


def test_metrics_and_servicemonitor_are_disabled_by_default() -> None:
    docs = _helm_template()
    assert not _by_kind(docs, "ServiceMonitor")
    deploy = _by_kind(docs, "Deployment")[0]
    env_names = {item["name"] for item in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "TOKENPLACE_METRICS_TOKEN" not in env_names


def test_authenticated_servicemonitor_contract_and_relay_constraints() -> None:
    docs = _helm_template(
        "--set",
        "ingress.enabled=true",
        "--set",
        "ingress.host=staging.token.place",
        "--set",
        "metrics.enabled=true",
        "--set",
        "metrics.auth.existingSecret=tokenplace-metrics",
        "--set",
        "serviceMonitor.enabled=true",
        "--set",
        "serviceMonitor.targetLabels.environment=staging",
        "--set",
        "serviceMonitor.targetLabels.cluster=sugarkube-staging",
    )

    service = _by_kind(docs, "Service")[0]
    deployment = _by_kind(docs, "Deployment")[0]
    ingress = _by_kind(docs, "Ingress")[0]
    service_monitor = _by_kind(docs, "ServiceMonitor")[0]

    assert service_monitor["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    assert service_monitor["metadata"]["namespace"] == "monitoring"
    assert service_monitor["spec"]["namespaceSelector"]["matchNames"] == ["tokenplace"]
    assert service_monitor["spec"]["selector"]["matchLabels"] == service["spec"]["selector"]

    endpoint = service_monitor["spec"]["endpoints"][0]
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"
    assert endpoint["authorization"] == {
        "type": "Bearer",
        "credentials": {"name": "tokenplace-metrics", "key": "token"},
    }
    assert endpoint["scrapeTimeout"] == "10s"
    assert endpoint["interval"] == "30s"
    assert {item["targetLabel"]: item["replacement"] for item in endpoint["relabelings"]} == {
        "app": "tokenplace",
        "environment": "staging",
        "release": "tokenplace",
        "cluster": "sugarkube-staging",
    }

    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["TOKENPLACE_METRICS_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "tokenplace-metrics",
        "key": "token",
    }
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert env["RELAY_WORKERS"]["value"] == "1"

    ingress_paths = [
        path["path"]
        for rule in ingress["spec"]["rules"]
        for path in rule["http"]["paths"]
    ]
    assert "/metrics" not in ingress_paths
