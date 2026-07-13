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


def test_build_revision_label_falls_back_to_display_version(monkeypatch) -> None:
    """Build info should remain identifiable when release metadata omits ref."""

    monkeypatch.setattr(relay_module, "resolve_deploy_ref", lambda: "")

    assert (
        relay_module._build_revision_label(
            {
                "environment": "staging",
                "version": "main-830d0a4",
                "label": "staging main-830d0a4",
            }
        )
        == "main-830d0a4"
    )


def test_build_revision_label_prefers_ref_then_resolved_deploy_ref(monkeypatch) -> None:
    """Explicit immutable refs should remain preferred for build_info revision."""

    monkeypatch.setattr(relay_module, "resolve_deploy_ref", lambda: "main-resolved")

    assert (
        relay_module._build_revision_label(
            {
                "environment": "prod",
                "version": "0.1.2",
                "label": "prod 0.1.2",
                "ref": "main-830d0a4",
            }
        )
        == "main-830d0a4"
    )
    assert (
        relay_module._build_revision_label(
            {
                "environment": "prod",
                "version": "0.1.2",
                "label": "prod 0.1.2",
            }
        )
        == "main-resolved"
    )


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


def test_late_response_rechecks_terminal_state_before_queueing(relay_client, monkeypatch) -> None:
    """A cancellation that wins during response acceptance prevents queueing a late response."""

    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-late-cancel")
    poll = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-key"})
    assert poll.status_code == 200

    original_clear_pending = relay_module._clear_pending_request

    def terminalize_during_acceptance(client_public_key: str, request_id: str) -> bool:
        cleared = original_clear_pending(client_public_key, request_id)
        relay_module._mark_request_terminal(
            client_public_key,
            request_id,
            status="cancelled",
            reason="requester_cancelled",
        )
        return cleared

    monkeypatch.setattr(relay_module, "_clear_pending_request", terminalize_during_acceptance)

    response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-key",
            "request_id": "request-late-cancel",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )

    assert response.status_code == 410
    assert response.get_json()["error"]["status"] == "cancelled"
    assert "client-key" not in client_responses


def test_response_acceptance_serializes_cancellation_before_queueing(relay_client, monkeypatch) -> None:
    """A cancellation between duplicate checks and lifecycle ownership cannot leave an orphan response."""

    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-atomic-cancel")
    poll = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-key"})
    assert poll.status_code == 200

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

    original_has_response = relay_module._has_client_response_for_request
    cancelled = False

    def cancel_during_acceptance(client_public_key: str, request_id: str) -> bool:
        nonlocal cancelled
        has_response = original_has_response(client_public_key, request_id)
        if not cancelled:
            cancelled = True
            relay_module._cancel_api_v1_request(
                client_public_key,
                request_id,
                status="cancelled",
                reason="requester_cancelled",
            )
        return has_response

    monkeypatch.setattr(relay_module, "_has_client_response_for_request", cancel_during_acceptance)

    response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-key",
            "request_id": "request-atomic-cancel",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )

    assert response.status_code == 410
    assert response.get_json()["error"]["status"] == "cancelled"
    assert "client-key" not in client_responses
    after = _metric_body(relay_client)
    assert (
        _metric_value(after, 'tokenplace_relay_request_outcomes_total', '{outcome="completed"}')
        == before_completed
    )
    assert (
        _metric_value(after, 'tokenplace_relay_request_outcomes_total', '{outcome="cancelled"}')
        == before_cancelled + 1
    )


