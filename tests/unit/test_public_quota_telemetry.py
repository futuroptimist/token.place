"""Bounded, privacy-safe public quota telemetry contract tests."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap

import pytest
from flask import Response, g

import relay

METRIC = "tokenplace_public_http_quota_outcomes_total"


def _samples(body: str) -> set[str]:
    return {
        line.split(" ", 1)[0]
        for line in body.splitlines()
        if line.startswith(f"{METRIC}{{")
    }


def _body(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture()
def client():
    relay.app.config["TESTING"] = True
    for limiter in relay.app.extensions.get("limiter", set()):
        storage = getattr(getattr(limiter, "limiter", None), "storage", None)
        if storage is not None and hasattr(storage, "reset"):
            storage.reset()
    with relay.app.test_client() as test_client:
        yield test_client


def test_quota_metric_uses_exact_closed_vocabularies() -> None:
    assert relay.PUBLIC_QUOTA_ROUTE_CLASS_ENUM == (
        "root",
        "public_metadata",
        "public_version",
        "public_api_v1",
        "public_api_compat",
        "operational",
        "other_known",
        "unknown_route",
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
        "hourly_limit",
        "daily_limit",
        "other_limit",
    )


def test_accepted_exempt_and_unknown_route_requests(client) -> None:
    assert client.get("/api/v1/models").status_code == 200
    for method in (client.get, client.head):
        assert method("/").status_code == 200
        assert method("/api/v1/meta").status_code == 200
        assert method("/api/v1/version").status_code == 200
    assert client.get("/not-an-application-route").status_code == 404

    body = _body(client)
    assert (
        f'{METRIC}{{method="GET",outcome="accepted",rejection_reason="none",route_class="public_api_v1"}}'
        in body
    )
    for route_class in ("root", "public_metadata", "public_version"):
        for method in ("GET", "HEAD"):
            assert (
                f'{METRIC}{{method="{method}",outcome="exempt",rejection_reason="none",route_class="{route_class}"}}'
                in body
            )
    assert (
        f'{METRIC}{{method="GET",outcome="accepted",rejection_reason="none",route_class="unknown_route"}}'
        in body
    )


@pytest.mark.parametrize(
    ("hourly", "daily", "reason"),
    [("1/hour", "1000/day", "hourly_limit"), ("100/hour", "1/day", "daily_limit")],
)
def test_real_limiter_rejections_are_classified(hourly, daily, reason) -> None:
    script = textwrap.dedent(f"""
        import relay
        client = relay.app.test_client()
        assert client.get('/api/v1/models').status_code == 200
        assert client.get('/api/v1/models').status_code == 429
        body = client.get('/metrics').get_data(as_text=True)
        expected = 'tokenplace_public_http_quota_outcomes_total{{method="GET",outcome="rejected",rejection_reason="{reason}",route_class="public_api_v1"}} 1.0'
        assert expected in body, expected
        assert 'tokenplace_relay_request_outcomes_total{{outcome="rate_limited"}} 0.0' in body
    """)
    env = {**os.environ, "API_RATE_LIMIT": hourly, "API_DAILY_QUOTA": daily}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unrecognized_methods_and_reasons_use_fallbacks(client) -> None:
    assert client.open("/unknown-method", method="BREW").status_code == 404
    with relay.app.test_request_context("/", method="GET"):
        g.tokenplace_public_quota_exempt = False
        g.tokenplace_public_quota_rejection_reason = "sentinel-exception-text"
        assert relay._public_quota_labels(Response(status=429)) == (
            "rejected",
            "other_limit",
        )
    body = _body(client)
    assert (
        f'{METRIC}{{method="other",outcome="accepted",rejection_reason="none",route_class="unknown_route"}}'
        in body
    )
    assert "sentinel-exception-text" not in body


def test_thousands_of_paths_and_identities_cannot_grow_or_taint_series(client) -> None:
    before = _samples(_body(client))
    sentinels = {
        "path": "raw-path-sentinel",
        "query": "query-secret-sentinel",
        "direct": "198.51.100.234",
        "forwarded": "forwarded-identity-sentinel",
        "limiter": "limiter-key-sentinel",
        "request": "request-id-sentinel",
        "token": "token-secret-sentinel",
        "credential": "credential-secret-sentinel",
        "exception": "exception-text-sentinel",
    }
    for index in range(2000):
        response = client.get(
            f"/{sentinels['path']}-{index}",
            query_string={"value": f"{sentinels['query']}-{index}"},
            headers={
                "Forwarded": f"for={sentinels['forwarded']}-{index}",
                "X-Forwarded-For": f"{sentinels['forwarded']}-{index}",
                "X-Request-Id": f"{sentinels['request']}-{index}",
                "Authorization": f"Bearer {sentinels['token']}-{index}",
                "X-Relay-Server-Token": f"{sentinels['credential']}-{index}",
                "X-Limiter-Key": f"{sentinels['limiter']}-{index}",
            },
            environ_overrides={"REMOTE_ADDR": f"198.51.{index // 250}.{index % 250}"},
        )
        assert response.status_code == 404

    body = _body(client)
    after = _samples(body)
    assert after == before
    assert len(after) == 320
    for sentinel in sentinels.values():
        assert sentinel not in body
    assert not re.search(r'route_class="/(?!")', body)


def test_public_quota_events_do_not_change_inference_outcomes(client) -> None:
    before = {
        line
        for line in _body(client).splitlines()
        if line.startswith("tokenplace_relay_request_outcomes_total{")
    }
    assert client.get("/").status_code == 200
    assert client.get("/missing").status_code == 404
    after = {
        line
        for line in _body(client).splitlines()
        if line.startswith("tokenplace_relay_request_outcomes_total{")
    }
    assert after == before


def test_single_worker_gunicorn_multiprocess_environment_has_bounded_exposition(
    tmp_path,
) -> None:
    script = textwrap.dedent("""
        import relay
        client = relay.app.test_client()
        assert client.get('/').status_code == 200
        body = client.get('/metrics').get_data(as_text=True)
        lines = [line for line in body.splitlines() if line.startswith('tokenplace_public_http_quota_outcomes_total{')]
        assert len(lines) == 320
        assert all('pid=' not in line for line in lines)
    """)
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
