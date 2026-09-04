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
from prometheus_client import REGISTRY
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


def test_metrics_registry_is_dedicated_and_not_multiprocess() -> None:
    """The one-worker release registry must not inherit process-global metrics."""

    assert relay.RELAY_METRICS_REGISTRY is not REGISTRY
    names = set(relay.RELAY_METRICS_REGISTRY._names_to_collectors)
    assert "tokenplace_http_requests" in names
    assert all(not name.startswith("flask_http_request") for name in names)


def test_single_process_startup_clears_only_dedicated_stale_metrics(tmp_path) -> None:
    """Container restart cleanup removes stale files without touching siblings."""

    metrics_dir = Path("/tmp/tokenplace-prometheus-multiproc")
    metrics_dir.mkdir(parents=True, exist_ok=True)
    stale = metrics_dir / "gauge_livemostrecent_123.db"
    stale.write_text("stale", encoding="utf-8")
    sibling = tmp_path / "must-survive"
    sibling.write_text("safe", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gunicorn = fake_bin / "gunicorn"
    fake_gunicorn.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gunicorn.chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:{env['PATH']}", "PROMETHEUS_MULTIPROC_DIR": str(metrics_dir)})

    subprocess.run(
        ["sh", "docker/relay/entrypoint.sh"],
        cwd=Path(__file__).parents[2], env=env, check=True, capture_output=True, text=True,
    )

    assert not stale.exists()
    assert sibling.read_text(encoding="utf-8") == "safe"


def test_single_process_startup_rejects_multiple_workers(tmp_path) -> None:
    """Multiprocess mode fails closed for the memory-backed maintenance release."""

    env = os.environ.copy()
    env["RELAY_WORKERS"] = "2"
    result = subprocess.run(
        ["sh", "docker/relay/entrypoint.sh"],
        cwd=Path(__file__).parents[2], env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "requires RELAY_WORKERS=1" in result.stderr


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


def test_unmatched_paths_have_fixed_cardinality_and_bounded_exposition(
    relay_client, caplog
) -> None:
    """Thousands of attacker-selected paths collapse to one deterministic series set."""

    marker = "raw-sensitive-unmatched"
    caplog.set_level(logging.ERROR, logger="tokenplace.relay")
    before = _scrape(relay_client)
    for index in range(2_000):
        response = relay_client.get(f"/{marker}-{index}?credential=secret-{index}")
        assert response.status_code in {404, 429}

    body = _scrape(relay_client)
    families = list(text_string_to_metric_families(body))
    samples = [sample for family in families for sample in family.samples]
    route_samples = [
        sample for sample in samples if sample.name == "tokenplace_http_requests_total"
    ]

    assert "flask_http_request" not in body
    assert marker not in body
    assert "credential" not in body
    before_labels = {
        tuple(sorted(sample.labels.items()))
        for family in text_string_to_metric_families(before)
        for sample in family.samples
        if sample.name == "tokenplace_http_requests_total"
    }
    unmatched_samples = [
        sample for sample in route_samples if sample.labels["route"] == "other"
    ]
    assert unmatched_samples
    new_labels = {tuple(sorted(sample.labels.items())) for sample in route_samples}
    assert len(new_labels - before_labels) <= 2
    assert {sample.labels["method"] for sample in route_samples} <= set(
        relay.CANONICAL_HTTP_METHOD_ENUM
    ) | {"other"}
    assert {sample.labels["status_class"] for sample in route_samples} <= {
        "1xx", "2xx", "3xx", "4xx", "5xx", "unknown"
    }
    assert {sample.labels["provider_mode"] for sample in route_samples} <= set(
        relay.PROVIDER_MODE_ENUM
    )
    assert {sample.labels["outcome"] for sample in route_samples} <= set(
        relay.OUTCOME_ENUM
    )
    # These budgets include every series accumulated by earlier module tests;
    # the 2,000 unmatched requests may add only the fixed labels asserted above.
    assert len(samples) <= 700
    assert len(body.encode("utf-8")) <= 100_000


def test_metrics_requests_do_not_instrument_themselves(relay_client) -> None:
    """Authorized scrapes must not recursively increase HTTP metric series."""

    before = _scrape(relay_client)
    for _ in range(5):
        _scrape(relay_client)
    after = _scrape(relay_client)

    def request_total(body: str) -> float:
        return sum(
            sample.value
            for family in text_string_to_metric_families(body)
            for sample in family.samples
            if sample.name == "tokenplace_http_requests_total"
        )

    assert request_total(after) == request_total(before)
    assert 'route="/metrics"' not in after
