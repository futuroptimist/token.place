"""Isolation tests for the explicit emergency bounded-metrics startup mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def _relay_process(mode: str | None, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if mode is None:
        env.pop("TOKENPLACE_METRICS_MODE", None)
    else:
        env["TOKENPLACE_METRICS_MODE"] = mode
    env.pop("TOKENPLACE_METRICS_DISABLED", None)
    env.pop("TOKENPLACE_METRICS_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-c", script], env=env, text=True, capture_output=True, check=False,
    )


PROBE = r'''
import json
import relay
client = relay.app.test_client()
before = client.get("/metrics").get_data(as_text=True)
for index in range(1000):
    headers = {
        "X-Request-Id": f"request-sensitive-{index}",
        "CF-Connecting-IP": f"198.51.{index // 256}.{index % 256}",
        "X-Forwarded-For": f"203.0.113.{index % 256}",
    }
    client.get(f"/unmatched-sensitive-{index}?token=secret-{index}", headers=headers)
    client.get(f"/api/v1/models/model-sensitive-{index}", headers=headers)
after = client.get("/metrics").get_data(as_text=True)
families = sorted({line.split("{")[0].split()[0] for line in after.splitlines()
                   if line and not line.startswith("#")})
print("RESULT=" + json.dumps({
    "mode": relay.METRICS_MODE,
    "before": before,
    "after": after,
    "families": families,
    "healthz": client.get("/healthz").status_code,
    "livez": client.get("/livez").status_code,
    "quota_collector": "tokenplace_public_quota_counter" in relay.app.extensions,
    "suppressed_noop": isinstance(relay.HTTP_REQUESTS_TOTAL, relay._NoopMetric),
}))
'''


def _result(process: subprocess.CompletedProcess[str]) -> dict:
    assert process.returncode == 0, process.stderr
    marker = next(line for line in process.stdout.splitlines() if line.startswith("RESULT="))
    return json.loads(marker.removeprefix("RESULT="))


@pytest.mark.parametrize("mode", [None, "normal"])
def test_normal_mode_is_default_and_preserves_application_metrics(mode: str | None) -> None:
    result = _result(_relay_process(mode, PROBE))
    assert result["mode"] == "normal"
    assert result["healthz"] == result["livez"] == 200
    assert result["quota_collector"] is True
    assert result["suppressed_noop"] is False
    assert "tokenplace_http_requests_total" in result["after"]
    assert "tokenplace_public_http_quota_outcomes_total" in result["after"]
    assert "tokenplace_metrics_degraded 0.0" in result["after"]


def test_degraded_mode_is_exactly_three_immutable_series() -> None:
    result = _result(_relay_process("degraded", PROBE))
    assert result["mode"] == "degraded"
    assert result["healthz"] == result["livez"] == 200
    assert result["quota_collector"] is False
    assert result["suppressed_noop"] is True
    assert result["before"] == result["after"]
    assert result["families"] == [
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_metrics_degraded",
    ]
    for sentinel in (
        "unmatched-sensitive", "model-sensitive", "request-sensitive", "secret-",
        "198.51.", "203.0.113.", "flask_http_request",
    ):
        assert sentinel not in result["after"]


@pytest.mark.parametrize("mode", ["", "NORMAL", "Degraded", " degraded", "degraded ", "unknown"])
def test_invalid_metrics_modes_fail_startup_without_echoing_input(mode: str) -> None:
    process = _relay_process(mode, "import relay")
    assert process.returncode != 0
    assert "TOKENPLACE_METRICS_MODE must be exactly one of: normal, degraded" in process.stderr
    if mode not in {"normal", "degraded"}:
        assert repr(mode) not in process.stderr


def test_metrics_authentication_is_identical_in_both_modes() -> None:
    script = r'''
import json, os
os.environ["TOKENPLACE_METRICS_TOKEN"] = "sensitive-auth-sentinel"
import relay
client = relay.app.test_client()
print("RESULT=" + json.dumps([
    client.get("/metrics").status_code,
    client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code,
    client.get("/metrics", headers={"Authorization": "Bearer sensitive-auth-sentinel"}).status_code,
]))
'''
    assert _result(_relay_process("normal", script)) == [401, 401, 200]
    assert _result(_relay_process("degraded", script)) == [401, 401, 200]


def test_fresh_process_can_restore_complete_normal_registry() -> None:
    degraded = _result(_relay_process("degraded", PROBE))
    restored = _result(_relay_process("normal", PROBE))
    assert degraded["families"] == [
        "tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded",
    ]
    assert "tokenplace_http_request_duration_seconds_count" in restored["families"]
    assert "tokenplace_relay_queue_depth" in restored["families"]
