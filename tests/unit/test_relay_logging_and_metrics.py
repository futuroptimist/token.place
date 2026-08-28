"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import relay
from relay import JsonFormatter, app, known_servers


@pytest.fixture()
def relay_client():
    """Provide a clean relay Flask test client."""

    app.config["TESTING"] = True
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


def _scrape(client, headers=None):
    response = client.get("/metrics", headers=headers or {})
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _register_api_v1_node(client, key="node-a"):
    response = client.post(
        "/api/v1/relay/servers/register", json={"server_public_key": key}
    )
    assert response.status_code == 200


def test_required_sugarkube_metric_families_and_initialization(relay_client) -> None:
    body = _scrape(relay_client)
    for metric in (
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_compute_nodes_registered",
        "tokenplace_compute_nodes_healthy",
        "tokenplace_relay_requests_total",
    ):
        assert metric in body
    assert _metric_value(body, "tokenplace_instrumentation_up") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 0
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0


def test_maintenance_gauges_have_one_pid_free_multiprocess_snapshot(tmp_path) -> None:
    """The production multiprocess exposition must not create per-worker gauges."""

    script = textwrap.dedent(
        """
        import os
        import re
        import secrets
        import subprocess
        import sys

        worker = '''
        import relay
        from prometheus_client import Gauge
        Gauge("tokenplace_default_mode_control", "Regression control.").set(1)
        relay.COMPUTE_NODES_REGISTERED.set(1)
        relay.COMPUTE_NODES_HEALTHY.set(1)
        '''
        for _ in range(2):
            subprocess.run([sys.executable, "-c", worker], check=True)

        import relay
        from prometheus_client import Gauge

        Gauge("tokenplace_default_mode_control", "Regression control.").set(1)

        token = secrets.token_urlsafe(24)
        os.environ["TOKENPLACE_METRICS_TOKEN"] = token
        client = relay.app.test_client()
        assert client.get("/metrics").status_code == 401
        relay.known_servers["node-a"] = {
            relay.API_V1_SERVER_MARKER: True,
            "last_ping": relay.datetime.now(),
            "last_ping_duration": 30,
        }
        response = client.get(
            "/metrics", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert token not in body

        names = (
            "tokenplace_build_info",
            "tokenplace_instrumentation_up",
            "tokenplace_compute_nodes_registered",
            "tokenplace_compute_nodes_healthy",
        )
        series = {
            name: [
                line for line in body.splitlines()
                if re.match(rf"^{name}(?:\\{{|\\s)", line)
            ]
            for name in names
        }
        assert all(len(lines) == 1 for lines in series.values()), series
        assert all('pid=' not in lines[0] for lines in series.values()), series
        assert 'version=' in series["tokenplace_build_info"][0]
        assert 'revision=' in series["tokenplace_build_info"][0]
        for name in names[1:]:
            assert "{" not in series[name][0]
        for name in names:
            assert float(series[name][0].rsplit(" ", 1)[1]) == 1

        # Prove the test exercises the real default multiprocess behavior: a
        # maintenance gauge restored to the default mode would fail above.
        control = [
            line for line in body.splitlines()
            if line.startswith("tokenplace_default_mode_control{")
        ]
        assert len(control) == 3
        assert all('pid=' in line for line in control)
        print("multiprocess metrics assertions passed")
        """
    )
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).parents[2]),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines()[-1] == "multiprocess metrics assertions passed"


def test_compute_node_gauges_follow_api_v1_lease_semantics(relay_client) -> None:
    _register_api_v1_node(relay_client)
    body = _scrape(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 1

    known_servers["node-a"]["last_ping"] = datetime.now() - timedelta(seconds=120)
    body = _scrape(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0


def test_stale_node_health_honors_active_polling_and_in_flight_leases(
    relay_client, monkeypatch
) -> None:
    now_monotonic = 1_000.0
    monkeypatch.setattr(relay.time, "monotonic", lambda: now_monotonic)
    _register_api_v1_node(relay_client)
    node = known_servers["node-a"]
    node["last_ping"] = datetime.now() - timedelta(seconds=120)

    node["polling_until_monotonic"] = now_monotonic + 1
    assert _metric_value(_scrape(relay_client), "tokenplace_compute_nodes_healthy") == 1
    node["polling_until_monotonic"] = now_monotonic

    node["api_v1_in_flight_requests"] = {
        "request-a": {"expires_at": now_monotonic + 1}
    }
    assert _metric_value(_scrape(relay_client), "tokenplace_compute_nodes_healthy") == 1
    node["api_v1_in_flight_requests"]["request-a"]["expires_at"] = now_monotonic

    assert _metric_value(_scrape(relay_client), "tokenplace_compute_nodes_healthy") == 0


def test_legacy_registration_is_not_counted_as_api_v1(relay_client) -> None:
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
    [None, "", "Basic incorrect", "Bearer", "Bearer "],
)
def test_metrics_auth_rejects_missing_and_malformed_credentials(
    relay_client, monkeypatch, authorization
) -> None:
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", secrets.token_urlsafe(24))
    headers = {"Authorization": authorization} if authorization is not None else {}
    assert relay_client.get("/metrics", headers=headers).status_code == 401


def test_metrics_auth_rejects_incorrect_bearer_credential(
    relay_client, monkeypatch
) -> None:
    expected_token = secrets.token_urlsafe(24)
    incorrect_token = secrets.token_urlsafe(24)
    assert incorrect_token != expected_token
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", expected_token)

    response = relay_client.get(
        "/metrics", headers={"Authorization": f"Bearer {incorrect_token}"}
    )

    assert response.status_code == 401


@pytest.mark.parametrize("authorization", [None, "Bearer "])
def test_metrics_auth_explicit_empty_token_fails_closed(
    relay_client, monkeypatch, authorization
) -> None:
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "")
    headers = {"Authorization": authorization} if authorization is not None else {}
    assert relay_client.get("/metrics", headers=headers).status_code == 401


def test_metrics_auth_accepts_correct_credential_without_logging_it(
    relay_client, monkeypatch, caplog
) -> None:
    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", token)
    with caplog.at_level(logging.DEBUG):
        response = relay_client.get(
            "/metrics", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert token not in caplog.text
    assert token not in response.get_data(as_text=True)


def test_repeated_scrapes_are_read_only_for_relay_state(relay_client) -> None:
    _register_api_v1_node(relay_client)
    known_servers["node-a"]["sentinel"] = {"unchanged": True}
    before = {
        key: dict(value) for key, value in known_servers.items()
    }
    request_series = (
        'tokenplace_relay_requests_total{endpoint="api_v1_relay_servers_register",'
        'method="POST",status="200"}'
    )
    before_scrapes = _metric_value(_scrape(relay_client), request_series)

    _scrape(relay_client)
    after_scrapes = _metric_value(_scrape(relay_client), request_series)

    assert known_servers == before
    assert after_scrapes == before_scrapes


def test_api_v1_model_catalog_remains_llama_31_only(relay_client) -> None:
    response = relay_client.get("/api/v1/models")
    assert response.status_code == 200
    assert [model["id"] for model in response.get_json()["data"]] == [
        "llama-3.1-8b-instruct"
    ]
