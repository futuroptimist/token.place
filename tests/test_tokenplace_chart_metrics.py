"""Regression tests for the canonical token.place Helm metrics contract."""

import shutil
import subprocess

import pytest
import yaml


pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _render(*args: str) -> list[dict]:
    command = ["helm", "template", "tokenplace", "charts/tokenplace", "--namespace", "tokenplace", *args]
    rendered = subprocess.check_output(command, text=True)
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _deploy(docs: list[dict]) -> dict:
    return _kind(docs, "Deployment")[0]


def _env(deploy: dict) -> dict:
    env = deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    return {item["name"]: item for item in env}


def test_default_metrics_and_servicemonitor_disabled() -> None:
    docs = _render()
    assert not _kind(docs, "ServiceMonitor")
    assert "TOKENPLACE_METRICS_TOKEN" not in _env(_deploy(docs))
    assert not any(doc.get("kind") == "Ingress" and "/metrics" in str(doc) for doc in docs)


def test_enabled_servicemonitor_selects_service_and_uses_existing_secret() -> None:
    docs = _render(
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "serviceMonitor.enabled=true",
        "--set", "serviceMonitor.relabelings.environment=staging",
    )
    service = _kind(docs, "Service")[0]
    monitor = _kind(docs, "ServiceMonitor")[0]
    endpoint = monitor["spec"]["endpoints"][0]

    assert monitor["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    assert monitor["spec"]["selector"]["matchLabels"] == service["spec"]["selector"]
    assert endpoint["port"] == "http"
    assert endpoint["path"] == "/metrics"
    assert endpoint["authorization"]["type"] == "Bearer"
    assert endpoint["authorization"]["credentials"] == {"name": "tokenplace-metrics", "key": "token"}

    metrics_env = _env(_deploy(docs))["TOKENPLACE_METRICS_TOKEN"]
    assert metrics_env["valueFrom"]["secretKeyRef"] == {"name": "tokenplace-metrics", "key": "token"}

    relabels = {item["targetLabel"]: item["replacement"] for item in endpoint["relabelings"]}
    assert relabels == {
        "app": "tokenplace",
        "environment": "staging",
        "release": "tokenplace",
        "cluster": "sugarkube",
    }


def test_no_public_metrics_ingress_and_relay_constraints_preserved() -> None:
    docs = _render(
        "--set", "ingress.enabled=true",
        "--set", "ingress.host=staging.token.place",
        "--set", "metrics.enabled=true",
        "--set", "metrics.auth.existingSecret=tokenplace-metrics",
        "--set", "serviceMonitor.enabled=true",
    )
    deploy = _deploy(docs)
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    env = _env(deploy)

    assert deploy["spec"]["replicas"] == 1
    assert deploy["spec"]["strategy"]["type"] == "Recreate"
    assert env["RELAY_WORKERS"]["value"] == "1"
    assert env["RELAY_THREADS"]["value"] == "4"
    assert not any(doc.get("kind") == "Ingress" and "/metrics" in str(doc) for doc in docs)
    assert [port["name"] for port in container["ports"]] == ["http"]
