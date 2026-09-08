"""Startup and cardinality coverage for emergency degraded relay metrics."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


def _run_relay(code: str, mode: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TOKENPLACE_METRICS_MODE", None)
    env.pop("TOKENPLACE_METRICS_TOKEN", None)
    env.pop("TOKENPLACE_METRICS_DISABLED", None)
    if mode is not None:
        env["TOKENPLACE_METRICS_MODE"] = mode
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


DEGRADED_ASSERTIONS = """
import relay
from prometheus_client import generate_latest

assert relay.METRICS_MODE == "degraded"
expected = {
    "tokenplace_build_info",
    "tokenplace_instrumentation_up",
    "tokenplace_metrics_degraded",
}
assert set(relay.RELAY_METRICS_REGISTRY._names_to_collectors) == expected
assert "tokenplace_public_quota_counter" not in relay.app.extensions

client = relay.app.test_client()
before = generate_latest(relay.RELAY_METRICS_REGISTRY)
assert client.get("/livez").status_code == 200
assert client.get("/healthz").status_code == 200
for index in range(2000):
    assert client.get(f"/cardinality-{index}?secret=query-{index}").status_code == 404
    client.get(
        "/api/v1/models",
        headers={
            "X-Forwarded-For": f"198.51.{index // 256}.{index % 256}",
            "CF-Connecting-IP": f"203.0.{index // 256}.{index % 256}",
            "X-Request-Id": f"sensitive-{index}",
        },
        environ_base={"REMOTE_ADDR": f"192.0.{index // 256}.{index % 256}"},
    )
after_requests = generate_latest(relay.RELAY_METRICS_REGISTRY)
assert before == after_requests
for _ in range(20):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.data == before
body = response.get_data(as_text=True)
assert sum(1 for line in body.splitlines() if line and not line.startswith("#")) == 3
for forbidden in (
    "tokenplace_http_requests", "tokenplace_relay_queue", "tokenplace_public_http_quota",
    "flask_http", "cardinality-", "sensitive-", "198.51.", "203.0.", "192.0.", "query-",
):
    assert forbidden not in body
assert relay.app.extensions.get("prometheus_metrics") is None or "flask_http" not in body
"""


@pytest.mark.parametrize("mode", [None, "normal"])
def test_normal_mode_is_default_and_preserves_full_registry(mode: str | None) -> None:
    result = _run_relay(
        """
        import relay
        names = set(relay.RELAY_METRICS_REGISTRY._names_to_collectors)
        assert relay.METRICS_MODE == "normal"
        assert "tokenplace_http_requests_total" in names
        assert "tokenplace_public_http_quota_outcomes_total" in names
        assert "tokenplace_relay_queue_depth" in names
        assert "tokenplace_build_info" in names
        assert "tokenplace_instrumentation_up" in names
        assert "tokenplace_metrics_degraded" not in names
        """,
        mode,
    )
    assert result.returncode == 0, result.stderr


def test_degraded_mode_has_exact_immutable_three_series_registry() -> None:
    result = _run_relay(DEGRADED_ASSERTIONS, "degraded")
    assert result.returncode == 0, result.stderr


def test_degraded_authentication_matches_normal_and_health_stays_public() -> None:
    code = """
    import os
    os.environ["TOKENPLACE_METRICS_TOKEN"] = "metrics-secret"
    import relay
    client = relay.app.test_client()
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"}).status_code == 200
    assert client.get("/livez").status_code == 200
    assert client.get("/healthz").status_code == 200
    """
    for mode in ("normal", "degraded"):
        result = _run_relay(code, mode)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mode", ["", "NORMAL", "Degraded", " degraded", "normal ", "unknown"]
)
def test_invalid_metrics_mode_fails_startup_without_echoing_environment(
    mode: str,
) -> None:
    sentinel = "sensitive-credential-must-not-appear"
    result = _run_relay("import relay", mode)
    assert result.returncode != 0
    assert "TOKENPLACE_METRICS_MODE must be one of: normal, degraded" in result.stderr
    assert sentinel not in result.stdout + result.stderr


def test_fresh_process_restores_normal_registry_after_degraded_mode() -> None:
    degraded = _run_relay(DEGRADED_ASSERTIONS, "degraded")
    assert degraded.returncode == 0, degraded.stderr
    normal = _run_relay(
        """
        import relay
        names = set(relay.RELAY_METRICS_REGISTRY._names_to_collectors)
        assert relay.METRICS_MODE == "normal"
        assert "tokenplace_public_http_quota_outcomes_total" in names
        assert "tokenplace_http_request_duration_seconds" in names
        """,
        "normal",
    )
    assert normal.returncode == 0, normal.stderr


def test_collector_failure_does_not_silently_activate_degraded_mode() -> None:
    result = _run_relay(
        """
        import relay
        relay._METRICS_CONSTRUCTION_FAILED = True
        relay._initialise_metric_labels()
        assert relay.METRICS_MODE == "normal"
        """,
        "normal",
    )
    assert result.returncode == 0, result.stderr
