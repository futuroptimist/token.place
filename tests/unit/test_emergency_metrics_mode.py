"""Startup-only emergency bounded metrics mode contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

METRICS_CREDENTIAL = "metrics-credential-sentinel"
SENTINELS = (
    "path-sentinel", "identity-sentinel", "request-sentinel", "token-sentinel",
    METRICS_CREDENTIAL,
)

EXPECTED_NORMAL_FAMILIES = {
    "tokenplace_build_info",
    "tokenplace_compute_node_evictions_total",
    "tokenplace_compute_node_lease_age_seconds",
    "tokenplace_compute_nodes_healthy",
    "tokenplace_compute_nodes_registered",
    "tokenplace_http_request_duration_seconds",
    "tokenplace_http_requests_total",
    "tokenplace_instrumentation_up",
    "tokenplace_metrics_degraded",
    "tokenplace_public_http_quota_outcomes_total",
    "tokenplace_relay_compute_control_lease_renewals_total",
    "tokenplace_relay_compute_control_requests_total",
    "tokenplace_relay_in_flight_requests",
    "tokenplace_relay_oldest_in_flight_age_seconds",
    "tokenplace_relay_oldest_queued_request_age_seconds",
    "tokenplace_relay_queue_depth",
    "tokenplace_relay_request_outcomes_total",
    "tokenplace_relay_requests_total",
}

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
headers = {"Authorization": "Bearer metrics-credential-sentinel"}
with relay.app.test_client() as client:
    health = {path: client.get(path).status_code for path in ("/livez", "/healthz")}
    unauthorized = client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code
    first = client.get("/metrics", headers=headers)
    before = first.get_data(as_text=True)
    for index in range(2000):
        direct_identity = f"192.0.2.{index % 254 + 1}"
        forwarded_identity = f"198.51.100.{index % 254 + 1}"
        request_id = f"request-sentinel-{index}"
        client.get(f"/static/path-sentinel-{index}?token=token-sentinel", environ_base={"REMOTE_ADDR": direct_identity})
        client.get(f"/path-sentinel-{index}?token=token-sentinel", headers={
            "CF-Connecting-IP": forwarded_identity, "X-Forwarded-For": forwarded_identity,
            "X-Request-Id": request_id,
        }, environ_base={"REMOTE_ADDR": "10.1.2.3"})
        relay._record_request_terminal_outcome_once(direct_identity, request_id, "completed")
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
    def __init__(self, name, metric): self.name, self.metric = name, metric
    def __getattr__(self, name): return getattr(self.metric, name)
    def labels(self, *args, **kwargs):
        return GaugeProxy(self.name, self.metric.labels(*args, **kwargs))
    def set(self, value):
        if failure == self.name + ":set": raise RuntimeError("private-sentinel")
        return self.metric.set(value)
def gauge(name, *args, **kwargs):
    if failure == name + ":construct":
        raise RuntimeError("private-sentinel")
    metric = real_gauge(name, *args, **kwargs)
    return GaugeProxy(name, metric)
prometheus_client.Gauge = gauge
import relay
with relay.app.test_client() as client:
    response = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
print(json.dumps({"status": response.status_code, "body": response.get_data(as_text=True),
                  "mode": relay.METRICS_MODE}))
'''

NORMAL_PROBE = r'''
import json, re, relay
from prometheus_client.parser import text_string_to_metric_families
with relay.app.test_client() as client:
    health = {path: client.get(path).status_code for path in ("/livez", "/healthz")}
    unauthorized = client.get("/metrics").status_code
    response = client.get("/metrics", headers={"Authorization": "Bearer metrics-credential-sentinel"})
body = response.get_data(as_text=True)
list(text_string_to_metric_families(body))
families = {
    name for name in re.findall(r"^# HELP (\S+)", body, re.MULTILINE)
    if not name.endswith("_created")
}
print(json.dumps({"mode": relay.METRICS_MODE, "families": sorted(families),
                  "quota": "tokenplace_public_quota_counter" in relay.app.extensions,
                  "health": health, "unauthorized": unauthorized, "authorized": response.status_code}))
'''


def _run_probe(source: str, mode: str | None, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra, TOKENPLACE_METRICS_TOKEN=METRICS_CREDENTIAL)
    if mode is None:
        env.pop("TOKENPLACE_METRICS_MODE", None)
    else:
        env["TOKENPLACE_METRICS_MODE"] = mode
    return subprocess.run([sys.executable, "-c", source], env=env, capture_output=True, text=True)


def _probe(mode: str | None) -> dict[str, object]:
    completed = _run_probe(STRESS_PROBE, mode)
    assert completed.returncode == 0, completed.stderr
    combined = completed.stdout + completed.stderr
    assert all(sentinel not in combined for sentinel in SENTINELS)
    return json.loads(completed.stdout.splitlines()[-1])


def _normal_probe(mode: str | None) -> dict[str, object]:
    completed = _run_probe(NORMAL_PROBE, mode)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result == {
        "mode": "normal",
        "families": sorted(EXPECTED_NORMAL_FAMILIES),
        "quota": True,
        "health": {"/livez": 200, "/healthz": 200},
        "unauthorized": 401,
        "authorized": 200,
    }
    return result


@pytest.mark.parametrize("mode", (None, "normal"))
def test_normal_mode_preserves_complete_bounded_registry(mode: str | None) -> None:
    _normal_probe(mode)


def test_unset_and_explicit_normal_registries_match() -> None:
    assert _normal_probe(None) == _normal_probe("normal")


def test_degraded_mode_is_exactly_three_stable_private_series_without_state() -> None:
    result = _probe("degraded")
    assert result == {
        "mode": "degraded", "health": {"/livez": 200, "/healthz": 200},
        "unauthorized": 401, "authorized": 200,
        "samples": ["tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded"],
        "stable": True, "quota_extension": False, "terminal_outcome_clients": 0,
        "stale_lease_entries": 0, "sensitive": False,
    }


@pytest.mark.parametrize("collector", (
    "tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded",
))
@pytest.mark.parametrize("operation", ("construct", "set"))
def test_required_degraded_collector_failure_fails_startup_privately(
    collector: str, operation: str,
) -> None:
    failure = f"{collector}:{operation}"
    completed = _run_probe(FAILURE_PROBE, "degraded", PROBE_FAILURE=failure)
    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "private-sentinel" not in combined
    assert "required degraded metrics" in combined


@pytest.mark.parametrize("mode", ("", "NORMAL", "Degraded", " degraded", "degraded ", "unknown"))
def test_invalid_mode_fails_startup_without_echoing_value(mode: str) -> None:
    completed = _run_probe("import relay", mode)
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "TOKENPLACE_METRICS_MODE must be exactly one of: normal, degraded" in combined
    assert METRICS_CREDENTIAL not in combined


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    (("collector", 200), ("registry", 200), ("runtime_gauge", 503), ("serialization", 503)),
)
def test_normal_metrics_errors_do_not_change_mode(failure: str, expected_status: int) -> None:
    source = r'''
import json, os, relay
failure = os.environ["PROBE_FAILURE"]
class Bomb:
    def labels(self, *args): raise RuntimeError("sentinel")
    def set(self, *args): raise RuntimeError("sentinel")
if failure == "collector":
    relay._collector("broken", lambda: (_ for _ in ()).throw(RuntimeError("sentinel")))
elif failure == "registry":
    relay.RELAY_METRICS_REGISTRY.register = lambda collector: (_ for _ in ()).throw(RuntimeError("sentinel"))
    relay._collector("broken-registry", lambda: relay.Counter("broken_registry", "broken", registry=relay.RELAY_METRICS_REGISTRY))
elif failure == "runtime_gauge":
    relay._update_runtime_gauges = lambda: (_ for _ in ()).throw(RuntimeError("sentinel"))
else:
    reached = {"serialization": False}
    def fail_serialization(registry):
        reached["serialization"] = True
        raise RuntimeError("sentinel")
    relay.generate_latest = fail_serialization
with relay.app.test_client() as client:
    status = client.get("/metrics", headers={"Authorization": "Bearer metrics-credential-sentinel"}).status_code
print(json.dumps({"mode": relay.METRICS_MODE, "status": status,
                  "serialization_reached": failure != "serialization" or reached["serialization"]}))
'''
    completed = _run_probe(source, "normal", PROBE_FAILURE=failure)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "mode": "normal", "status": expected_status, "serialization_reached": True,
    }


def test_fresh_process_can_restore_normal_after_degraded() -> None:
    assert _probe("degraded")["samples"] == [
        "tokenplace_build_info", "tokenplace_instrumentation_up", "tokenplace_metrics_degraded",
    ]
    _normal_probe("normal")