def test_stale_cancellation_does_not_delete_accepted_response(relay_client) -> None:
    """Cancellation selected before locking must not terminalize after response acceptance wins."""

    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-response-wins")
    poll = relay_client.post("/api/v1/relay/servers/poll", json={"server_public_key": "server-key"})
    assert poll.status_code == 200

    before = _metric_body(relay_client)
    before_completed = _metric_value(
        before,
        'tokenplace_relay_request_outcomes_total',
        '{outcome="completed"}',
    )
    before_expired = _metric_value(
        before,
        'tokenplace_relay_request_outcomes_total',
        '{outcome="expired"}',
    )

    response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-key",
            "request_id": "request-response-wins",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )
    assert response.status_code == 200

    removed = relay_module._cancel_api_v1_request(
        "client-key",
        "request-response-wins",
        status="expired",
        reason="server_unregistered",
    )

    assert removed == 0
    assert relay_module._get_terminal_request("client-key", "request-response-wins") is None
    retrieve = relay_client.post("/api/v1/relay/responses/retrieve", json={"client_public_key": "client-key"})
    assert retrieve.status_code == 200
    assert retrieve.get_json()["request_id"] == "request-response-wins"

    after = _metric_body(relay_client)
    assert (
        _metric_value(after, 'tokenplace_relay_request_outcomes_total', '{outcome="completed"}')
        == before_completed + 1
    )
    assert (
        _metric_value(after, 'tokenplace_relay_request_outcomes_total', '{outcome="expired"}')
        == before_expired
    )


def test_failed_http_responses_are_not_counted_as_completed(relay_client, monkeypatch) -> None:
    """Metrics auth failures should be failed HTTP outcomes, not completed traffic."""

    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "metrics-secret")
    before = _metric_body(relay_client, headers={"Authorization": "Bearer metrics-secret"})
    before_failed = 0.0
    try:
        before_failed = _metric_value(
            before,
            'tokenplace_http_requests_total',
            '{method="GET",outcome="failed",provider_mode="relay",route="/metrics",status_class="4xx"}',
        )
    except AssertionError:
        pass
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


def _outcome_value(client, outcome: str) -> float:
    return _metric_value(
        _metric_body(client),
        "tokenplace_relay_request_outcomes_total",
        f'{{outcome="{outcome}"}}',
    )


def test_canonical_http_method_label_is_bounded(relay_client) -> None:
    """Canonical HTTP metrics collapse caller-controlled methods into a fixed enum."""

    before = _metric_body(relay_client)
    try:
        before_other = _metric_value(
            before,
            "tokenplace_http_requests_total",
            '{method="other",outcome="failed",provider_mode="relay",route="other",status_class="4xx"}',
        )
    except AssertionError:
        before_other = 0.0

    for idx in range(20):
        response = relay_client.open(f"/method-sensitive-{idx}", method=f"CUSTOM{idx}")
        assert response.status_code == 404

    body = _metric_body(relay_client)
    assert (
        _metric_value(
            body,
            "tokenplace_http_requests_total",
            '{method="other",outcome="failed",provider_mode="relay",route="other",status_class="4xx"}',
        )
        == before_other + 20
    )
    canonical_method_lines = [
        line
        for line in body.splitlines()
        if line.startswith("tokenplace_http_requests_total{")
    ]
    for idx in range(20):
        assert f"CUSTOM{idx}" not in "\n".join(canonical_method_lines)


def test_unknown_terminal_attempts_do_not_increment_outcomes(relay_client) -> None:
    """Orphan cancellation and late/orphan responses are ignored for terminal outcomes."""

    before = {outcome: _outcome_value(relay_client, outcome) for outcome in ("completed", "cancelled")}

    relay_module._cancel_api_v1_request(
        "client-missing",
        "request-missing",
        status="cancelled",
        reason="requester_cancelled",
    )
    orphan_response = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-missing",
            "request_id": "request-missing",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )

    assert orphan_response.status_code == 200
    assert _outcome_value(relay_client, "cancelled") == before["cancelled"]
    assert _outcome_value(relay_client, "completed") == before["completed"]


