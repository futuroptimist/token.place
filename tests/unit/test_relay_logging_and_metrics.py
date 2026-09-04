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


BOUNDED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "other"}
BOUNDED_ROUTES = {
    "/api/v1/relay/requests",
    "/api/v1/relay/requests/cancel",
    "/api/v1/relay/responses",
    "/api/v1/relay/responses/retrieve",
    "/api/v1/relay/servers/register",
    "/api/v1/relay/servers/unregister",
    "/api/v1/relay/servers/poll",
    "/api/v1/relay/servers/next",
    "/healthz",
    "/livez",
    "/relay/diagnostics",
    "/api/v1/*",
    "/api/v2/*",
    "other",
}


def _samples(body: str):
    return [
        sample
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    ]


def test_unmatched_paths_have_deterministically_bounded_exposition(relay_client) -> None:
    before = _scrape(relay_client)
    before_samples = len(_samples(before))
    secrets_seen = []
    for index in range(2_000):
        raw = f"unmatched-attacker-path-{index}"
        secrets_seen.append(raw)
        response = relay_client.get(f"/{raw}?credential=query-{index}")
        assert response.status_code == 404

    body = _scrape(relay_client)
    samples = _samples(body)
    assert len(samples) <= before_samples + 20
    assert len(body.encode()) <= len(before.encode()) + 10_000
    assert all(raw not in body for raw in secrets_seen)
    assert "query-1999" not in body
    assert "flask_http_request" not in body

    http_samples = [sample for sample in samples if sample.name == "tokenplace_http_requests_total"]
    unmatched = [
        sample
        for sample in http_samples
        if sample.labels
        == {
            "method": "GET",
            "route": "other",
            "status_class": "4xx",
            "provider_mode": "relay",
            "outcome": "failed",
        }
    ]
    assert len(unmatched) == 1
    assert unmatched[0].value >= 2_000
    assert {sample.labels["method"] for sample in http_samples} <= BOUNDED_METHODS
    assert {sample.labels["route"] for sample in http_samples} <= BOUNDED_ROUTES
    assert {sample.labels["status_class"] for sample in http_samples} <= {
        "1xx", "2xx", "3xx", "4xx", "5xx", "unknown"
    }
    assert {sample.labels["provider_mode"] for sample in http_samples} <= set(relay.PROVIDER_MODE_ENUM)
    assert {sample.labels["outcome"] for sample in http_samples} <= set(relay.OUTCOME_ENUM)


def test_metrics_scrapes_do_not_instrument_themselves(relay_client) -> None:
    before = _scrape(relay_client)
    for _ in range(5):
        _scrape(relay_client)
    after = _scrape(relay_client)
    for metric_name in (
        "tokenplace_http_requests_total",
        "tokenplace_http_request_duration_seconds_count",
        "tokenplace_relay_requests_total",
    ):
        assert sum(s.value for s in _samples(after) if s.name == metric_name) == sum(
            s.value for s in _samples(before) if s.name == metric_name
        )
    assert 'route="/metrics"' not in after
    assert 'endpoint="/metrics"' not in after


def _run_relay_entrypoint(tmp_path: Path, metrics_dir: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_gunicorn = bin_dir / "gunicorn"
    fake_gunicorn.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$PROMETHEUS_MULTIPROC_DIR\"\n",
        encoding="utf-8",
    )
    fake_gunicorn.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "PROMETHEUS_MULTIPROC_DIR": str(metrics_dir),
        }
    )
    return subprocess.run(
        ["sh", "docker/relay/entrypoint.sh"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_container_restart_clears_only_dedicated_metrics_directory(tmp_path) -> None:
    metrics_dir = tmp_path / "prometheus"
    metrics_dir.mkdir()
    stale_file = metrics_dir / "counter_123.db"
    stale_file.write_text("stale", encoding="utf-8")
    sibling = tmp_path / "must-survive.txt"
    sibling.write_text("safe", encoding="utf-8")

    first = _run_relay_entrypoint(tmp_path, metrics_dir)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == str(metrics_dir)
    assert list(metrics_dir.iterdir()) == []
    assert sibling.read_text(encoding="utf-8") == "safe"

    stale_file.write_text("stale-after-container-restart", encoding="utf-8")
    second = _run_relay_entrypoint(tmp_path, metrics_dir)
    assert second.returncode == 0, second.stderr
    assert list(metrics_dir.iterdir()) == []
    assert sibling.read_text(encoding="utf-8") == "safe"


def test_metrics_startup_fails_closed_for_symlink_target(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    metrics_link = tmp_path / "prometheus-link"
    metrics_link.symlink_to(target, target_is_directory=True)

    result = _run_relay_entrypoint(tmp_path, metrics_link)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "safe"


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
