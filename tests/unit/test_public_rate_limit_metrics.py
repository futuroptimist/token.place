"""Bounded, privacy-safe public quota telemetry contract tests."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import relay

METRIC = "tokenplace_public_rate_limit_requests_total"
SAMPLE_RE = re.compile(rf"^{METRIC}\{{([^}}]+)\}} [0-9.eE+-]+$")


def _reset_limiters() -> None:
    for limiter in relay.app.extensions.get("limiter", set()):
        storage = getattr(getattr(limiter, "limiter", None), "storage", None)
        if storage is not None and hasattr(storage, "reset"):
            storage.reset()


@pytest.fixture()
def client():
    relay.app.config["TESTING"] = True
    _reset_limiters()
    with relay.app.test_client() as test_client:
        yield test_client
    _reset_limiters()


def _metrics(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _samples(body: str) -> set[str]:
    return {line for line in body.splitlines() if line.startswith(f"{METRIC}{'{'}")}


def test_fixed_vocabularies_are_closed() -> None:
    assert relay.PUBLIC_QUOTA_ROUTE_CLASS_ENUM == (
        "root",
        "public_metadata",
        "public_version",
        "api_v1_inference",
        "api_v1_other",
        "api_v2",
        "relay_control",
        "health",
        "metrics",
        "static",
        "unknown",
    )
    assert (*relay.CANONICAL_HTTP_METHOD_ENUM, "other") == (
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
        "hourly",
        "daily",
        "other",
    )


@pytest.mark.parametrize(
    ("method", "path", "route_class"),
    [
        ("GET", "/", "root"),
        ("HEAD", "/", "root"),
        ("GET", "/api/v1/meta", "public_metadata"),
        ("HEAD", "/api/v1/meta", "public_metadata"),
        ("GET", "/api/v1/version", "public_version"),
        ("HEAD", "/api/v1/version", "public_version"),
    ],
)
def test_public_information_reads_record_exempt(
    client, method, path, route_class
) -> None:
    assert client.open(path, method=method).status_code == 200
    expected = (
        f'{METRIC}{{method="{method}",outcome="exempt",'
        f'rejection_reason="none",route_class="{route_class}"}}'
    )
    assert expected in _metrics(client)


def test_accepted_request_and_unknown_route_are_observable(client) -> None:
    assert client.get("/api/v1/models").status_code == 200
    assert client.get("/unmatched-once").status_code == 404
    body = _metrics(client)
    assert (
        f'{METRIC}{{method="GET",outcome="accepted",rejection_reason="none",route_class="api_v1_other"}}'
        in body
    )
    assert (
        f'{METRIC}{{method="GET",outcome="accepted",rejection_reason="none",route_class="unknown"}}'
        in body
    )


@pytest.mark.parametrize(
    ("hourly", "daily", "reason"),
    [("1/hour", "100/day", "hourly"), ("100/hour", "1/day", "daily")],
)
def test_actual_default_limit_rejections_have_bounded_reason(
    hourly, daily, reason
) -> None:
    code = f"""\nimport relay\nc = relay.app.test_client()\nassert c.get("/api/v1/models").status_code == 200\nassert c.get("/api/v1/models").status_code == 429\nb = c.get("/metrics").get_data(as_text=True)\nneedle = 'tokenplace_public_rate_limit_requests_total{{method="GET",outcome="rejected",rejection_reason="{reason}",route_class="api_v1_other"}}'\nassert needle in b, b\nprint(needle)\n"""
    env = os.environ.copy()
    env.update(API_RATE_LIMIT=hourly, API_DAILY_QUOTA=daily, API_STREAM_RATE_LIMIT="")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_unmatched_paths_identities_and_hostile_values_cannot_add_series(
    client,
) -> None:
    sentinels = {
        "path-secret",
        "query-secret",
        "192.0.2.123",
        "198.51.100.77",
        "limiter-key-secret",
        "request-id-secret",
        "token-secret",
        "credential-secret",
        "exception-secret",
        "UNREVIEWED-METHOD",
    }
    before = _samples(_metrics(client))
    for index in range(2_000):
        response = client.open(
            f"/path-secret-{index}",
            method="UNREVIEWED-METHOD",
            query_string={"q": f"query-secret-{index}"},
            headers={
                "X-Forwarded-For": f"forwarded-identity-{index}",
                "X-Request-Id": f"request-id-secret-{index}",
                "Authorization": f"Bearer token-secret-{index}",
                "X-Relay-Server-Token": f"credential-secret-{index}",
            },
            environ_base={"REMOTE_ADDR": f"direct-identity-{index}"},
        )
        assert response.status_code in {404, 429}
    body = _metrics(client)
    after = _samples(body)
    added = after - before
    assert len(added) <= 2
    unmatched_added = {sample for sample in added if 'route_class="unknown"' in sample}
    assert unmatched_added
    assert all('method="other"' in sample for sample in unmatched_added)
    assert all('route_class="metrics"' in sample for sample in added - unmatched_added)
    assert all(SAMPLE_RE.fullmatch(sample) for sample in after)
    assert not any(value in body for value in sentinels)

    with relay.app.test_request_context("/", method="GET"):
        relay.g.tokenplace_public_quota_rejection_reason = "exception-secret"
        assert relay._public_quota_decision(relay.Response(status=429)) == (
            "rejected",
            "other",
        )


def test_non_inference_429_does_not_change_inference_outcome(client) -> None:
    before = relay.generate_latest(relay.RELAY_METRICS_REGISTRY)
    before_value = re.search(
        rb'tokenplace_relay_request_outcomes_total\{outcome="rate_limited"\} ([0-9.eE+-]+)',
        before,
    ).group(1)
    with relay.app.test_request_context("/api/v1/meta", method="GET"):
        relay.g.request_start_time = 0
        relay.g.tokenplace_public_quota_rejection_reason = "hourly"
        relay._log_request(relay.Response(status=429))
    after = relay.generate_latest(relay.RELAY_METRICS_REGISTRY)
    after_value = re.search(
        rb'tokenplace_relay_request_outcomes_total\{outcome="rate_limited"\} ([0-9.eE+-]+)',
        after,
    ).group(1)
    assert after_value == before_value


def test_counters_are_process_local_and_reset_in_each_worker() -> None:
    code = """\nimport relay\nc = relay.app.test_client()\nif __import__("os").environ.get("SEND"):\n    c.get("/api/v1/models")\nprint(c.get("/metrics").get_data(as_text=True))\n"""
    outputs = []
    for send in ("1", ""):
        env = os.environ.copy()
        env["SEND"] = send
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[2],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    accepted = f'{METRIC}{{method="GET",outcome="accepted",rejection_reason="none",route_class="api_v1_other"}}'
    assert accepted in outputs[0]
    assert accepted not in outputs[1]
