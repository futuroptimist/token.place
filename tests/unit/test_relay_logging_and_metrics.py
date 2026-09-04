"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
import io
import logging
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

import relay
from relay import JsonFormatter, app, known_servers


@pytest.fixture()
def relay_client():
    """Provide a clean relay Flask test client."""

    app.config["TESTING"] = True
    state_stores = (
        known_servers,
        relay.client_inference_requests,
        relay.client_responses,
        relay.client_pending_request_ids,
        relay.client_terminal_request_ids,
        relay.client_terminal_outcomes,
        relay.streaming_sessions,
        relay.streaming_sessions_by_client,
        relay.api_v1_recently_unregistered_servers,
    )
    for store in state_stores:
        store.clear()
    with app.test_client() as client:
        yield client
    for store in state_stores:
        store.clear()


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
    record.request_id = "sensitive-request-id"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "processed chat request"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tokenplace.relay"
    assert "request_id" not in payload
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_bounds_exception_records() -> None:
    """Exception records must not interpolate messages or serialize tracebacks."""

    try:
        raise RuntimeError("private-exception-marker")
    except RuntimeError:
        record = logging.LogRecord(
            name="flask.app",
            level=logging.ERROR,
            pathname=__file__,
            lineno=42,
            msg="Exception on %s",
            args=("/private-path-marker?secret=private-query-marker",),
            exc_info=sys.exc_info(),
        )

    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)

    assert payload["message"] == "relay.exception"
    assert payload["reason"] == "exception"
    assert "private-path-marker" not in formatted
    assert "private-query-marker" not in formatted
    assert "private-exception-marker" not in formatted


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
        "tokenplace_http_requests_total",
        "tokenplace_http_request_duration_seconds",
        "tokenplace_relay_queue_depth",
        "tokenplace_relay_oldest_queued_request_age_seconds",
        "tokenplace_compute_node_lease_age_seconds",
        "tokenplace_compute_node_evictions_total",
        "tokenplace_relay_in_flight_requests",
        "tokenplace_relay_oldest_in_flight_age_seconds",
        "tokenplace_relay_request_outcomes_total",
    ):
        assert metric in body
    assert _metric_value(body, "tokenplace_instrumentation_up") == 1
    assert _metric_value(body, "tokenplace_compute_nodes_registered") == 0
    assert _metric_value(body, "tokenplace_compute_nodes_healthy") == 0


def test_entrypoint_clears_only_dedicated_metrics_directory_on_each_restart(tmp_path) -> None:
    """Container restarts cannot inherit stale shards from the pod-lifetime directory."""

    metrics_dir = tmp_path / "tokenplace-prometheus-multiproc"
    sibling = tmp_path / "keep-me"
    sibling.write_text("outside", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gunicorn = fake_bin / "gunicorn"
    invocation = tmp_path / "gunicorn-arguments"
    gunicorn.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{invocation}"\n', encoding="utf-8"
    )
    gunicorn.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "PROMETHEUS_MULTIPROC_DIR": str(metrics_dir),
        "RELAY_WORKER_TMP_DIR": str(tmp_path / "worker-tmp"),
    })
    entrypoint = Path(__file__).parents[2] / "docker/relay/entrypoint.sh"

    for stale_name in ("counter_11.db", "gauge_liveall_22.db"):
        metrics_dir.mkdir(exist_ok=True)
        (metrics_dir / stale_name).write_text("stale", encoding="utf-8")
        subprocess.run([str(entrypoint)], env=env, check=True, capture_output=True, text=True)
        assert metrics_dir.is_dir()
        assert list(metrics_dir.iterdir()) == []
        assert sibling.read_text(encoding="utf-8") == "outside"

    arguments = invocation.read_text(encoding="utf-8").splitlines()
    assert "--access-logfile" not in arguments
    assert arguments[arguments.index("--error-logfile") + 1] == "-"


