"""Regression tests for relay logging and metrics endpoints."""

from __future__ import annotations

import json
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


def test_maintenance_gauges_use_single_multiprocess_snapshot(tmp_path) -> None:
    """Multiprocess exposition must not emit per-worker maintenance gauges."""

    multiprocess_dir = tmp_path / "prometheus"
    multiprocess_dir.mkdir()
    token = secrets.token_urlsafe(24)
    env = os.environ.copy()
    env.update(
        {
            "PROMETHEUS_MULTIPROC_DIR": str(multiprocess_dir),
            "TOKENPLACE_IMAGE_TAG": "sha-deadbee",
            "TOKENPLACE_METRICS_TOKEN": token,
        }
    )
    worker = """
import os
import sys

from prometheus_client import Gauge

import relay

default_mode_control = Gauge(
    "default_mode_control",
    "Control gauge using the default multiprocess mode",
)
default_mode_control.set(1)

relay.app.config["TESTING"] = True
with relay.app.test_client() as client:
    registered = client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": "node-a"},
    )
    assert registered.status_code == 200
    assert client.get("/metrics").status_code == 401
    response = client.get(
        "/metrics",
        headers={
            "Authorization": "Bearer " + os.environ["TOKENPLACE_METRICS_TOKEN"]
        },
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert os.environ["TOKENPLACE_METRICS_TOKEN"] not in body
    if sys.argv[1] == "emit":
        sys.stdout.write("\\n__METRICS__\\n" + body)
"""

    # Three independent workers each report one node. The final scrape is emitted
    # by the last worker so its authoritative snapshot remains the most recent.
    results = []
    for mode in ("write", "write"):
        results.append(
            subprocess.run(
                [sys.executable, "-c", worker, mode],
                cwd=Path(__file__).parents[2],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        )
    results.append(
        subprocess.run(
            [sys.executable, "-c", worker, "emit"],
            cwd=Path(__file__).parents[2],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    )
    assert all(token not in result.stdout for result in results)
    assert all(token not in result.stderr for result in results)
    exposed = results[-1].stdout.split("__METRICS__\n", 1)[1]

    names = {
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_compute_nodes_registered",
        "tokenplace_compute_nodes_healthy",
    }
    samples = [
        sample
        for family in text_string_to_metric_families(exposed)
        for sample in family.samples
        if sample.name in names
    ]
    by_name = {
        name: [sample for sample in samples if sample.name == name]
        for name in names
    }
    control_samples = [
        sample
        for family in text_string_to_metric_families(exposed)
        for sample in family.samples
        if sample.name == "default_mode_control"
    ]

    assert len(control_samples) == len(results)
    assert len({sample.labels["pid"] for sample in control_samples}) == len(results)
    assert all(set(sample.labels) == {"pid"} for sample in control_samples)
    assert all(len(metric_samples) == 1 for metric_samples in by_name.values())
    assert all("pid" not in sample.labels for sample in samples)
    assert by_name["tokenplace_build_info"][0].value == 1
    assert by_name["tokenplace_instrumentation_up"][0].value == 1
    assert by_name["tokenplace_compute_nodes_registered"][0].value == 1
    assert by_name["tokenplace_compute_nodes_healthy"][0].value == 1
    assert set(by_name["tokenplace_build_info"][0].labels) == {
        "version",
        "revision",
    }
    assert by_name["tokenplace_build_info"][0].labels == {
        "version": "sha-deadbee",
        "revision": "sha-deadbee",
    }
    for name in names - {"tokenplace_build_info"}:
        assert by_name[name][0].labels == {}


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


def test_repeated_scrapes_are_read_only_for_relay_state(relay_client) -> None:
    _register_api_v1_node(relay_client)
    known_servers["node-a"]["sentinel"] = {"unchanged": True}
    before = {
        key: dict(value) for key, value in known_servers.items()
    }
    request_series = (
        'tokenplace_relay_requests_total{endpoint="/api/v1/relay/servers/register",'
        'method="POST",status="2xx"}'
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


BOUNDED_METRIC_NAMES = {
    "tokenplace_build_info",
    "tokenplace_compute_nodes_healthy",
    "tokenplace_compute_nodes_registered",
    "tokenplace_http_request_duration_seconds",
    "tokenplace_http_requests_total",
    "tokenplace_http_requests",
    "tokenplace_http_requests_created",
    "tokenplace_instrumentation_up",
    "tokenplace_relay_in_flight_requests",
    "tokenplace_relay_queue_depth",
    "tokenplace_relay_request_outcomes_total",
    "tokenplace_relay_request_outcomes",
    "tokenplace_relay_request_outcomes_created",
    "tokenplace_relay_requests_total",
    "tokenplace_relay_requests",
    "tokenplace_relay_requests_created",
    "tokenplace_http_request_duration_seconds_created",
}


def test_thousands_of_unmatched_paths_have_a_deterministic_budget(relay_client, monkeypatch) -> None:
    """Attacker-controlled 404s collapse to one route and bounded exposition."""

    monkeypatch.setattr(relay.LOGGER, "info", lambda *_args, **_kwargs: None)
    for index in range(2_000):
        response = relay_client.get(
            f"/private-raw-path-{index}?credential=query-secret-{index}",
            headers={"User-Agent": f"raw-agent-{index}", "X-Request-Id": f"raw-id-{index}"},
        )
        assert response.status_code == 404

    body = _scrape(relay_client)
    families = list(text_string_to_metric_families(body))
    names = {family.name for family in families}
    samples = [sample for family in families for sample in family.samples]

    assert "flask_http_request" not in body
    assert names <= BOUNDED_METRIC_NAMES
    assert len(samples) <= 500
    assert len(body.encode()) <= 65_536
    assert 'route="unmatched"' in body
    assert 'endpoint="unmatched"' in body
    for index in (0, 1, 999, 1_999):
        assert f"private-raw-path-{index}" not in body
        assert f"query-secret-{index}" not in body
        assert f"raw-id-{index}" not in body
        assert f"raw-agent-{index}" not in body


def test_metrics_scrapes_do_not_instrument_themselves(relay_client) -> None:
    before = _scrape(relay_client)
    for _ in range(5):
        _scrape(relay_client)
    after = _scrape(relay_client)
    assert before == after
    assert 'route="/metrics"' not in after
    assert 'endpoint="/metrics"' not in after


def test_metric_label_domains_are_reviewed_and_finite(relay_client) -> None:
    relay_client.open("/unmatched", method="ATTACKER-METHOD")
    body = _scrape(relay_client)
    samples = [
        sample
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    ]
    domains = {"method": set(), "route": set(), "endpoint": set(), "status_class": set(), "provider_mode": set(), "outcome": set()}
    for sample in samples:
        for label in domains:
            if label in sample.labels:
                domains[label].add(sample.labels[label])
    assert domains["method"] <= relay.METHODS | {"other"}
    assert domains["route"] <= relay.KNOWN_ROUTES | {"unmatched"}
    assert domains["endpoint"] <= relay.KNOWN_ROUTES | {"unmatched"}
    assert domains["status_class"] <= {"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"}
    assert domains["provider_mode"] <= {"relay"}
    assert domains["outcome"] <= relay.OUTCOMES


def test_entrypoint_restart_cleanup_is_exact_and_rejects_unsafe_targets(tmp_path) -> None:
    entrypoint = Path(__file__).parents[2] / "docker/relay/entrypoint.sh"
    metrics_dir = tmp_path / "metrics"
    sibling = tmp_path / "keep"
    metrics_dir.mkdir()
    (metrics_dir / "stale.db").write_text("stale")
    sibling.write_text("keep")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gunicorn = bin_dir / "gunicorn"
    fake_gunicorn.write_text("#!/bin/sh\ntest ! -e \"$PROMETHEUS_MULTIPROC_DIR/stale.db\"\ntouch \"$PROMETHEUS_MULTIPROC_DIR/fresh.db\"\n")
    fake_gunicorn.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "PROMETHEUS_MULTIPROC_DIR": str(metrics_dir)}

    subprocess.run([str(entrypoint)], env=env, check=True)

    assert (metrics_dir / "fresh.db").is_file()
    assert sibling.read_text() == "keep"
    unsafe = subprocess.run(
        [str(entrypoint)],
        env={**env, "PROMETHEUS_MULTIPROC_DIR": "/tmp"},
        capture_output=True,
        text=True,
    )
    assert unsafe.returncode != 0
    assert "must be dedicated" in unsafe.stderr


def test_entrypoint_rejects_metrics_directory_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("keep")
    link = tmp_path / "metrics-link"
    link.symlink_to(target, target_is_directory=True)
    entrypoint = Path(__file__).parents[2] / "docker/relay/entrypoint.sh"
    result = subprocess.run(
        [str(entrypoint)],
        env={**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(link)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert sentinel.read_text() == "keep"


def test_gunicorn_child_exit_marks_worker_dead(monkeypatch) -> None:
    import gunicorn_metrics

    seen = []
    monkeypatch.setattr(gunicorn_metrics.multiprocess, "mark_process_dead", seen.append)
    worker = type("Worker", (), {"pid": 4242})()
    gunicorn_metrics.child_exit(None, worker)
    assert seen == [4242]
