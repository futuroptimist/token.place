"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

import relay as relay_module
from relay import JsonFormatter, app, known_servers


@pytest.fixture()
def relay_client():
    """Provide a clean relay Flask test client."""

    known_servers.clear()
    with app.test_client() as client:
        yield client
    known_servers.clear()


def test_json_formatter_outputs_structured_payload() -> None:
    """JsonFormatter should emit parseable JSON with expected fields."""

    record = logging.LogRecord(
        name="tokenplace.relay",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="processed %s request",
        args=("chat",),
        exc_info=None,
    )
    record.request_id = "abc123"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "processed chat request"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tokenplace.relay"
    assert payload["request_id"] == "abc123"
    assert payload["timestamp"].endswith("Z")


@pytest.mark.integration
def test_metrics_endpoint_exposes_prometheus_text(relay_client) -> None:
    """/metrics should expose Prometheus plaintext suitable for scraping."""

    livez_response = relay_client.get("/livez")
    assert livez_response.status_code == 200

    metrics_response = relay_client.get("/metrics")
    assert metrics_response.status_code == 200
    assert metrics_response.mimetype.startswith("text/plain")

    body = metrics_response.get_data(as_text=True)
    assert "tokenplace_relay_requests_total" in body


def _metric_value(body: str, name: str) -> float:
    match = re.search(rf"^{re.escape(name)}\s+([0-9.eE+-]+)$", body, re.MULTILINE)
    assert match is not None, f"missing metric: {name}"
    return float(match.group(1))


def _scrape(relay_client, headers=None) -> str:
    response = relay_client.get("/metrics", headers=headers or {})
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_required_sugarkube_metric_families_and_zero_nodes(relay_client) -> None:
    """The maintenance contract exposes its exact names and initializes cleanly."""

    body = _scrape(relay_client)

    assert 'tokenplace_build_info{' in body
    assert _metric_value(body, "tokenplace_instrumentation_up") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 0
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0
    assert "tokenplace_relay_requests_total" in body


def test_compute_node_metrics_follow_api_v1_lease_semantics(relay_client) -> None:
    """Only fresh API v1 registrations are counted as healthy."""

    fresh_key = "test-api-v1-fresh"
    response = relay_client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": fresh_key},
    )
    assert response.status_code == 200
    body = _scrape(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 1

    known_servers[fresh_key]["last_ping"] = datetime.now() - timedelta(seconds=120)
    body = _scrape(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0


def test_legacy_registration_is_not_an_api_v1_compute_node(relay_client) -> None:
    """Legacy registrations cannot satisfy the production API v1 health gauge."""

    known_servers["legacy-node"] = {
        "public_key": "legacy-node",
        "last_ping": datetime.now(),
        "last_ping_duration": 30,
    }

    body = _scrape(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 0
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic abc", "Bearer", "Bearer ", "bearer metrics-test-token", "Bearer incorrect"],
)
def test_metrics_rejects_missing_malformed_and_incorrect_auth(
    relay_client, monkeypatch, authorization
) -> None:
    """Configured metrics authentication accepts only an exact Bearer credential."""

    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "metrics-test-token")
    headers = {"Authorization": authorization} if authorization is not None else {}

    response = relay_client.get("/metrics", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "metrics-test-token" not in response.get_data(as_text=True)


def test_metrics_accepts_correct_auth_without_logging_token(
    relay_client, monkeypatch, capsys
) -> None:
    """A correct bearer credential succeeds and remains absent from relay logs."""

    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "metrics-test-token")

    body = _scrape(
        relay_client,
        headers={"Authorization": "Bearer metrics-test-token"},
    )

    assert "tokenplace_instrumentation_up" in body
    assert "metrics-test-token" not in capsys.readouterr().out


def test_repeated_scrapes_do_not_mutate_relay_state(relay_client) -> None:
    """Gauge collection is observational and does not run lifecycle eviction."""

    known_servers["stale-api-v1-node"] = {
        "public_key": "stale-api-v1-node",
        relay_module.API_V1_SERVER_MARKER: True,
        "last_ping": datetime.now() - timedelta(seconds=120),
        "last_ping_duration": 30,
    }
    state_before = deepcopy(known_servers)

    first = _scrape(relay_client)
    second = _scrape(relay_client)

    assert _metric_value(first, "tokenplace_compute_nodes_healthy") == 0
    assert _metric_value(second, "tokenplace_compute_nodes_healthy") == 0
    assert known_servers == state_before
