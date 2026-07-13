from __future__ import annotations

from pathlib import Path

import yaml

CHART = Path("charts/tokenplace")


def _read(relative: str) -> str:
    return (CHART / relative).read_text(encoding="utf-8")


def _values() -> dict:
    return yaml.safe_load(_read("values.yaml"))


def test_metrics_and_service_monitor_default_disabled_values() -> None:
    values = _values()

    assert values["metrics"]["enabled"] is False
    assert "path" not in values["metrics"]
    assert values["metrics"]["auth"]["existingSecret"] == ""
    assert values["metrics"]["auth"]["secretKey"] == "token"
    assert values["serviceMonitor"]["enabled"] is False
    assert values["serviceMonitor"]["interval"] == "30s"
    assert values["serviceMonitor"]["scrapeTimeout"] == "10s"


def test_metrics_token_is_injected_only_from_existing_secret() -> None:
    deployment = _read("templates/deployment.yaml")

    assert "{{- if .Values.metrics.enabled }}" in deployment
    assert 'required "metrics.auth.existingSecret is required when metrics.enabled=true"' in deployment
    assert '"TOKENPLACE_METRICS_TOKEN"' in deployment
    assert '"TOKENPLACE_METRICS_DISABLED"' in deployment
    assert '"valueFrom" (dict "secretKeyRef"' in deployment
    assert ".Values.metrics.auth.secretKey" in deployment
    assert "TOKENPLACE_METRICS_TOKEN" not in _read("values.yaml")


def test_service_monitor_renders_only_when_enabled_and_selects_canonical_service() -> None:
    service_monitor = _read("templates/servicemonitor.yaml")
    service = _read("templates/service.yaml")

    assert service_monitor.startswith("{{- if .Values.serviceMonitor.enabled }}")
    assert 'fail "metrics.enabled=true is required when serviceMonitor.enabled=true"' in service_monitor
    assert "kind: ServiceMonitor" in service_monitor
    assert "selector:\n    matchLabels:" in service_monitor
    assert 'include "tokenplace.selectorLabels"' in service_monitor
    assert "- port: http" in service_monitor
    assert 'path: "/metrics"' in service_monitor
    assert "targetPort: http" in service
    assert "- name: http" in service


def test_service_monitor_uses_supported_authorization_secret_reference() -> None:
    service_monitor = _read("templates/servicemonitor.yaml")

    assert "authorization:" in service_monitor
    assert "type: Bearer" in service_monitor
    assert "credentials:" in service_monitor
    assert "name: {{ $metricsSecret | quote }}" in service_monitor
    assert 'key: {{ .Values.metrics.auth.secretKey | quote }}' in service_monitor
    assert "bearerTokenSecret" not in service_monitor
    assert "bearerTokenFile" not in service_monitor


def test_service_monitor_release_discovery_label_is_configured_not_hard_coded() -> None:
    values = _values()
    service_monitor = _read("templates/servicemonitor.yaml")

    assert values["serviceMonitor"]["additionalLabels"]["release"] == "kube-prometheus-stack"
    assert "with .Values.serviceMonitor.additionalLabels" in service_monitor
    assert "release: kube-prometheus-stack" not in service_monitor


def test_service_monitor_bounded_relabeling_hooks() -> None:
    values = _values()
    service_monitor = _read("templates/servicemonitor.yaml")

    assert set(values["serviceMonitor"]["metricLabels"]) == {"app", "environment", "release", "cluster"}
    for label in ("app", "environment", "release", "cluster"):
        assert f"targetLabel: {label}" in service_monitor
    assert "sourceLabels" not in service_monitor


def test_ingress_does_not_create_public_metrics_path() -> None:
    ingress = _read("templates/ingress.yaml")

    assert "path: /metrics" not in ingress
    assert "metrics" not in ingress
    assert "serviceMonitor" not in ingress


def test_single_replica_recreate_one_worker_constraints_preserved() -> None:
    values = _values()
    deployment = _read("templates/deployment.yaml")

    assert values["replicaCount"] == 1
    assert values["strategy"]["type"] == "Recreate"
    assert '"RELAY_WORKERS" (dict "name" "RELAY_WORKERS" "value" "1")' in deployment
    assert "replicas: {{ .Values.replicaCount }}" in deployment
    assert '$strategyType := default "Recreate" .Values.strategy.type' in deployment


def test_service_monitor_rejects_reserved_additional_label_keys() -> None:
    service_monitor = _read("templates/servicemonitor.yaml")
    schema = _read("values.schema.json")

    assert "serviceMonitor.additionalLabels must not override chart label" in service_monitor
    assert "app.kubernetes.io/name" in schema
    assert "helm.sh/chart" in schema