def test_entrypoint_fails_closed_for_unsafe_metrics_targets(tmp_path) -> None:
    entrypoint = Path(__file__).parents[2] / "docker/relay/entrypoint.sh"
    env = os.environ.copy()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gunicorn = fake_bin / "gunicorn"
    invocation = tmp_path / "gunicorn-invoked"
    gunicorn.write_text(f'#!/bin/sh\ntouch "{invocation}"\nexit 99\n', encoding="utf-8")
    gunicorn.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    for target in ("", "/tmp", "relative/tokenplace-prometheus-multiproc"):
        env["PROMETHEUS_MULTIPROC_DIR"] = target
        result = subprocess.run([str(entrypoint)], env=env, capture_output=True, text=True)
        assert result.returncode == 1

    real_target = tmp_path / "real"
    real_target.mkdir()
    symlink_target = tmp_path / "tokenplace-prometheus-multiproc"
    symlink_target.symlink_to(real_target, target_is_directory=True)
    env["PROMETHEUS_MULTIPROC_DIR"] = str(symlink_target)
    result = subprocess.run([str(entrypoint)], env=env, capture_output=True, text=True)
    assert result.returncode == 1

    traversal_parent = tmp_path / "traversal"
    traversal_parent.mkdir()
    (traversal_parent / "child").mkdir()
    traversal_target = traversal_parent / "tokenplace-prometheus-multiproc"
    traversal_target.mkdir()
    traversal_sentinel = traversal_target / "keep"
    traversal_sentinel.write_text("untouched", encoding="utf-8")
    env["PROMETHEUS_MULTIPROC_DIR"] = str(
        traversal_parent / "child" / ".." / "tokenplace-prometheus-multiproc"
    )
    result = subprocess.run([str(entrypoint)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert traversal_sentinel.read_text(encoding="utf-8") == "untouched"

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    ancestor_target = real_parent / "tokenplace-prometheus-multiproc"
    ancestor_target.mkdir()
    ancestor_sentinel = ancestor_target / "keep"
    ancestor_sentinel.write_text("untouched", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    env["PROMETHEUS_MULTIPROC_DIR"] = str(
        linked_parent / "tokenplace-prometheus-multiproc"
    )
    result = subprocess.run([str(entrypoint)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert ancestor_sentinel.read_text(encoding="utf-8") == "untouched"

    file_target = tmp_path / "file-parent" / "tokenplace-prometheus-multiproc"
    file_target.parent.mkdir()
    file_target.write_text("untouched", encoding="utf-8")
    env["PROMETHEUS_MULTIPROC_DIR"] = str(file_target)
    result = subprocess.run([str(entrypoint)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert file_target.read_text(encoding="utf-8") == "untouched"
    assert not invocation.exists()


def test_gunicorn_child_exit_marks_worker_dead(monkeypatch) -> None:
    import importlib.util

    config_path = Path(__file__).parents[2] / "docker/relay/gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("relay_gunicorn_config", config_path)
    assert spec is not None and spec.loader is not None
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    seen = []
    monkeypatch.setattr(config.multiprocess, "mark_process_dead", seen.append)

    config.child_exit(None, type("Worker", (), {"pid": 4242})())

    assert seen == [4242]


def test_gunicorn_error_log_filter_bounds_request_records() -> None:
    """Gunicorn request errors must reach handlers without request-controlled data."""

    import importlib.util

    config_path = Path(__file__).parents[2] / "docker/relay/gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("relay_gunicorn_privacy_config", config_path)
    assert spec is not None and spec.loader is not None
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)

    error_log = logging.Logger("gunicorn.error.test")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    error_log.addHandler(handler)
    server = type("Server", (), {"error_log": error_log})()

    config.on_starting(server)
    config.on_starting(server)
    assert len(error_log.filters) == 1

    error_log.warning(
        "Invalid request from ip=%s: %s",
        "private-source-address-marker",
        "private-parser-exception-marker",
    )
    error_log.info("Worker lifecycle event %s", "preserved")
    try:
        raise RuntimeError("private-traceback-marker")
    except RuntimeError:
        error_log.exception(
            "Error handling request %s",
            "/private-uri-marker?secret=private-query-marker",
        )

    formatted = output.getvalue()
    assert "gunicorn.invalid_request" in formatted
    assert "gunicorn.request_error" in formatted
    assert "Worker lifecycle event preserved" in formatted
    for marker in (
        "private-source-address-marker",
        "private-parser-exception-marker",
        "private-uri-marker",
        "private-query-marker",
        "private-traceback-marker",
        "Traceback",
    ):
        assert marker not in formatted


def test_gunicorn_error_log_filter_is_installed_after_fork() -> None:
    """The privacy filter must also be present on a worker-created logger."""

    import importlib.util

    config_path = Path(__file__).parents[2] / "docker/relay/gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("relay_gunicorn_worker_config", config_path)
    assert spec is not None and spec.loader is not None
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    master_log = logging.Logger("gunicorn.error.master")
    worker_log = logging.Logger("gunicorn.error.worker")
    server = type("Server", (), {"error_log": master_log})()
    worker = type("Worker", (), {"error_log": worker_log})()

    config.post_fork(server, worker)

    assert len(master_log.filters) == 1
    assert len(worker_log.filters) == 1


@pytest.mark.parametrize("image_tag", [None, ""])
def test_build_info_labels_fall_back_to_release_metadata(
    monkeypatch, image_tag
) -> None:
    if image_tag is None:
        monkeypatch.delenv("TOKENPLACE_IMAGE_TAG", raising=False)
    else:
        monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", image_tag)
    monkeypatch.setattr(
        relay,
        "get_release_metadata",
        lambda _root: {"version": "0.1.1", "ref": "sha-release"},
    )

    assert relay._get_build_info_labels() == ("0.1.1", "sha-release")


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


def test_multiprocess_workers_aggregate_and_gauges_have_no_pid(tmp_path) -> None:
    """Two real worker PIDs contribute to one dedicated aggregate exposition."""

    metrics_dir = tmp_path / "multiprocess"
    metrics_dir.mkdir()
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(metrics_dir)
    worker = """
import relay
relay.HTTP_REQUESTS_TOTAL.labels('POST', '/api/v1/relay/requests', '2xx', 'relay', 'completed').inc()
relay.HTTP_REQUEST_DURATION_SECONDS.labels('POST', '/api/v1/relay/requests', '2xx', 'relay', 'completed').observe(0.25)
relay.RELAY_QUEUE_DEPTH.labels('relay').set(3)
"""
    for _ in range(2):
        subprocess.run([sys.executable, "-c", worker], env=env, check=True, capture_output=True)

    scrape = """
import json
import relay
from prometheus_client import generate_latest
print(json.dumps({'body': generate_latest(relay.RELAY_METRICS_REGISTRY).decode()}))
"""
    result = subprocess.run(
        [sys.executable, "-c", scrape], env=env, check=True, capture_output=True, text=True
    )
    body = json.loads(result.stdout.splitlines()[-1])["body"]
    samples = _samples(body)
    counter = [sample for sample in samples if sample.name == "tokenplace_http_requests_total"]
    histogram_count = [
        sample for sample in samples
        if sample.name == "tokenplace_http_request_duration_seconds_count"
    ]
    queue = [sample for sample in samples if sample.name == "tokenplace_relay_queue_depth"]
    assert len(counter) == len(histogram_count) == len(queue) == 1
    assert counter[0].value == histogram_count[0].value == 2
    assert all("pid" not in sample.labels for sample in samples)


def test_relay_gauges_use_bounded_multiprocess_mode() -> None:
    for gauge in (
        relay.RELAY_QUEUE_DEPTH,
        relay.RELAY_OLDEST_QUEUED_REQUEST_AGE_SECONDS,
        relay.COMPUTE_NODES_REGISTERED,
        relay.COMPUTE_NODES_HEALTHY,
        relay.COMPUTE_NODE_LEASE_AGE_SECONDS,
        relay.RELAY_IN_FLIGHT_REQUESTS,
        relay.RELAY_OLDEST_IN_FLIGHT_AGE_SECONDS,
        relay.BUILD_INFO,
        relay.INSTRUMENTATION_UP,
    ):
        assert gauge._multiprocess_mode == "livemostrecent"


def _outcome_value(client, outcome: str) -> float:
    return _metric_value(
        _scrape(client),
        f'tokenplace_relay_request_outcomes_total{{outcome="{outcome}"}}',
    )


def _relay_envelope(*, server_key: str, client_key: str, request_id: str, cancel_token=None):
    envelope = {
        "request_id": request_id,
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": client_key,
        "server_public_key": server_key,
        "chat_history": "encrypted-request",
        "cipherkey": "encrypted-key",
        "iv": "encrypted-iv",
    }
    if cancel_token is not None:
        envelope["cancel_token"] = cancel_token
    return envelope


def test_queue_dispatch_completion_and_terminal_metrics_update_once(relay_client) -> None:
    server_key = "metric-node"
    client_key = "metric-client"
    _register_api_v1_node(relay_client, server_key)
    envelope = _relay_envelope(
        server_key=server_key, client_key=client_key, request_id="metric-complete"
    )
    completed_before = _outcome_value(relay_client, "completed")
    assert relay_client.post("/api/v1/relay/requests", json=envelope).status_code == 200
    queued = _scrape(relay_client)
    assert _metric_value(queued, 'tokenplace_relay_queue_depth{provider_mode="relay"}') == 1

    assert relay_client.post(
        "/api/v1/relay/servers/poll", json={"server_public_key": server_key}
    ).status_code == 200
    dispatched = _scrape(relay_client)
    assert _metric_value(dispatched, 'tokenplace_relay_queue_depth{provider_mode="relay"}') == 0
    assert _metric_value(dispatched, "tokenplace_relay_in_flight_requests") == 1

    response = dict(envelope)
    response.pop("server_public_key")
    response["chat_history"] = "encrypted-response"
    assert relay_client.post("/api/v1/relay/responses", json=response).status_code == 200
    complete = _scrape(relay_client)
    assert _metric_value(complete, "tokenplace_relay_in_flight_requests") == 0
    assert _outcome_value(relay_client, "completed") == completed_before + 1

    # A duplicate provider submission is accepted idempotently, but cannot claim
    # the lifecycle or increment the terminal outcome again.
    assert relay_client.post("/api/v1/relay/responses", json=response).status_code == 200
    assert _outcome_value(relay_client, "completed") == completed_before + 1


def test_cancel_route_records_cancelled_outcome_once(relay_client) -> None:
    server_key = "cancel-node"
    client_key = "cancel-client"
    request_id = "cancel-request"
    cancel_token = "cancel-proof"
    _register_api_v1_node(relay_client, server_key)
    envelope = _relay_envelope(
        server_key=server_key,
        client_key=client_key,
        request_id=request_id,
        cancel_token=cancel_token,
    )
    assert relay_client.post("/api/v1/relay/requests", json=envelope).status_code == 200
    before = _outcome_value(relay_client, "cancelled")
    cancellation = {
        "client_public_key": client_key,
        "request_id": request_id,
        "cancel_token": cancel_token,
        "status": "cancelled",
        "reason": "requester_cancelled",
    }

    assert relay_client.post("/api/v1/relay/requests/cancel", json=cancellation).status_code == 200
    assert relay_client.post("/api/v1/relay/requests/cancel", json=cancellation).status_code == 403
    assert _outcome_value(relay_client, "cancelled") == before + 1


def test_response_submission_records_expired_pending_request_once(relay_client) -> None:
    server_key = "expiry-node"
    client_key = "expiry-client"
    request_id = "expiry-request"
    _register_api_v1_node(relay_client, server_key)
    envelope = _relay_envelope(
        server_key=server_key, client_key=client_key, request_id=request_id
    )
    assert relay_client.post("/api/v1/relay/requests", json=envelope).status_code == 200
    relay.client_pending_request_ids[client_key][request_id] = (
        relay.time.time() - relay.PENDING_REQUEST_TTL_SECONDS - 1
    )
    response = dict(envelope)
    response.pop("server_public_key")
    response["chat_history"] = "encrypted-response"
    before = _outcome_value(relay_client, "expired")

    assert relay_client.post("/api/v1/relay/responses", json=response).status_code == 410
    assert relay_client.post("/api/v1/relay/responses", json=response).status_code == 410
    assert _outcome_value(relay_client, "expired") == before + 1


def test_poll_server_removal_records_provider_timeout_once(relay_client, monkeypatch) -> None:
    server_key = "timeout-node"
    client_key = "timeout-client"
    request_id = "timeout-request"
    _register_api_v1_node(relay_client, server_key)
    envelope = _relay_envelope(
        server_key=server_key, client_key=client_key, request_id=request_id
    )
    assert relay_client.post("/api/v1/relay/requests", json=envelope).status_code == 200
    original_pop = relay._pop_next_api_v1_request

    def pop_then_remove_server(public_key):
        claimed = original_pop(public_key)
        known_servers.pop(public_key, None)
        return claimed

    monkeypatch.setattr(relay, "_pop_next_api_v1_request", pop_then_remove_server)
    before = _outcome_value(relay_client, "timed_out")

    assert relay_client.post(
        "/api/v1/relay/servers/poll", json={"server_public_key": server_key}
    ).status_code == 404
    assert relay_client.post(
        "/api/v1/relay/servers/poll", json={"server_public_key": server_key}
    ).status_code == 404
    assert _outcome_value(relay_client, "timed_out") == before + 1


def test_request_route_429_records_rate_limited_outcome(relay_client, monkeypatch) -> None:
    before = _outcome_value(relay_client, "rate_limited")
    monkeypatch.setitem(
        app.view_functions,
        "api_v1_relay_requests",
        lambda: ({"error": {"message": "limited", "code": 429}}, 429),
    )

    response = relay_client.post(
        "/api/v1/relay/requests",
        json={"synthetic": "payload"},
        environ_base={"REMOTE_ADDR": "192.0.2.55"},
    )
    assert response.status_code == 429
    assert _outcome_value(relay_client, "rate_limited") == before + 1


def test_public_route_records_stale_eviction_once(relay_client) -> None:
    server_key = "stale-node"
    _register_api_v1_node(relay_client, server_key)
    known_servers[server_key]["last_ping"] = datetime.now() - timedelta(days=1)
    before = _metric_value(
        _scrape(relay_client),
        'tokenplace_compute_node_evictions_total{reason="stale_lease"}',
    )

    assert relay_client.get("/healthz").status_code == 200
    assert relay_client.get("/healthz").status_code == 200
    assert _metric_value(
        _scrape(relay_client),
        'tokenplace_compute_node_evictions_total{reason="stale_lease"}',
    ) == before + 1


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


BOUNDED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "other"}
BOUNDED_STATUS_CLASSES = {"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"}
BOUNDED_PROVIDER_MODES = set(relay.PROVIDER_MODE_ENUM)
BOUNDED_OUTCOMES = set(relay.OUTCOME_ENUM)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (99, "unknown"), (100, "1xx"), (599, "5xx"), (600, "unknown"),
        (-200, "unknown"), ("not-a-status", "unknown"),
    ],
)
def test_status_class_is_strictly_bounded(value, expected) -> None:
    assert relay._normalise_status_class(value) == expected


@pytest.mark.parametrize("value", ["TRACE", "BREW", "CUSTOM-ATTACK", None])
def test_nonstandard_methods_are_bounded(value) -> None:
    assert relay._normalise_http_method(value) == "other"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(99, "unknown"), (100, "100"), (599, "599"), (600, "unknown"), ("bad", "unknown")],
)
def test_legacy_status_is_exact_but_bounded(value, expected) -> None:
    assert relay._normalise_legacy_status(value) == expected


def test_legacy_endpoint_preserves_known_names_and_bounds_unmatched_values() -> None:
    assert (
        relay._normalise_legacy_endpoint("api_v1_relay_servers_register")
        == "api_v1_relay_servers_register"
    )
    assert relay._normalise_legacy_endpoint("attacker-selected-endpoint") == "unknown"


def _samples(body: str):
    return [
        sample
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    ]


def test_thousands_of_unmatched_paths_have_bounded_cardinality_and_exposition(relay_client) -> None:
    """Attacker-selected 404 paths collapse to one finite route class."""

    raw_values = []
    for index in range(2_000):
        marker = f"unmatched-sensitive-{index}"
        raw_values.append(marker)
        response = relay_client.get(f"/{marker}?credential=query-{index}")
        assert response.status_code == 404

    body = _scrape(relay_client)
    samples = _samples(body)
    request_samples = [s for s in samples if s.name == "tokenplace_http_requests_total"]
    unmatched = [s for s in request_samples if s.labels.get("route") == "other"]
    assert unmatched
    assert {s.labels["route"] for s in unmatched} == {"other"}
    assert all(marker not in body for marker in raw_values)
    assert "credential=query-" not in body
    assert "flask_http_request" not in body
    # Explicit deterministic budgets guard against series and payload regressions.
    # Includes the preserved legacy request-counter contract alongside the
    # bounded metric families accumulated by the full unit-test process.
    assert len(samples) <= 725
    assert len(body.encode("utf-8")) <= 107_000


def test_metric_names_and_label_domains_are_finite(relay_client) -> None:
    for index in range(20):
        response = relay_client.open(f"/custom-{index}", method=f"CUSTOM{index}")
        assert response.status_code == 404

    body = _scrape(relay_client)
    allowed_routes = {
        "/api/v1/relay/requests", "/api/v1/relay/requests/cancel",
        "/api/v1/relay/responses", "/api/v1/relay/responses/retrieve",
        "/api/v1/relay/servers/register", "/api/v1/relay/servers/unregister",
        "/api/v1/relay/servers/poll", "/api/v1/relay/servers/next",
        "/api/v1/*", "/api/v2/*", "/healthz", "/livez",
        "/relay/diagnostics", "other",
    }
    allowed_endpoints = set(relay.LEGACY_ENDPOINT_ENUM) | {"unknown"}
    for sample in _samples(body):
        labels = sample.labels
        if "method" in labels:
            assert labels["method"] in BOUNDED_METHODS
        if "route" in labels:
            assert labels["route"] in allowed_routes
        if "endpoint" in labels:
            assert labels["endpoint"] in allowed_endpoints
        if "status_class" in labels:
            assert labels["status_class"] in BOUNDED_STATUS_CLASSES
        if "status" in labels:
            assert labels["status"] == "unknown" or (
                labels["status"].isdigit() and 100 <= int(labels["status"]) <= 599
            )
        if "provider_mode" in labels:
            assert labels["provider_mode"] in BOUNDED_PROVIDER_MODES
        if "outcome" in labels:
            assert labels["outcome"] in BOUNDED_OUTCOMES
        if "reason" in labels:
            assert labels["reason"] in set(relay.EVICTION_REASON_ENUM)


def test_only_explicit_relay_metric_families_are_exposed(relay_client) -> None:
    allowed = {
        "tokenplace_build_info",
        "tokenplace_compute_node_evictions",
        "tokenplace_compute_node_evictions_created",
        "tokenplace_compute_node_lease_age_seconds",
        "tokenplace_compute_nodes_healthy",
        "tokenplace_compute_nodes_registered",
        "tokenplace_http_request_duration_seconds",
        "tokenplace_http_request_duration_seconds_created",
        "tokenplace_http_requests",
        "tokenplace_http_requests_created",
        "tokenplace_instrumentation_up",
        "tokenplace_relay_in_flight_requests",
        "tokenplace_relay_oldest_in_flight_age_seconds",
        "tokenplace_relay_oldest_queued_request_age_seconds",
        "tokenplace_relay_queue_depth",
        "tokenplace_relay_request_outcomes",
        "tokenplace_relay_request_outcomes_created",
        "tokenplace_relay_requests",
        "tokenplace_relay_requests_created",
    }
    body = _scrape(relay_client)
    families = {family.name for family in text_string_to_metric_families(body)}
    assert families == allowed
    assert "flask_http_request" not in body


def test_sensitive_inputs_and_exceptions_never_reach_observability(relay_client) -> None:
    markers = {
        "path": "private-path-marker",
        "query": "private-query-marker",
        "request": "private-request-id-marker",
        "agent": "private-user-agent-marker",
        "key": "private-public-key-marker",
        "ciphertext": "private-ciphertext-marker",
        "auth": "private-authorization-marker",
        "exception": "private-exception-marker",
    }
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    relay.LOGGER.addHandler(handler)
    try:
        relay_client.get(
            f"/{markers['path']}?secret={markers['query']}&ciphertext={markers['ciphertext']}",
            headers={
                "X-Request-Id": markers["request"],
                "User-Agent": markers["agent"],
                "Authorization": markers["auth"],
            },
        )
        unknown = relay_client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": markers["key"], "request_id": markers["request"]},
        )
        try:
            raise RuntimeError(markers["exception"])
        except RuntimeError:
            relay.LOGGER.error(
                "relay.synthetic_failure",
                exc_info=True,
                extra={"request_id": markers["request"], "ciphertext": markers["ciphertext"]},
            )
    finally:
        relay.LOGGER.removeHandler(handler)

    assert unknown.status_code == 404
    assert unknown.get_json() == {"error": {"message": "Unknown request", "code": 404}}
    observable = "\n".join((stream.getvalue(), _scrape(relay_client), unknown.get_data(as_text=True)))
    for marker in markers.values():
        assert marker not in observable


def test_metrics_scrapes_do_not_instrument_themselves(relay_client) -> None:
    before = _scrape(relay_client)
    for _ in range(5):
        _scrape(relay_client)
    after = _scrape(relay_client)

    for body in (before, after):
        assert 'route="/metrics"' not in body
        assert 'endpoint="/metrics"' not in body
    assert before == after


def test_unauthorized_metrics_does_not_leak_or_instrument_credential(relay_client, monkeypatch) -> None:
    expected = "expected-metrics-secret"
    supplied = "attacker-controlled-credential"
    monkeypatch.setenv("TOKENPLACE_METRICS_TOKEN", expected)

    response = relay_client.get("/metrics", headers={"Authorization": f"Bearer {supplied}"})

    assert response.status_code == 401
    assert expected not in response.get_data(as_text=True)
    assert supplied not in response.get_data(as_text=True)
    body = _scrape(relay_client, headers={"Authorization": f"Bearer {expected}"})
    assert expected not in body
    assert supplied not in body
    assert 'route="/metrics"' not in body