def test_node_stale_registered_then_evicted_and_expired_in_flight_excluded(relay_client) -> None:
    """Gauge collection reports stale registered nodes and skips expired in-flight entries without mutation."""

    before_evictions = _metric_value(
        _metric_body(relay_client),
        "tokenplace_compute_node_evictions_total",
        '{reason="stale_lease"}',
    )
    _register_node(relay_client)
    stale_payload = known_servers["server-key"]
    stale_payload["last_ping"] = datetime.now() - timedelta(seconds=120)
    stale_payload["api_v1_in_flight_requests"] = {
        "expired-in-flight": {
            "client_public_key": "client-key",
            "started_at_monotonic": time.monotonic() - 10,
            "expires_at": time.monotonic() - 1,
        }
    }

    body = _metric_body(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0
    assert _metric_value(body, "tokenplace_relay_in_flight_requests") == 0
    assert "expired-in-flight" in stale_payload["api_v1_in_flight_requests"]

    relay_client.get("/healthz")
    body = _metric_body(relay_client)
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 0
    assert (
        _metric_value(body, "tokenplace_compute_node_evictions_total", '{reason="stale_lease"}')
        == before_evictions + 1
    )


def test_stale_eviction_uses_terminal_lock_before_server_lock(relay_client, monkeypatch) -> None:
    """Stale eviction must not acquire the terminal transition lock while holding the server lock."""

    class TrackingLock:
        def __init__(self, lock):
            self._lock = lock
            self.depth = 0
            self.max_depth = 0

        def acquire(self, *args, **kwargs):
            acquired = self._lock.acquire(*args, **kwargs)
            if acquired:
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)
            return acquired

        def release(self):
            self.depth -= 1
            self._lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self.release()
            return False

    class TerminalTrackingLock(TrackingLock):
        def __init__(self, lock, server_lock):
            super().__init__(lock)
            self.server_lock = server_lock
            self.acquired_while_server_lock_held = False

        def acquire(self, *args, **kwargs):
            if self.server_lock.depth and not self.depth:
                self.acquired_while_server_lock_held = True
            return super().acquire(*args, **kwargs)

    server_lock = TrackingLock(relay_module.server_round_robin_lock)
    terminal_lock = TerminalTrackingLock(relay_module.api_v1_terminal_transition_lock, server_lock)
    monkeypatch.setattr(relay_module, "server_round_robin_lock", server_lock)
    monkeypatch.setattr(relay_module, "api_v1_terminal_transition_lock", terminal_lock)

    before = {
        "cancelled": _outcome_value(relay_client, "cancelled"),
        "completed": _outcome_value(relay_client, "completed"),
    }
    _register_node(relay_client)
    _queue_request(relay_client, request_id="request-stale-eviction-lock-order")
    known_servers["server-key"]["last_ping"] = datetime.now() - timedelta(seconds=120)

    evicted = relay_module._evict_stale_servers()

    assert evicted == ["server-key"]
    assert terminal_lock.acquired_while_server_lock_held is False
    assert "server-key" not in known_servers
    assert "server-key" not in client_inference_requests
    assert "client-key" not in client_pending_request_ids
    assert "client-key" not in client_responses
    assert (
        relay_module._get_terminal_request(
            "client-key",
            "request-stale-eviction-lock-order",
        )["status"]
        == "cancelled"
    )
    assert _outcome_value(relay_client, "cancelled") == before["cancelled"] + 1
    assert _outcome_value(relay_client, "completed") == before["completed"]


def test_metrics_failure_logs_do_not_expose_raw_exception_values(relay_client, monkeypatch) -> None:
    """Metrics failure paths log fixed reasons without exception text or tracebacks."""

    secret = "secret-bearing-exception-value"

    def fail_update_runtime_gauges() -> None:
        raise RuntimeError(secret)

    log_records = []

    def spy_error(message, *args, **kwargs):
        log_records.append({"message": message, **kwargs.get("extra", {})})

    monkeypatch.setattr(relay_module, "_update_runtime_gauges", fail_update_runtime_gauges)
    monkeypatch.setattr(relay_module.LOGGER, "error", spy_error)

    response = relay_client.get("/metrics")

    assert response.status_code == 503
    serialized_logs = "\n".join(json.dumps(record, default=str) for record in log_records)
    assert "metrics.gauge_update_failed" in serialized_logs
    assert secret not in serialized_logs
    assert secret not in response.get_data(as_text=True)


