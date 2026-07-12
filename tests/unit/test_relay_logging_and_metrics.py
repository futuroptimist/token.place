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
def clean_relay_state(monkeypatch):
    """Reset in-memory relay state around metrics tests."""

    import relay

    monkeypatch.delenv("TOKENPLACE_METRICS_TOKEN", raising=False)
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
    yield


def _capabilities():
    return {
        "api_version": "v1",
        "supported_model_ids": ["qwen3-8b-instruct"],
        "active_context_tier": "8k-fast",
        "maximum_total_context_tokens": 8192,
        "default_output_token_reservation": 1024,
        "maximum_output_tokens": 1024,
        "max_concurrency": 1,
        "backend_class": "cpu",
    }


def _envelope(server_key="server-key", client_key="client-key", request_id="request-1"):
    return {
        "server_public_key": server_key,
        "client_public_key": client_key,
        "request_id": request_id,
        "ciphertext": "sealed-box",
        "cipherkey": "sealed-key",
        "iv": "sealed-iv",
        "cancel_token": f"cancel-{request_id}",
    }


def _register(client, server_key="server-key"):
    return client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": server_key, "capabilities": _capabilities()},
    )


def _metrics_text(client, headers=None):
    response = client.get("/metrics", headers=headers or {})
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.mark.integration
def test_metrics_endpoint_requires_bearer_token_when_configured(
    relay_client, monkeypatch
) -> None:
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "scrape-secret")

    assert relay_client.get("/metrics").status_code == 401
    assert (
        relay_client.get(
            "/metrics", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert (
        relay_client.get(
            "/metrics", headers={"Authorization": "Bearer scrape-secret"}
        ).status_code
        == 200
    )


@pytest.mark.integration
def test_canonical_metrics_cover_queue_in_flight_completion_and_eviction(
    relay_client, monkeypatch
) -> None:
    import relay

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    assert _register(relay_client).status_code == 200
    text = _metrics_text(relay_client)
    assert "tokenplace_compute_nodes_registered" in text
    assert "tokenplace_compute_nodes_healthy" in text
    assert "tokenplace_build_info" in text
    assert "tokenplace_instrumentation_up 1.0" in text

    assert (
        relay_client.post("/api/v1/relay/requests", json=_envelope()).status_code == 200
    )
    text = _metrics_text(relay_client)
    assert "tokenplace_relay_queue_depth" in text
    assert "tokenplace_relay_oldest_queued_request_age_seconds" in text

    poll = relay_client.post(
        "/api/v1/relay/servers/poll", json={"server_public_key": "server-key"}
    )
    assert poll.status_code == 200
    assert poll.get_json()["request_id"] == "request-1"
    text = _metrics_text(relay_client)
    assert "tokenplace_relay_queue_depth" in text
    assert "tokenplace_relay_in_flight_requests" in text

    response = dict(_envelope(server_key="unused"))
    response.pop("server_public_key")
    assert (
        relay_client.post("/api/v1/relay/responses", json=response).status_code == 200
    )
    text = _metrics_text(relay_client)
    assert "tokenplace_relay_in_flight_requests 0.0" in text
    assert 'tokenplace_relay_request_outcomes_total{outcome="completed"}' in text

    assert (
        relay_client.post(
            "/api/v1/relay/servers/unregister",
            json={"server_public_key": "server-key"},
        ).status_code
        == 200
    )
    text = _metrics_text(relay_client)
    assert 'tokenplace_compute_node_evictions_total{reason="explicit_unregister"}' in text


@pytest.mark.integration
def test_terminal_outcome_metrics_cover_cancel_expire_and_timeout_mapping(
    relay_client, monkeypatch
) -> None:
    import relay

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    assert _register(relay_client).status_code == 200
    assert (
        relay_client.post(
            "/api/v1/relay/requests", json=_envelope(request_id="cancel-me")
        ).status_code
        == 200
    )
    cancel = relay_client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": "client-key",
            "request_id": "cancel-me",
            "cancel_token": "cancel-cancel-me",
            "status": "cancelled",
            "reason": "requester_cancelled",
        },
    )
    assert cancel.status_code == 200

    assert (
        relay_client.post(
            "/api/v1/relay/requests", json=_envelope(request_id="expire-me")
        ).status_code
        == 200
    )
    relay._cancel_api_v1_request(
        "client-key", "expire-me", status="expired", reason="provider_timeout"
    )

    rate_limited = relay_client.post(
        "/api/v1/relay/requests", json={"prompt": "plaintext forbidden"}
    )
    assert rate_limited.status_code == 400
    text = _metrics_text(relay_client)
    assert 'tokenplace_relay_request_outcomes_total{outcome="cancelled"}' in text
    assert 'tokenplace_relay_request_outcomes_total{outcome="expired"}' in text
    assert 'outcome="timed_out"' in text
    assert 'outcome="rate_limited"' in text


@pytest.mark.integration
def test_metrics_are_bounded_and_relay_blind_for_synthetic_sensitive_values(
    relay_client,
) -> None:
    for index in range(8):
        server_key = f"server-sensitive-{index}"
        client_key = f"client-sensitive-{index}"
        request_id = f"request-sensitive-{index}"
        assert _register(relay_client, server_key).status_code == 200
        payload = _envelope(
            server_key=server_key, client_key=client_key, request_id=request_id
        )
        assert relay_client.post(
            f"/api/v1/relay/requests?url=https://example.test/{index}",
            json=payload,
            headers={"User-Agent": f"agent-sensitive-{index}"},
        ).status_code in {200, 400, 404}

    body = _metrics_text(relay_client)
    for sensitive in (
        "server-sensitive-",
        "client-sensitive-",
        "request-sensitive-",
        "example.test",
        "agent-sensitive-",
        "sealed-box",
        "sealed-key",
    ):
        assert sensitive not in body
    assert "tokenplace_http_requests_total" in body
    assert "tokenplace_http_request_duration_seconds" in body
    assert body.count("tokenplace_http_requests_total{") < 80
