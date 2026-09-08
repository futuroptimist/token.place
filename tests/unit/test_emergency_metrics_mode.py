"""Startup-only emergency bounded metrics mode contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

SENTINELS = ("path-sentinel", "identity-sentinel", "request-sentinel", "token-sentinel")

STRESS_PROBE = r'''
import json
import relay
from prometheus_client.parser import text_string_to_metric_families

class Bomb:
    def __getattr__(self, name):
        raise AssertionError("suppressed collector invoked")

relay.app.config["TESTING"] = True
relay._update_runtime_gauges = lambda: (_ for _ in ()).throw(AssertionError("runtime gauges invoked"))
relay.RELAY_REQUEST_OUTCOMES_TOTAL = Bomb()
relay.COMPUTE_NODE_EVICTIONS_TOTAL = Bomb()
relay.RELAY_COMPUTE_CONTROL_REQUESTS_TOTAL = Bomb()
relay.RELAY_COMPUTE_CONTROL_LEASE_RENEWALS_TOTAL = Bomb()
headers = {"Authorization": "Bearer metrics-secret"}
with relay.app.test_client() as client:
    health = {path: client.get(path).status_code for path in ("/livez", "/healthz")}
    unauthorized = client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code
    first = client.get("/metrics", headers=headers)
    before = first.get_data(as_text=True)
    for index in range(2000):
        identity = f"identity-sentinel-{index}"
        request_id = f"request-sentinel-{index}"
        client.get(f"/static/path-sentinel-{index}?token=token-sentinel", environ_base={"REMOTE_ADDR": identity})
        client.get(f"/path-sentinel-{index}?token=token-sentinel", headers={
            "CF-Connecting-IP": identity, "X-Forwarded-For": identity, "X-Request-Id": request_id,
        })
        relay._record_request_terminal_outcome_once(identity, request_id, "completed")
    relay._record_terminal_outcome("completed")
    relay._record_compute_control_state("active")
    relay._record_compute_control_lease_renewal()
    relay._reconcile_api_v1_stale_lease_evictions(Bomb())
    second = client.get("/metrics", headers=headers)
    third = client.get("/metrics", headers=headers)
    diagnostics = client.get("/relay/diagnostics").get_data(as_text=True)
    after = second.get_data(as_text=True)
families = list(text_string_to_metric_families(after))
samples = sorted(sample.name for family in families for sample in family.samples)
print(json.dumps({
    "mode": relay.METRICS_MODE, "health": health, "unauthorized": unauthorized,
    "authorized": first.status_code, "samples": samples,
    "stable": before == after == third.get_data(as_text=True),
    "quota_extension": "tokenplace_public_quota_counter" in relay.app.extensions,
    "terminal_outcome_clients": len(relay.client_terminal_outcomes),
    "stale_lease_entries": len(relay._api_v1_seen_stale_lease_evictions),
    "sensitive": any(value in after + diagnostics for value in
                     ("path-sentinel", "identity-sentinel", "request-sentinel", "token-sentinel")),
}))
'''

FAILURE_PROBE = r'''
import json, os, prometheus_client
real_gauge = prometheus_client.Gauge
failure = os.environ["PROBE_FAILURE"]
class GaugeProxy:
    def __init__(self, metric): self.metric = metric
    def __getattr__(self, name): return getattr(self.metric, name)
    def set(self, value):
        if failure == "indicator_set": raise RuntimeError("private-sentinel")
        return self.metric.set(value)
def gauge(name, *args, **kwargs):
    if name == "tokenplace_metrics_degraded" and failure == "indicator_construct":
        raise RuntimeError("private-sentinel")
    if name == "tokenplace_build_info" and failure == "other_retained_construct":
        raise RuntimeError("private-sentinel")
    metric = real_gauge(name, *args, **kwargs)
    return GaugeProxy(metric) if name == "tokenplace_metrics_degraded" else metric
prometheus_client.Gauge = gauge
import relay
with relay.app.test_client() as client:
    response = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
print(json.dumps({"status": response.status_code, "body": response.get_data(as_text=True),
                  "mode": relay.METRICS_MODE}))
'''


def _run_probe(source: str, mode: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra, TOKENPLACE_METRICS_MODE=mode, TOKENPLACE_METRICS_TOKEN="metrics-secret")
    return subprocess.run([sys.executable, "-c", source], env=env, capture_output=True, text=True)


def _probe(mode: str | None) -> dict[str, object]:
    selected = "normal" if mode is None else mode
    completed = _run_probe(STRESS_PROBE, selected)
    assert completed.returncode == 0, completed.stderr
    combined = completed.stdout + completed.stderr
    assert all(sentinel not in combined for sentinel in SENTINELS)
    return json.loads(completed.stdout.splitlines()[-1])


@pytest.mark.parametrize("mode", (None, "normal"))
def test_normal_mode_preserves_complete_bounded_registry(mode: str | None) -> None:
    source = r'''
import json, relay
from prometheus_client.parser import text_string_to_metric_families
with relay.app.test_client() as client:
    client.get("/healthz")
body = relay.generate_latest(relay.RELAY_METRICS_REGISTRY).decode()
samples = {sample.name for family in text_string_to_metric_families(body) for sample in family.samples}
print(json.dumps({"mode": relay.METRICS_MODE, "samples": sorted(samples),
                  "quota": "tokenplace_public_quota_counter" in relay.app.extensions}))
'''
    completed = _run_probe(source, "normal")
    assert completed.returncode == 0
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["mode"] == "normal"
    assert result["quota"] is True
    assert {
        "tokenplace_http_requests_total", "tokenplace_public_http_quota_outcomes_total",
        "tokenplace_relay_queue_depth", "tokenplace_relay_compute_control_requests_total",
        "tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded",
    }.issubset(result["samples"])


def test_degraded_mode_is_exactly_three_stable_private_series_without_state() -> None:
    result = _probe("degraded")
    assert result == {
        "mode": "degraded", "health": {"/livez": 200, "/healthz": 200},
        "unauthorized": 401, "authorized": 200,
        "samples": ["tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded"],
        "stable": True, "quota_extension": False, "terminal_outcome_clients": 0,
        "stale_lease_entries": 0, "sensitive": False,
    }


@pytest.mark.parametrize("failure", ("indicator_construct", "indicator_set"))
def test_degraded_indicator_failure_fails_startup_privately(failure: str) -> None:
    completed = _run_probe(FAILURE_PROBE, "degraded", PROBE_FAILURE=failure)
    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "private-sentinel" not in combined
    assert "required degraded metrics" in combined


def test_other_collector_failure_retains_truthful_degraded_signal() -> None:
    completed = _run_probe(FAILURE_PROBE, "degraded", PROBE_FAILURE="other_retained_construct")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["status"] == 200
    assert "tokenplace_metrics_degraded 1.0" in result["body"]
    assert "tokenplace_instrumentation_up 0.0" in result["body"]
    assert "private-sentinel" not in completed.stdout + completed.stderr


@pytest.mark.parametrize("mode", ("", "NORMAL", "Degraded", " degraded", "degraded ", "unknown"))
def test_invalid_mode_fails_startup_without_echoing_value(mode: str) -> None:
    completed = _run_probe("import relay", mode)
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "TOKENPLACE_METRICS_MODE must be exactly one of: normal, degraded" in combined
    assert "metrics-secret" not in combined


def test_normal_runtime_metrics_errors_do_not_change_mode() -> None:
    source = r'''
import relay
class Bomb:
    def labels(self, *args): raise RuntimeError("sentinel")
    def set(self, *args): raise RuntimeError("sentinel")
relay.RELAY_REQUEST_OUTCOMES_TOTAL = Bomb()
relay._record_terminal_outcome("completed")
relay._collector("broken", lambda: (_ for _ in ()).throw(RuntimeError("sentinel")))
relay.RELAY_METRICS_REGISTRY.register = lambda collector: (_ for _ in ()).throw(RuntimeError("sentinel"))
relay._collector("broken-registry", lambda: relay.Counter("broken_registry", "broken", registry=relay.RELAY_METRICS_REGISTRY))
relay._update_runtime_gauges = lambda: (_ for _ in ()).throw(RuntimeError("sentinel"))
relay.generate_latest = lambda registry: (_ for _ in ()).throw(RuntimeError("sentinel"))
with relay.app.test_client() as client:
    status = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"}).status_code
print(__import__("json").dumps({"mode": relay.METRICS_MODE, "status": status}))
'''
    completed = _run_probe(source, "normal")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == {"mode": "normal", "status": 503}


def test_fresh_process_can_restore_normal_after_degraded() -> None:
    assert _probe("degraded")["samples"] == [
        "tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded",
    ]
    completed = _run_probe("import relay; print(relay.METRICS_MODE)", "normal")
    assert completed.returncode == 0
    assert completed.stdout.splitlines()[-1] == "normal"
