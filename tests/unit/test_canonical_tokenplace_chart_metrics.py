"""Guardrails for the canonical Sugarkube token.place metrics scrape contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_ROOT = Path("charts/tokenplace")


def _render(*args: str) -> list[dict[str, Any]]:
    helm = subprocess.run(
        ["helm", "template", "tokenplace", str(CHART_ROOT), "--namespace", "tokenplace", *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return [doc for doc in yaml.safe_load_all(helm.stdout) if doc]


def _kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind]


def test_metrics_and_service_monitor_defaults_are_disabled() -> None:
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))
    schema = json.loads((CHART_ROOT / "values.schema.json").read_text(encoding="utf-8"))
    docs = _render()

    assert values["metrics"]["enabled"] is False
    assert values["metrics"]["path"] == "/metrics"
    assert values["metrics"]["auth"]["existingSecret"] == ""
    assert values["metrics"]["auth"]["secretKey"] == "token"
    assert values["serviceMonitor"]["enabled"] is False
    assert values["serviceMonitor"]["interval"] == "30s"
    assert values["serviceMonitor"]["scrapeTimeout"] == "10s"
    assert values["serviceMonitor"]["additionalLabels"]["release"] == "kube-prometheus-stack"
    assert schema["properties"]["replicaCount"]["const"] == 1
    assert not _kind(docs, "ServiceMonitor")
    deployment = _kind(docs, "Deployment")[0]
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert "TOKENPLACE_METRICS_TOKEN" not in {item["name"] for item in env}


def test_enabled_service_monitor_selects_canonical_service_with_bearer_secret() -> None:
    docs = _render(
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "serviceMonitor.enabled=true",
        "--set", "serviceMonitor.relabelings.environment=staging",
        "--set", "serviceMonitor.relabelings.release=v0.1.2",
        "--set", "serviceMonitor.relabelings.cluster=sugarkube-pi",
    )

    service = _kind(docs, "Service")[0]
    monitor = _kind(docs, "ServiceMonitor")[0]
    deployment = _kind(docs, "Deployment")[0]

    assert service["metadata"]["name"] == "tokenplace"
    assert service["spec"]["ports"][0]["name"] == "http"
    assert monitor["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    assert monitor["spec"]["namespaceSelector"] == {"matchNames": ["tokenplace"]}
    assert monitor["spec"]["selector"] == {"matchLabels": service["spec"]["selector"]}
    endpoint = monitor["spec"]["endpoints"][0]
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"
    assert endpoint["interval"] == "30s"
    assert endpoint["scrapeTimeout"] == "10s"
    assert endpoint["bearerTokenSecret"] == {"name": "tokenplace-metrics", "key": "token"}

    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    metrics_env = next(item for item in env if item["name"] == "TOKENPLACE_METRICS_TOKEN")
    assert metrics_env["valueFrom"]["secretKeyRef"] == {"name": "tokenplace-metrics", "key": "token"}


def test_service_monitor_release_label_and_bounded_relabeling_are_configurable() -> None:
    docs = _render(
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "metrics.auth.secretKey=bearer",
        "--set", "serviceMonitor.enabled=true",
        "--set", "serviceMonitor.additionalLabels.release=observability",
        "--set", "serviceMonitor.relabelings.app=tokenplace-relay",
        "--set", "serviceMonitor.relabelings.environment=prod",
        "--set", "serviceMonitor.relabelings.release=v0.1.2",
        "--set", "serviceMonitor.relabelings.cluster=sugarkube-prod",
    )
    monitor = _kind(docs, "ServiceMonitor")[0]

    assert monitor["metadata"]["labels"]["release"] == "observability"
    endpoint = monitor["spec"]["endpoints"][0]
    assert endpoint["bearerTokenSecret"] == {"name": "tokenplace-metrics", "key": "bearer"}
    assert endpoint["relabelings"] == [
        {"targetLabel": "app", "replacement": "tokenplace-relay"},
        {"targetLabel": "environment", "replacement": "prod"},
        {"targetLabel": "release", "replacement": "v0.1.2"},
        {"targetLabel": "cluster", "replacement": "sugarkube-prod"},
    ]


def test_no_public_metrics_ingress_and_relay_constraints_are_preserved() -> None:
    docs = _render(
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "serviceMonitor.enabled=true",
        "--set", "ingress.enabled=true",
        "--set", "ingress.host=staging.token.place",
    )
    ingress = _kind(docs, "Ingress")[0]
    deployment = _kind(docs, "Deployment")[0]

    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert paths == [{
        "path": "/",
        "pathType": "Prefix",
        "backend": {"service": {"name": "tokenplace", "port": {"name": "http"}}},
    }]
    assert all(path["path"] != "/metrics" for path in paths)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    values = {item["name"]: item.get("value") for item in env}
    assert values["RELAY_WORKERS"] == "1"
    assert values["TOKENPLACE_RELAY_INTERNAL_URL"] == "http://127.0.0.1:$(RELAY_PORT)"


def test_service_monitor_requires_existing_secret_without_plaintext_token() -> None:
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _render("--set", "serviceMonitor.enabled=true")

    assert "metrics.auth.existingSecret is required" in excinfo.value.stderr
    chart_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [CHART_ROOT / "values.yaml", CHART_ROOT / "templates" / "servicemonitor.yaml"]
    )
    assert "tokenplace-metrics-token-value" not in chart_text
    assert "bearerTokenSecret" in chart_text
