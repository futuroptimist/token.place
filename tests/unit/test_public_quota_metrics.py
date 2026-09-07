"""Bounded, privacy-safe public quota telemetry contract tests."""

from __future__ import annotations

import re

import pytest
from flask import Flask

import relay
from api import init_app


@pytest.fixture()
def relay_client():
    """Provide the relay client with fresh public limiter counters."""

    relay.app.config["TESTING"] = True
    for limiter in relay.app.extensions.get("limiter", set()):
        storage = getattr(getattr(limiter, "limiter", None), "storage", None)
        reset = getattr(storage, "reset", None)
        if reset is not None:
            reset()
    with relay.app.test_client() as client:
        yield client


def _samples(body: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if line.startswith("tokenplace_public_http_quota_outcomes_total{")
    ]


def _value(body: str, labels: str) -> float:
    pattern = re.compile(
        rf"^tokenplace_public_http_quota_outcomes_total{re.escape(labels)} ([0-9.eE+-]+)$"
    )
    for line in body.splitlines():
        match = pattern.match(line)
        if match:
            return float(match.group(1))
    return 0.0


def _body(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _install_recorder(app: Flask) -> None:
    app.after_request(relay._record_public_quota_outcome)


@pytest.mark.parametrize(
    ("rate_limit", "daily_quota", "reason"),
    [
        ("1/hour", "100/day", "hourly_limit"),
        ("100/hour", "1/day", "daily_limit"),
    ],
)
def test_accepted_and_limited_requests_have_fixed_reasons(
    monkeypatch, rate_limit, daily_quota, reason
) -> None:
    monkeypatch.setenv("API_RATE_LIMIT", rate_limit)
    monkeypatch.setenv("API_DAILY_QUOTA", daily_quota)
    app = Flask(__name__)
    init_app(app, metrics_export_defaults=False, metrics_path=None)
    _install_recorder(app)

    with app.test_client() as client:
        with app.test_request_context("/api/v1/models"):
            accepted_labels = (
                '{method="GET",outcome="accepted",reason="none",'
                'route_class="public_api"}'
            )
            limited_labels = (
                f'{{method="GET",outcome="rejected",reason="{reason}",'
                'route_class="public_api"}'
            )
        before = relay.generate_latest(relay.RELAY_METRICS_REGISTRY).decode()
        assert client.get("/api/v1/models").status_code == 200
        assert client.get("/api/v1/models").status_code == 429
        after = relay.generate_latest(relay.RELAY_METRICS_REGISTRY).decode()

    assert _value(after, accepted_labels) == _value(before, accepted_labels) + 1
    assert _value(after, limited_labels) == _value(before, limited_labels) + 1


@pytest.mark.parametrize(
    ("path", "route_class"),
    [
        ("/", "root"),
        ("/api/v1/meta", "public_metadata"),
        ("/api/v1/version", "public_version"),
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_public_information_reads_are_observable_as_exempt(
    relay_client, path, route_class, method
) -> None:
    before = _body(relay_client)
    response = relay_client.open(path, method=method)
    assert response.status_code == 200
    after = _body(relay_client)
    labels = (
        f'{{method="{method}",outcome="exempt",reason="none",'
        f'route_class="{route_class}"}}'
    )
    assert _value(after, labels) == _value(before, labels) + 1


def test_unknown_paths_and_methods_collapse_to_fixed_fallbacks(relay_client) -> None:
    before = _body(relay_client)
    for index in range(1_000):
        response = relay_client.open(
            f"/sentinel-unmatched-{index}?query=sentinel-query-{index}",
            method=f"SENTINEL_METHOD_{index}",
        )
        assert response.status_code in {404, 429}
    after = _body(relay_client)

    before_labels = {line.partition(" ")[0] for line in _samples(before)}
    after_labels = {line.partition(" ")[0] for line in _samples(after)}
    added = {
        line
        for line in after_labels - before_labels
        if 'route_class="unknown_route"' in line
    }
    assert 1 <= len(added) <= 2
    assert all('method="other"' in line for line in added)
    assert all('route_class="unknown_route"' in line for line in added)
    assert "sentinel-unmatched" not in after
    assert "sentinel-query" not in after
    assert "SENTINEL_METHOD" not in after


def test_identities_and_sensitive_values_never_create_series_or_leak(
    relay_client,
) -> None:
    before = _body(relay_client)
    sentinels = {
        "sentinel-address",
        "sentinel-forwarded",
        "sentinel-limiter-key",
        "sentinel-request-id",
        "sentinel-token",
        "sentinel-credential",
        "sentinel-exception",
    }
    for index in range(1_000):
        response = relay_client.get(
            "/api/v1/models",
            environ_base={"REMOTE_ADDR": f"198.51.{index // 256}.{index % 256}"},
            headers={
                "Forwarded": f"for=sentinel-forwarded-{index}",
                "X-Forwarded-For": f"203.0.113.{index % 256}",
                "X-Request-Id": f"sentinel-request-id-{index}",
                "Authorization": f"Bearer sentinel-token-{index}",
                "X-Relay-Server-Token": f"sentinel-credential-{index}",
            },
            query_string={"bucket": f"sentinel-limiter-key-{index}"},
        )
        assert response.status_code == 200
    after = _body(relay_client)

    before_labels = {line.partition(" ")[0] for line in _samples(before)}
    after_labels = {line.partition(" ")[0] for line in _samples(after)}
    assert len(after_labels - before_labels) <= 1
    assert all(value not in after for value in sentinels)
    assert "198.51." not in after
    assert "203.0.113." not in after


def test_metric_vocabularies_and_cardinality_are_closed() -> None:
    assert relay.PUBLIC_QUOTA_ROUTE_CLASS_ENUM == (
        "root",
        "public_metadata",
        "public_version",
        "public_api",
        "client_relay_read",
        "compute_control_plane",
        "operational",
        "unknown_route",
    )
    assert relay.CANONICAL_HTTP_METHOD_ENUM + ("other",) == (
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
        "other",
    )
    assert relay.PUBLIC_QUOTA_OUTCOME_ENUM == ("accepted", "exempt", "rejected")
    assert relay.PUBLIC_QUOTA_REJECTION_REASON_ENUM == (
        "none",
        "hourly_limit",
        "daily_limit",
        "other_limit",
    )
    assert 8 * 8 * (2 + 3) == 320


def test_supported_gunicorn_metrics_configuration_is_single_process() -> None:
    entrypoint = relay.os.path.dirname(relay.__file__) + "/docker/relay/entrypoint.sh"
    with open(entrypoint, encoding="utf-8") as stream:
        script = stream.read()
    assert 'WORKERS="${RELAY_WORKERS:-1}"' in script
    assert "PROMETHEUS_MULTIPROC_DIR" not in script
    assert "MultiProcessCollector" not in script


def test_inference_outcome_contract_is_unchanged_by_public_429(relay_client) -> None:
    before = _body(relay_client)
    outcome_lines_before = {
        line
        for line in before.splitlines()
        if line.startswith("tokenplace_relay_request_outcomes_total{")
    }
    for index in range(65):
        relay_client.get(f"/ordinary-public-{index}")
    after = _body(relay_client)
    outcome_lines_after = {
        line
        for line in after.splitlines()
        if line.startswith("tokenplace_relay_request_outcomes_total{")
    }
    assert outcome_lines_after == outcome_lines_before
