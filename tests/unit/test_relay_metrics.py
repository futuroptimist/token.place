"""Focused contract tests for production relay Prometheus metrics."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

import relay


def _metric_value(response, name: str) -> float:
    prefix = f"{name} "
    line = next(line for line in response.get_data(as_text=True).splitlines() if line.startswith(prefix))
    return float(line.removeprefix(prefix))


def _api_v1_node(*, age_seconds: float = 0) -> dict[str, object]:
    return {
        "public_key": "opaque-test-node",
        "last_ping": datetime.now() - timedelta(seconds=age_seconds),
        "last_ping_duration": 30,
        relay.API_V1_SERVER_MARKER: True,
    }


def test_required_metric_families_and_initialization(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
    relay.known_servers.clear()
    with relay.app.test_client() as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for name in (
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_compute_nodes_registered",
        "tokenplace_compute_nodes_healthy",
        "tokenplace_relay_requests_total",
    ):
        assert name in body
    assert _metric_value(response, "tokenplace_instrumentation_up") == 1
    assert _metric_value(response, "tokenplace_compute_nodes_registered") == 0
    assert _metric_value(response, "tokenplace_compute_nodes_healthy") == 0


def test_compute_node_gauges_follow_api_v1_registration_and_lease(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
    relay.known_servers.clear()
    relay.known_servers["fresh"] = _api_v1_node()
    relay.known_servers["stale"] = _api_v1_node(age_seconds=31)
    relay.known_servers["legacy"] = {
        "public_key": "legacy-node",
        "last_ping": datetime.now(),
        "last_ping_duration": 30,
    }
    with relay.app.test_client() as client:
        response = client.get("/metrics")

    assert _metric_value(response, "tokenplace_compute_nodes_registered") == 2
    assert _metric_value(response, "tokenplace_compute_nodes_healthy") == 1


def test_metrics_authentication_rejects_invalid_credentials(monkeypatch, capfd):
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "configured-test-value")
    with relay.app.test_client() as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Basic malformed"}).status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer incorrect"}).status_code == 401
        response = client.get(
            "/metrics", headers={"Authorization": "Bearer configured-test-value"}
        )
    assert response.status_code == 200
    assert "configured-test-value" not in response.get_data(as_text=True)
    captured = capfd.readouterr()
    assert "configured-test-value" not in captured.out
    assert "configured-test-value" not in captured.err


def test_repeated_scrapes_do_not_mutate_relay_state(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
    relay.known_servers.clear()
    relay.known_servers["stale"] = _api_v1_node(age_seconds=31)
    before = deepcopy(relay.known_servers)
    with relay.app.test_client() as client:
        first = client.get("/metrics")
        second = client.get("/metrics")

    assert first.status_code == second.status_code == 200
    assert relay.known_servers == before
    assert _metric_value(second, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(second, "tokenplace_compute_nodes_healthy") == 0
