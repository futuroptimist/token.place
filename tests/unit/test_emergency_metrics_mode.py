"""Focused process-level coverage for the emergency metrics mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def _run(code: str, mode: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TOKENPLACE_METRICS_MODE", None)
    env.pop("TOKENPLACE_METRICS_TOKEN", None)
    if mode is not None:
        env["TOKENPLACE_METRICS_MODE"] = mode
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


FAMILIES_SCRIPT = """
import json
import relay
print(json.dumps({
    "mode": relay.METRICS_MODE,
    "families": sorted(metric.name for metric in relay.RELAY_METRICS_REGISTRY.collect()),
    "quota_hook": "tokenplace_public_quota_counter" in relay.app.extensions,
}))
"""


def _last_json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_unset_and_explicit_normal_preserve_full_registry() -> None:
    unset = _last_json(_run(FAMILIES_SCRIPT))
    normal = _last_json(_run(FAMILIES_SCRIPT, "normal"))
    assert unset == normal
    assert normal["mode"] == "normal"
    assert normal["quota_hook"] is True
    assert "tokenplace_http_requests" in normal["families"]
    assert "tokenplace_public_http_quota_outcomes" in normal["families"]
    assert "tokenplace_metrics_degraded" not in normal["families"]


@pytest.mark.parametrize("mode", ["", "NORMAL", "Degraded", " degraded", "degraded ", "unknown"])
def test_invalid_mode_fails_startup_without_echoing_value(mode: str) -> None:
    sentinel = f"sensitive-{mode}"
    result = _run("import relay", mode)
    assert result.returncode != 0
    assert "TOKENPLACE_METRICS_MODE must be exactly one of: normal, degraded" in result.stderr
    assert sentinel not in result.stdout + result.stderr


def test_degraded_registry_is_exactly_three_fixed_series_and_preserves_endpoints() -> None:
    result = _run(
        """
import json, logging, os
import relay
relay.LOGGER.setLevel(logging.CRITICAL)
client = relay.app.test_client()
unauthorized_before = client.get('/metrics').status_code
os.environ['TOKENPLACE_METRICS_TOKEN'] = 'metrics-secret'
unauthorized_after = client.get('/metrics').status_code
headers = {'Authorization': 'Bearer metrics-secret'}
first = client.get('/metrics', headers=headers)
first_body = first.get_data(as_text=True)
for index in range(2000):
    unique = f'private-canary-{index}'
    client.get('/known/' + unique, headers={
        'CF-Connecting-IP': f'198.51.{index % 255}.{(index * 7) % 255}',
        'X-Forwarded-For': unique,
        'X-Request-Id': unique,
    })
    client.get('/static/' + unique, query_string={'token': unique})
for _ in range(25):
    assert client.get('/metrics', headers=headers).get_data(as_text=True) == first_body
last_body = client.get('/metrics', headers=headers).get_data(as_text=True)
print(json.dumps({
    'mode': relay.METRICS_MODE,
    'families': sorted(metric.name for metric in relay.RELAY_METRICS_REGISTRY.collect()),
    'samples': sum(len(metric.samples) for metric in relay.RELAY_METRICS_REGISTRY.collect()),
    'quota_hook': 'tokenplace_public_quota_counter' in relay.app.extensions,
    'unauthorized_before': unauthorized_before,
    'unauthorized_after': unauthorized_after,
    'scrape': first.status_code,
    'livez': client.get('/livez').status_code,
    'healthz': client.get('/healthz').status_code,
    'stable': first_body == last_body,
    'private_absent': 'private-canary-' not in last_body,
}))
""",
        "degraded",
    )
    assert "private-canary-" not in result.stdout + result.stderr
    evidence = _last_json(result)
    assert evidence == {
        "mode": "degraded",
        "families": [
            "tokenplace_build_info",
            "tokenplace_instrumentation_up",
            "tokenplace_metrics_degraded",
        ],
        "samples": 3,
        "quota_hook": False,
        "unauthorized_before": 200,
        "unauthorized_after": 401,
        "scrape": 200,
        "livez": 200,
        "healthz": 200,
        "stable": True,
        "private_absent": True,
    }


def test_metrics_errors_do_not_activate_degraded_mode() -> None:
    evidence = _last_json(
        _run(
            """
import json
import relay
relay.generate_latest = lambda registry: (_ for _ in ()).throw(RuntimeError('sentinel-secret'))
response = relay.app.test_client().get('/metrics')
print(json.dumps({'mode': relay.METRICS_MODE, 'status': response.status_code, 'body': response.get_data(as_text=True)}))
""",
            "normal",
        )
    )
    assert evidence == {"mode": "normal", "status": 503, "body": "metrics unavailable\n"}


def test_fresh_process_restores_normal_registry_after_degraded() -> None:
    degraded = _last_json(_run(FAMILIES_SCRIPT, "degraded"))
    restored = _last_json(_run(FAMILIES_SCRIPT, "normal"))
    assert degraded["families"] == [
        "tokenplace_build_info",
        "tokenplace_instrumentation_up",
        "tokenplace_metrics_degraded",
    ]
    assert "tokenplace_http_requests" in restored["families"]
    assert "tokenplace_public_http_quota_outcomes" in restored["families"]
