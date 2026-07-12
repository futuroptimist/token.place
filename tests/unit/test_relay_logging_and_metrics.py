"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import logging

import pytest

from relay import JsonFormatter, app


@pytest.fixture()
def relay_client():
    """Provide a clean relay Flask test client."""

    with app.test_client() as client:
        yield client


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

@pytest.fixture(autouse=True)
def clear_relay_state(monkeypatch):
    import relay

    with relay.server_round_robin_lock:
        relay.known_servers.clear()
        relay.server_round_robin_next_index = 0
    with relay.client_inference_requests_changed:
        relay.client_inference_requests.clear()
    with relay.client_responses_lock:
        relay.client_responses.clear()
    with relay.client_pending_request_ids_lock:
        relay.client_pending_request_ids.clear()
    with relay.client_terminal_request_ids_lock:
        relay.client_terminal_request_ids.clear()
    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
    monkeypatch.setenv("API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT", "3/hour")
    yield
    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)


def _server_payload(server_key="server-key", **extra):
    payload = {"server_public_key": server_key}
    payload.update(extra)
    return payload


def _envelope(server_key="server-key", client_key="client-key", request_id="request-id", **extra):
    payload = {
        "server_public_key": server_key,
        "client_public_key": client_key,
        "request_id": request_id,
        "ciphertext": "ciphertext-value",
        "cipherkey": "cipherkey-value",
        "iv": "iv-value",
        "cancel_token": f"cancel-{request_id}",
    }
    payload.update(extra)
    return payload


def test_metrics_token_authentication(relay_client, monkeypatch) -> None:
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "scrape-secret")

    assert relay_client.get("/metrics").status_code == 401
    assert relay_client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = relay_client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert response.status_code == 200
    assert "tokenplace_instrumentation_up" in response.get_data(as_text=True)


def test_relay_queue_in_flight_node_and_outcome_metrics(relay_client, monkeypatch) -> None:
    import relay

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    assert relay_client.post("/api/v1/relay/servers/register", json=_server_payload()).status_code == 200
    assert relay_client.post("/api/v1/relay/requests", json=_envelope()).status_code == 200

    body = relay_client.get("/metrics").get_data(as_text=True)
    assert 'tokenplace_relay_queue_depth{provider_mode="relay"} 1.0' in body
    assert "tokenplace_relay_oldest_queued_request_age_seconds" in body
    assert "tokenplace_compute_nodes_registered 1.0" in body
    assert "tokenplace_compute_nodes_healthy 1.0" in body

    poll = relay_client.post("/api/v1/relay/servers/poll", json=_server_payload())
    assert poll.status_code == 200
    body = relay_client.get("/metrics").get_data(as_text=True)
    assert 'tokenplace_relay_queue_depth{provider_mode="relay"} 0.0' in body
    assert "tokenplace_relay_in_flight_requests 1.0" in body

    response_payload = _envelope(server_key=None)
    response_payload.pop("server_public_key", None)
    assert relay_client.post("/api/v1/relay/responses", json=response_payload).status_code == 200
    body = relay_client.get("/metrics").get_data(as_text=True)
    assert "tokenplace_relay_in_flight_requests 0.0" in body
    relay._cancel_api_v1_request("client-key", "timeout-id", status="expired", reason="provider_timeout")
    relay._cancel_api_v1_request("client-key", "expired-id", status="expired", reason="pending_request_ttl_exceeded")
    relay._record_relay_request_outcome("rate_limited")
    relay._record_relay_request_outcome("dependency_failure")
    relay._record_relay_request_outcome("failed")
    body = relay_client.get("/metrics").get_data(as_text=True)
    assert 'tokenplace_relay_request_outcomes_total{outcome="completed"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="timed_out"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="expired"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="rate_limited"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="dependency_failure"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="failed"}' in body

    relay_client.post("/api/v1/relay/servers/register", json=_server_payload())
    with relay.server_round_robin_lock:
        assert relay.known_servers
        for server_payload in relay.known_servers.values():
            server_payload["last_ping"] = relay.datetime.fromtimestamp(0)
    assert relay._evict_stale_servers()
    body = relay_client.get("/metrics").get_data(as_text=True)
    assert 'tokenplace_compute_node_evictions_total{reason="stale_lease"}' in body


def test_metrics_are_bounded_and_relay_blind_for_synthetic_values(relay_client) -> None:
    import relay

    for idx in range(20):
        key = f"server-key-{idx}-sensitive"
        client_key = f"client-key-{idx}-sensitive"
        request_id = f"request-id-{idx}-sensitive"
        url = f"https://example.invalid/{idx}/sensitive"
        model = f"model-sensitive-{idx}"
        error = f"raw-error-sensitive-{idx}"
        relay_client.post("/api/v1/relay/servers/register", json=_server_payload(key, url=url, model=model, error=error))
        relay_client.post("/api/v1/relay/requests", json=_envelope(key, client_key, request_id, model=model, url=url, error=error))
        relay._cancel_api_v1_request(client_key, request_id, status="cancelled", reason="client_cancelled")

    body = relay_client.get("/metrics").get_data(as_text=True)
    forbidden = ["sensitive", "ciphertext-value", "cipherkey-value", "iv-value", "raw-error"]
    assert not any(value in body for value in forbidden)
    http_label_lines = [
        line for line in body.splitlines()
        if line.startswith("tokenplace_http_requests_total{")
    ]
    assert http_label_lines
    assert all("/api/v1/relay/requests" in line or "route=" in line for line in http_label_lines)
    assert 'outcome="cancelled"' in body
