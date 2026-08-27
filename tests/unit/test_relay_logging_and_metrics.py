"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta

import pytest

import relay
from relay import JsonFormatter, app


@pytest.fixture()
def relay_client(monkeypatch):
    """Provide a clean relay Flask test client."""

    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
    with relay.server_round_robin_lock:
        relay.known_servers.clear()
    with app.test_client() as client:
        yield client
    with relay.server_round_robin_lock:
        relay.known_servers.clear()


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
    required_names = {
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_compute_nodes_registered",
        "tokenplace_compute_nodes_healthy",
        "tokenplace_relay_requests_total",
    }
    assert all(name in body for name in required_names)
    assert "tokenplace_instrumentation_up 1.0" in body
    assert "tokenplace_compute_nodes_registered 0.0" in body
    assert "tokenplace_compute_nodes_healthy 0.0" in body


def test_metrics_counts_only_healthy_api_v1_nodes(relay_client) -> None:
    """Gauges should follow API v1 registration and existing lease semantics."""

    with relay.server_round_robin_lock:
        relay.known_servers.update(
            {
                "fresh": {
                    relay.API_V1_SERVER_MARKER: True,
                    "last_ping": datetime.now(),
                    "last_ping_duration": 30,
                },
                "stale": {
                    relay.API_V1_SERVER_MARKER: True,
                    "last_ping": datetime.now() - timedelta(seconds=31),
                    "last_ping_duration": 30,
                },
                "legacy": {
                    "last_ping": datetime.now(),
                    "last_ping_duration": 30,
                },
            }
        )

    body = relay_client.get("/metrics").get_data(as_text=True)

    assert "tokenplace_compute_nodes_registered 2.0" in body
    assert "tokenplace_compute_nodes_healthy 1.0" in body


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic malformed", "Bearer", "Bearer incorrect"],
)
def test_metrics_rejects_invalid_credentials(
    relay_client, monkeypatch, authorization
) -> None:
    """Configured metrics authentication should fail closed for invalid headers."""

    expected = secrets.token_urlsafe()
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", expected)
    headers = {"Authorization": authorization} if authorization is not None else {}

    response = relay_client.get("/metrics", headers=headers)

    assert response.status_code == 401
    assert expected not in response.get_data(as_text=True)


def test_metrics_accepts_correct_credential_without_logging_it(
    relay_client, monkeypatch, caplog
) -> None:
    """The matching bearer value succeeds and never appears in relay logs."""

    expected = secrets.token_urlsafe()
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", expected)
    caplog.set_level(logging.DEBUG)

    response = relay_client.get(
        "/metrics", headers={"Authorization": f"Bearer {expected}"}
    )

    assert response.status_code == 200
    assert expected not in caplog.text


def test_repeated_metrics_scrapes_do_not_mutate_relay_state(relay_client) -> None:
    """Collection must not evict nodes or advance unrelated counters."""

    stale_payload = {
        relay.API_V1_SERVER_MARKER: True,
        "last_ping": datetime.now() - timedelta(seconds=31),
        "last_ping_duration": 30,
    }
    with relay.server_round_robin_lock:
        relay.known_servers["stale"] = stale_payload
        before = dict(relay.known_servers)
    before_counter = relay.REQUEST_COUNTER.labels(
        "POST", "api_v1_relay_servers_register", "200"
    )._value.get()

    assert relay_client.get("/metrics").status_code == 200
    assert relay_client.get("/metrics").status_code == 200

    with relay.server_round_robin_lock:
        assert relay.known_servers == before
        assert relay.known_servers["stale"] is stale_payload
    assert (
        relay.REQUEST_COUNTER.labels(
            "POST", "api_v1_relay_servers_register", "200"
        )._value.get()
        == before_counter
    )
