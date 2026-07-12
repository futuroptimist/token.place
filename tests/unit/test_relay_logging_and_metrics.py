"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

import pytest

import relay as relay_module
from relay import (
    JsonFormatter,
    api_v1_recently_unregistered_servers,
    api_v1_filtered_round_robin_next_positions,
    app,
    client_inference_requests,
    client_pending_request_ids,
    client_responses,
    client_terminal_request_ids,
    client_terminal_outcomes,
    known_servers,
    streaming_sessions,
    streaming_sessions_by_client,
)


@pytest.fixture()
def relay_client():
    """Provide a clean relay Flask test client."""

    app.config["TESTING"] = True
    known_servers.clear()
    client_inference_requests.clear()
    client_pending_request_ids.clear()
    client_terminal_request_ids.clear()
    client_terminal_outcomes.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()
    api_v1_recently_unregistered_servers.clear()
    api_v1_filtered_round_robin_next_positions.clear()
    relay_module.server_round_robin_next_index = 0
    with app.test_client() as client:
        yield client
    known_servers.clear()
    client_inference_requests.clear()
    client_pending_request_ids.clear()
    client_terminal_request_ids.clear()
    client_terminal_outcomes.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()
    api_v1_recently_unregistered_servers.clear()
    api_v1_filtered_round_robin_next_positions.clear()
    relay_module.server_round_robin_next_index = 0


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


def _register_node(client, server_key="server-key", *, token=None):
    headers = {"X-Relay-Server-Token": token} if token else {}
    payload = {
        "server_public_key": server_key,
        "capabilities": {
            "api_version": "v1",
            "supported_model_ids": ["qwen3-8b-instruct"],
            "active_context_tier": "8k-fast",
            "maximum_total_context_tokens": 8192,
            "default_output_token_reservation": 256,
            "maximum_output_tokens": 512,
            "max_concurrency": 1,
            "backend_class": "cpu",
        },
    }
    response = client.post("/api/v1/relay/servers/register", json=payload, headers=headers)
    assert response.status_code == 200
    return response


def _queue_request(client, *, server_key="server-key", client_key="client-key", request_id="request-1"):
    response = client.post(
        "/api/v1/relay/requests",
        json={
            "server_public_key": server_key,
            "client_public_key": client_key,
            "request_id": request_id,
            "cancel_token": f"cancel-{request_id}",
            "ciphertext": "sealed-ciphertext",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )
    assert response.status_code == 200
    return response


def _metric_body(client, headers=None):
    response = client.get("/metrics", headers=headers or {})
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _metric_value(body: str, metric_name: str, labels: str = "") -> float:
    label_pattern = re.escape(labels)
    pattern = rf"^{re.escape(metric_name)}{label_pattern}\s+([0-9.eE+-]+)$"
    for line in body.splitlines():
        match = re.match(pattern, line)
        if match:
            return float(match.group(1))
    raise AssertionError(f"metric not found: {metric_name}{labels}")


def test_canonical_metrics_track_queue_in_flight_completion_and_eviction(relay_client) -> None:
    """Canonical gauges and counters reflect bounded relay state transitions."""

    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-complete")

    body = _metric_body(relay_client)
    assert _metric_value(body, 'tokenplace_relay_queue_depth', '{provider_mode="relay"}') == 1
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 1
    assert _metric_value(body, "tokenplace_relay_oldest_queued_request_age_seconds", '{provider_mode="relay"}') >= 0

    poll = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-key"})
    assert poll.status_code == 200
    body = _metric_body(relay_client)
    assert _metric_value(body, 'tokenplace_relay_queue_depth', '{provider_mode="relay"}') == 0
    assert _metric_value(body, "tokenplace_relay_in_flight_requests") == 1
    assert _metric_value(body, "tokenplace_relay_oldest_in_flight_age_seconds") >= 0

    response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-key",
            "request_id": "request-complete",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )
    assert response.status_code == 200
    body = _metric_body(relay_client)
    assert _metric_value(body, "tokenplace_relay_in_flight_requests") == 0
    assert _metric_value(body, 'tokenplace_relay_request_outcomes_total{outcome="completed"}') >= 1

    known_servers["server-key"]["last_ping"] = datetime.now() - timedelta(seconds=120)
    relay_client.get("/healthz")
    body = _metric_body(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 0
    assert _metric_value(body, 'tokenplace_compute_node_evictions_total{reason="stale_lease"}') >= 1


def test_metrics_endpoint_optional_bearer_auth(relay_client, monkeypatch) -> None:
    """TOKENPLACE_METRICS_TOKEN protects /metrics without affecting health."""

    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "metrics-secret")
    assert relay_client.get("/healthz").status_code == 200
    assert relay_client.get("/metrics").status_code == 401
    assert relay_client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = relay_client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
    assert response.status_code == 200
    assert "tokenplace_instrumentation_up" in response.get_data(as_text=True)


