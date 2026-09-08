"""Startup-only emergency bounded metrics mode contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROBE = r"""
import json
import relay
from prometheus_client.parser import text_string_to_metric_families

relay.app.config["TESTING"] = True
with relay.app.test_client() as client:
    health = {path: client.get(path).status_code for path in ("/livez", "/healthz")}
    unauthorized = client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code
    headers = {"Authorization": "Bearer metrics-secret"}
    first = client.get("/metrics", headers=headers)
    before = first.get_data(as_text=True)
    for index in range(2000):
        client.get(
            f"/sensitive-path-{index}?token=query-secret",
            environ_base={"REMOTE_ADDR": f"198.51.{index // 256}.{index % 256}"},
            headers={
                "CF-Connecting-IP": f"203.0.113.{index % 256}",
                "X-Forwarded-For": f"192.0.2.{index % 256}",
                "X-Request-Id": f"request-secret-{index}",
            },
        )
        relay._record_request_terminal_outcome_once(
            f"client-secret-{index}", f"terminal-request-secret-{index}", "completed"
        )
    second = client.get("/metrics", headers=headers)
    after = second.get_data(as_text=True)
families = list(text_string_to_metric_families(after))
samples = sorted(sample.name for family in families for sample in family.samples)
print(json.dumps({
    "mode": relay.METRICS_MODE,
    "health": health,
    "unauthorized": unauthorized,
    "authorized": first.status_code,
    "samples": samples,
    "stable": before == after,
    "quota_extension": "tokenplace_public_quota_counter" in relay.app.extensions,
    "terminal_outcome_clients": len(relay.client_terminal_outcomes),
    "sensitive": any(value in after for value in (
        "sensitive-path", "query-secret", "198.51", "203.0.113", "192.0.2", "request-secret",
        "metrics-secret",
    )),
}))
"""


def _probe(mode: str | None) -> dict[str, object]:
    env = os.environ.copy()
    env["TOKENPLACE_METRICS_TOKEN"] = "metrics-secret"
    if mode is None:
        env.pop("TOKENPLACE_METRICS_MODE", None)
    else:
        env["TOKENPLACE_METRICS_MODE"] = mode
    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.splitlines()[-1])


@pytest.mark.parametrize("mode", (None, "normal"))
def test_normal_mode_preserves_complete_bounded_registry(mode: str | None) -> None:
    result = _probe(mode)
    assert result["mode"] == "normal"
    samples = result["samples"]
    assert "tokenplace_http_requests_total" in samples
    assert "tokenplace_public_http_quota_outcomes_total" in samples
    assert "tokenplace_relay_queue_depth" in samples
    assert "tokenplace_relay_compute_control_requests_total" in samples
    assert "tokenplace_build_info" in samples
    assert "tokenplace_instrumentation_up" in samples
    assert "tokenplace_metrics_degraded" in samples
    assert result["quota_extension"] is True


def test_degraded_mode_is_exactly_three_stable_private_series() -> None:
    result = _probe("degraded")
    assert result == {
        "mode": "degraded",
        "health": {"/livez": 200, "/healthz": 200},
        "unauthorized": 401,
        "authorized": 200,
        "samples": [
            "tokenplace_build_info",
            "tokenplace_instrumentation_up",
            "tokenplace_metrics_degraded",
        ],
        "stable": True,
        "quota_extension": False,
        "terminal_outcome_clients": 0,
        "sensitive": False,
    }


@pytest.mark.parametrize(
    "mode", ("", "NORMAL", "Degraded", " degraded", "degraded ", "unknown")
)
def test_invalid_mode_fails_startup_without_echoing_value(mode: str) -> None:
    env = os.environ.copy()
    env["TOKENPLACE_METRICS_MODE"] = mode
    env["TOKENPLACE_METRICS_TOKEN"] = "credential-sentinel"
    completed = subprocess.run(
        [sys.executable, "-c", "import relay"],
        env=env,
        capture_output=True,
        text=True,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert (
        "TOKENPLACE_METRICS_MODE must be exactly one of: normal, degraded" in combined
    )
    assert "credential-sentinel" not in combined


def test_fresh_process_can_restore_normal_after_degraded() -> None:
    assert _probe("degraded")["samples"] == [
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_metrics_degraded",
    ]
    restored = _probe("normal")
    assert restored["mode"] == "normal"
    assert "tokenplace_http_request_duration_seconds_count" in restored["samples"]
    assert "tokenplace_compute_nodes_registered" in restored["samples"]