def test_collector_construction_failure_keeps_instrumentation_down(monkeypatch) -> None:
    """Collector construction failures use no-op metrics and do not mark instrumentation healthy."""

    secret = "collector-secret-value"

    def fail_factory():
        raise RuntimeError(secret)

    log_records = []

    def spy_error(message, *args, **kwargs):
        log_records.append({"message": message, **kwargs.get("extra", {})})

    monkeypatch.setattr(relay_module, "_METRICS_CONSTRUCTION_FAILED", False)
    monkeypatch.setattr(relay_module.LOGGER, "error", spy_error)

    metric = relay_module._collector("tokenplace_test_failure_metric", fail_factory)
    metric.labels("x").inc()

    assert relay_module._METRICS_CONSTRUCTION_FAILED is True
    serialized_logs = "\n".join(json.dumps(record, default=str) for record in log_records)
    assert "metrics.collector_construction_failed" in serialized_logs
    assert secret not in serialized_logs

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


def test_metrics_scrape_uses_bounded_relay_registry(relay_client) -> None:
    """The relay scrape should not expose exporter defaults grouped by raw path."""

    for idx in range(30):
        relay_client.get(f"/unmatched-sensitive-{idx}?token=query-sensitive-{idx}")

    body = _metric_body(relay_client)

    assert "flask_http_request" not in body
    assert "prometheus_flask_exporter" not in body
    for idx in range(30):
        assert f"unmatched-sensitive-{idx}" not in body
        assert f"query-sensitive-{idx}" not in body


def test_metrics_gauge_collection_auth_order_and_once_per_authorized_scrape(
    relay_client,
    monkeypatch,
) -> None:
    """Unauthorized scrapes should not collect gauges; authorized scrapes collect once."""

    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", "metrics-secret")
    calls = {"count": 0}

    def spy_update_runtime_gauges() -> None:
        calls["count"] += 1

    monkeypatch.setattr(relay_module, "_update_runtime_gauges", spy_update_runtime_gauges)

    unauthorized = relay_client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert unauthorized.status_code == 401
    assert calls["count"] == 0

    authorized = relay_client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
    assert authorized.status_code == 200
    assert calls["count"] == 1


def test_metrics_gauge_collection_failure_returns_503(relay_client, monkeypatch) -> None:
    """Scrape collection failures should fail metrics without blocking relay traffic."""

    def fail_update_runtime_gauges() -> None:
        raise RuntimeError("synthetic scrape failure")

    monkeypatch.setattr(relay_module, "_update_runtime_gauges", fail_update_runtime_gauges)

    response = relay_client.get("/metrics")

    assert response.status_code == 503
    assert relay_client.get("/healthz").status_code == 200


def test_structured_logs_keep_compat_http_path_normalized(relay_client, monkeypatch) -> None:
    """Desktop-compatible http_path should carry only the normalized route group."""

    log_records = []

    def spy_info(message, *args, **kwargs):
        log_records.append((message, kwargs.get("extra", {})))

    monkeypatch.setattr(relay_module.LOGGER, "info", spy_info)
    raw_values = {"raw-request-id-next", "agent-next-sensitive", "query-next-sensitive"}

    response = relay_client.get(
        "/api/v1/relay/servers/next?request_id=query-next-sensitive",
        headers={
            "X-Request-Id": "raw-request-id-next",
            "User-Agent": "agent-next-sensitive",
        },
    )

    assert response.status_code in {200, 404, 503}
    http_logs = [extra for message, extra in log_records if message == "http.request"]
    assert http_logs
    assert http_logs[-1]["http_path"] == "/api/v1/relay/servers/next"
    assert http_logs[-1]["http_route"] == "/api/v1/relay/servers/next"
    serialized_logs = "\n".join(json.dumps(payload, default=str) for payload in http_logs)
    for value in raw_values:
        assert value not in serialized_logs