def test_request_outcomes_cover_cancel_expire_timeout_and_rate_limit(relay_client, monkeypatch) -> None:
    """Terminal transitions increment fixed outcome counters without identity labels."""

    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-cancel")
    cancel = relay_client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": "client-key",
            "request_id": "request-cancel",
            "cancel_token": "cancel-request-cancel",
            "status": "cancelled",
            "reason": "requester_cancelled",
        },
    )
    assert cancel.status_code == 200

    _queue_request(relay_client, request_id="request-timeout")
    claimed = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-key"})
    assert claimed.status_code == 200
    relay_module._cancel_api_v1_request(
        "client-key",
        "request-timeout",
        status="expired",
        reason="provider_timeout",
    )

    monkeypatch.setattr(relay_module, "PENDING_REQUEST_TTL_SECONDS", 0.001)
    _queue_request(relay_client, request_id="request-expire")
    time.sleep(0.01)
    relay_module._expire_stale_pending_requests()

    before_rate_limit_body = _metric_body(relay_client)
    before_rate_limit = _metric_value(
        before_rate_limit_body,
        'tokenplace_relay_request_outcomes_total',
        '{outcome="rate_limited"}',
    )
    with app.test_request_context("/api/v1/relay/requests", method="POST"):
        relay_module.g.request_start_time = time.time()
        response = relay_module._log_request(relay_module.Response("rate limited", status=429))
    assert response.status_code == 429

    body = _metric_body(relay_client)
    assert _metric_value(body, 'tokenplace_relay_request_outcomes_total', '{outcome="cancelled"}') >= 1
    assert _metric_value(body, 'tokenplace_relay_request_outcomes_total', '{outcome="timed_out"}') >= 1
    assert _metric_value(body, 'tokenplace_relay_request_outcomes_total', '{outcome="expired"}') >= 1
    assert (
        _metric_value(body, 'tokenplace_relay_request_outcomes_total', '{outcome="rate_limited"}')
        == before_rate_limit + 1
    )


def test_response_completion_does_not_double_count_after_terminal_race(relay_client) -> None:
    """A late completion after cancellation should not emit a second terminal outcome."""

    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-race")
    poll = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-key"})
    assert poll.status_code == 200
    relay_module._cancel_api_v1_request(
        "client-key",
        "request-race",
        status="cancelled",
        reason="requester_cancelled",
    )
    before = _metric_body(relay_client)
    before_completed = _metric_value(
        before,
        'tokenplace_relay_request_outcomes_total',
        '{outcome="completed"}',
    )
    before_cancelled = _metric_value(
        before,
        'tokenplace_relay_request_outcomes_total',
        '{outcome="cancelled"}',
    )

    response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-key",
            "request_id": "request-race",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )

    assert response.status_code == 410
    after = _metric_body(relay_client)
    assert (
        _metric_value(after, 'tokenplace_relay_request_outcomes_total', '{outcome="completed"}')
        == before_completed
    )
    assert (
        _metric_value(after, 'tokenplace_relay_request_outcomes_total', '{outcome="cancelled"}')
        == before_cancelled
    )


def test_failed_http_responses_are_not_counted_as_completed(relay_client, monkeypatch) -> None:
    """Metrics auth failures should be failed HTTP outcomes, not completed traffic."""

    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "metrics-secret")
    before = _metric_body(relay_client, headers={"Authorization": "Bearer metrics-secret"})
    before_failed = _metric_value(
        before,
        'tokenplace_http_requests_total',
        '{method="GET",outcome="failed",provider_mode="relay",route="/metrics",status_class="4xx"}',
    )
    before_completed = 0.0
    try:
        before_completed = _metric_value(
            before,
            'tokenplace_http_requests_total',
            '{method="GET",outcome="completed",provider_mode="relay",route="/metrics",status_class="4xx"}',
        )
    except AssertionError:
        pass

    response = relay_client.get("/metrics", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401
    after = _metric_body(relay_client, headers={"Authorization": "Bearer metrics-secret"})
    assert (
        _metric_value(
            after,
            'tokenplace_http_requests_total',
            '{method="GET",outcome="failed",provider_mode="relay",route="/metrics",status_class="4xx"}',
        )
        == before_failed + 1
    )
    try:
        after_completed = _metric_value(
            after,
            'tokenplace_http_requests_total',
            '{method="GET",outcome="completed",provider_mode="relay",route="/metrics",status_class="4xx"}',
        )
    except AssertionError:
        after_completed = 0.0
    assert after_completed == before_completed

def test_metrics_do_not_expose_high_cardinality_or_sensitive_values(relay_client, caplog) -> None:
    """Synthetic identities and payload values do not become metric labels or logs."""

    sensitive_values = set()
    caplog.set_level(logging.INFO, logger="tokenplace.relay")
    before_body = _metric_body(relay_client)
    before_label_lines = {
        line for line in before_body.splitlines()
        if line.startswith("tokenplace_http_requests_total{")
    }
    for idx in range(25):
        server_key = f"server-sensitive-{idx}"
        client_key = f"client-sensitive-{idx}"
        request_id = f"request-sensitive-{idx}"
        model = f"model-sensitive-{idx}"
        url = f"https://example.invalid/sensitive/{idx}"
        error = f"raw-error-sensitive-{idx}"
        sensitive_values.update({server_key, client_key, request_id, model, url, error})
        _register_node(relay_client, server_key=server_key)
        relay_client.post(
            f"/api/v1/relay/requests?next={url}",
            json={
                "server_public_key": server_key,
                "client_public_key": client_key,
                "request_id": request_id,
                "model": model,
                "error": error,
                "ciphertext": f"cipher-sensitive-{idx}",
                "cipherkey": f"key-sensitive-{idx}",
                "iv": f"iv-sensitive-{idx}",
            },
            headers={"User-Agent": f"agent-sensitive-{idx}"},
        )
        sensitive_values.update({f"cipher-sensitive-{idx}", f"key-sensitive-{idx}", f"iv-sensitive-{idx}", f"agent-sensitive-{idx}"})

    body = _metric_body(relay_client)
    for value in sensitive_values:
        assert value not in body

    label_lines = {
        line for line in body.splitlines()
        if line.startswith("tokenplace_http_requests_total{")
    }
    assert len(label_lines - before_label_lines) <= 3

    logs = "\n".join(record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records)
    for value in sensitive_values:
        assert value not in logs
