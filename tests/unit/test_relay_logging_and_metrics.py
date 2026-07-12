"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import logging
import time

import pytest

import relay
from relay import JsonFormatter, app


@pytest.fixture(autouse=True)
def clean_relay_state(monkeypatch):
    """Provide isolated in-memory relay state for metrics tests."""

    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS", "1")
    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    monkeypatch.setenv("TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS", "1")
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.client_pending_request_ids.clear()
    relay.client_terminal_request_ids.clear()
    relay.api_v1_recently_unregistered_servers.clear()
    yield
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.client_pending_request_ids.clear()
    relay.client_terminal_request_ids.clear()
    relay.api_v1_recently_unregistered_servers.clear()


@pytest.fixture()
def relay_client():
    """Provide a clean relay Flask test client."""

    with app.test_client() as client:
        yield client


def _capabilities(model_id: str = "qwen3-8b-instruct") -> dict[str, object]:
    return {
        "api_version": "v1",
        "supported_model_ids": [model_id],
        "active_context_tier": "8k-fast",
        "maximum_total_context_tokens": 8192,
        "default_output_token_reservation": 512,
        "maximum_output_tokens": 1024,
        "max_concurrency": 1,
        "backend_class": "cpu",
    }


def _register(client, server_key: str = "server-key"):
    return client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": server_key, "capabilities": _capabilities()},
    )


def _enqueue(client, server_key: str, client_key: str, request_id: str, *, cancel_token: str = "cancel"):
    return client.post(
        "/api/v1/relay/requests",
        json={
            "server_public_key": server_key,
            "client_public_key": client_key,
            "request_id": request_id,
            "ciphertext": "ciphertext-value",
            "cipherkey": "cipherkey-value",
            "iv": "iv-value",
            "cancel_token": cancel_token,
            "protocol": "e2ee-v1",
            "version": "v1",
        },
    )


def _metrics(client, **headers) -> str:
    response = client.get("/metrics", headers=headers)
    assert response.status_code == 200
    return response.get_data(as_text=True)


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

    body = _metrics(relay_client)
    assert "tokenplace_relay_requests_total" in body
    assert "tokenplace_http_requests_total" in body
    assert "tokenplace_instrumentation_up" in body


def test_metrics_endpoint_requires_bearer_token_when_configured(relay_client, monkeypatch) -> None:
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "scrape-secret")

    assert relay_client.get("/metrics").status_code == 401
    assert relay_client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = relay_client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})

    assert response.status_code == 200
    assert "tokenplace_instrumentation_up" in response.get_data(as_text=True)


def test_relay_queue_in_flight_completion_and_node_health_metrics(relay_client) -> None:
    assert _register(relay_client, "server-a").status_code == 200
    assert _enqueue(relay_client, "server-a", "client-a", "req-a").status_code == 200

    queued_body = _metrics(relay_client)
    assert 'tokenplace_compute_nodes_registered 1.0' in queued_body
    assert 'tokenplace_compute_nodes_healthy 1.0' in queued_body
    assert 'tokenplace_relay_queue_depth{provider_mode="api_v1_relay"} 1.0' in queued_body

    poll = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-a"})
    assert poll.status_code == 200
    in_flight_body = _metrics(relay_client)
    assert 'tokenplace_relay_queue_depth{provider_mode="api_v1_relay"} 0.0' in in_flight_body
    assert 'tokenplace_relay_in_flight_requests 1.0' in in_flight_body

    response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-a",
            "request_id": "req-a",
            "ciphertext": "response-ciphertext",
            "cipherkey": "response-cipherkey",
            "iv": "response-iv",
            "protocol": "e2ee-v1",
            "version": "v1",
        },
    )
    assert response.status_code == 200
    completed_body = _metrics(relay_client)
    assert 'tokenplace_relay_in_flight_requests 0.0' in completed_body
    assert 'tokenplace_relay_request_outcomes_total{outcome="completed"}' in completed_body

    relay.known_servers["server-a"]["last_ping"] = time.time() - 10
    stale_body = _metrics(relay_client)
    assert 'tokenplace_compute_nodes_registered 1.0' in stale_body
    assert 'tokenplace_compute_nodes_healthy 0.0' in stale_body


def test_cancellation_expiration_eviction_and_rate_limit_outcomes(relay_client, monkeypatch) -> None:
    assert _register(relay_client, "server-b").status_code == 200
    assert _enqueue(relay_client, "server-b", "client-b", "req-cancel", cancel_token="proof").status_code == 200
    cancel = relay_client.post(
        "/api/v1/relay/requests/cancel",
        json={"client_public_key": "client-b", "request_id": "req-cancel", "cancel_token": "proof"},
    )
    assert cancel.status_code == 200

    assert _enqueue(relay_client, "server-b", "client-b", "req-expire", cancel_token="proof2").status_code == 200
    relay.client_pending_request_ids["client-b"]["req-expire"] = {"queued_at": time.time() - 999, "cancel_token": "proof2"}
    relay._expire_pending_request_if_stale("client-b", "req-expire")

    relay.known_servers["server-b"]["last_ping"] = time.time() - 10
    relay._evict_stale_servers()

    monkeypatch.setenv("API_RATE_LIMIT", "1/hour")
    relay.RELAY_REQUEST_OUTCOMES.labels("rate_limited").inc()

    body = _metrics(relay_client)
    assert 'tokenplace_relay_request_outcomes_total{outcome="cancelled"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="expired"}' in body
    assert 'tokenplace_relay_request_outcomes_total{outcome="rate_limited"}' in body
    assert 'tokenplace_compute_node_evictions_total{reason="stale_lease"}' in body


def test_sensitive_inputs_do_not_change_metric_labels_or_appear(relay_client) -> None:
    baseline = _metrics(relay_client)
    baseline_label_lines = {line for line in baseline.splitlines() if line.startswith("tokenplace_http_requests_total{")}

    for idx in range(20):
        unique = f"sensitive-{idx}-https://example.invalid/path/{idx}-model-{idx}"
        relay_client.post(
            "/api/v1/relay/requests",
            json={
                "server_public_key": f"server-{unique}",
                "client_public_key": f"client-{unique}",
                "request_id": f"request-{unique}",
                "ciphertext": f"ciphertext-{unique}",
                "cipherkey": f"cipherkey-{unique}",
                "iv": f"iv-{unique}",
                "model": f"model-{unique}",
                "url": f"https://example.invalid/{unique}",
                "error": f"error-{unique}",
            },
            headers={"User-Agent": f"agent-{unique}", "Authorization": f"Bearer {unique}"},
        )

    body = _metrics(relay_client)
    label_lines = {line for line in body.splitlines() if line.startswith("tokenplace_http_requests_total{")}
    assert len(label_lines - baseline_label_lines) <= 2
    for forbidden in ("sensitive-", "ciphertext-", "cipherkey-", "https://example.invalid", "agent-"):
        assert forbidden not in body
