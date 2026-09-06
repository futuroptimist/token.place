from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path("scripts/relay_release_safety_gate.py")
SPEC = importlib.util.spec_from_file_location("relay_release_safety_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)

VALID = """# HELP tokenplace_http_requests_total requests
# TYPE tokenplace_http_requests_total counter
tokenplace_http_requests_total{method="GET",route="other",status_class="4xx"} 1e+06
tokenplace_instrumentation_up 1
"""

BACKPORT_VALID = """tokenplace_http_requests_total{method="GET", endpoint="metrics", route="/metrics", status_class="2xx"} 12
tokenplace_http_requests_total{method="GET",endpoint="unknown",route="/{unmatched}",status_class="4xx"} 3
tokenplace_instrumentation_up 1
"""


def test_parser_accepts_scientific_notation_and_decodes_identity():
    samples = gate.parse_metrics(VALID + 'escaped{route="template/{id} with space",note="a\\\\b\\\"c"} -2.5E-3 123\n'
                                  + "special_nan NaN\nspecial_inf +Inf\n")
    assert gate.Sample("escaped", (("note", 'a\\b"c'), ("route", "template/{id} with space"))) in samples
    assert len(samples) == 5


@pytest.mark.parametrize("text", ["", "# comments only\n", "metric help text\n", 'metric{bad="unterminated} 1\n',
                                  'metric{a="1" b="2"} 1\n', 'metric{a="1",a="2"} 1\n'])
def test_parser_rejects_empty_or_malformed_exposition(text):
    with pytest.raises(gate.GateFailure):
        gate.parse_metrics(text)


def test_metrics_fail_closed_for_http_errors_transport_and_unmatched_redirect(monkeypatch):
    for status in (500, 0, 302):
        monkeypatch.setattr(gate, "request", lambda *_args, s=status, **_kwargs: (s, ""))
        assert not any(v["passed"] for v in gate.execute_metrics_checks("http://loopback").values())


def test_hashed_raw_route_series_grows_even_with_scientific_values(monkeypatch):
    calls = 0
    paths: list[str] = []
    def fake_request(_base, path, method="GET"):
        nonlocal calls
        if path == "/metrics":
            calls += 1
            extra = "".join(f'hits{{route="{p}"}} 1e+06\n' for p in paths)
            return 200, VALID + extra
        paths.append(path)
        return 404, ""
    monkeypatch.setattr(gate, "request", fake_request)
    results = gate.execute_metrics_checks("http://loopback")
    assert results["metrics.no_raw_paths"]["passed"] is False
    assert results["metrics.bounded_unmatched_paths"]["passed"] is False


def test_bounded_template_metrics_pass_after_collector_warmup(monkeypatch):
    monkeypatch.setattr(gate, "request", lambda _base, path, method="GET":
                        (200, VALID) if path == "/metrics" else (404, ""))
    assert all(v["passed"] for v in gate.execute_metrics_checks("http://loopback").values())


@pytest.mark.parametrize("exposition", [VALID, BACKPORT_VALID])
def test_current_and_backport_bounded_endpoint_metrics_pass(monkeypatch, exposition):
    monkeypatch.setattr(gate, "request", lambda _base, path, method="GET":
                        (200, exposition) if path == "/metrics" else (404, ""))
    assert all(v["passed"] for v in gate.execute_metrics_checks("http://loopback").values())


@pytest.mark.parametrize("scrape_number", range(1, 5))
def test_every_required_metrics_scrape_fails_closed(monkeypatch, scrape_number):
    scrapes = 0

    def fake_request(_base, path, method="GET"):
        nonlocal scrapes
        if path == "/metrics":
            scrapes += 1
            return (500, "") if scrapes == scrape_number else (200, VALID)
        return 404, ""

    monkeypatch.setattr(gate, "request", fake_request)
    assert not any(v["passed"] for v in gate.execute_metrics_checks("http://loopback").values())


def test_instrumentation_down_fails_even_when_expected_series_exist(monkeypatch):
    down = VALID.replace("tokenplace_instrumentation_up 1", "tokenplace_instrumentation_up 0")
    monkeypatch.setattr(gate, "request", lambda _base, path, method="GET":
                        (200, down) if path == "/metrics" else (404, ""))
    results = gate.execute_metrics_checks("http://loopback")
    assert results["metrics.valid_instrumentation"]["passed"] is False


def test_hashed_per_path_labels_fail_without_raw_probe_values(monkeypatch):
    scrapes = 0

    def fake_request(_base, path, method="GET"):
        nonlocal scrapes
        if path != "/metrics":
            return 404, ""
        scrapes += 1
        count = 0 if scrapes < 3 else (24 if scrapes == 3 else 48)
        transformed = "".join(f'hits{{endpoint="digest-{number:064x}"}} 1\n' for number in range(count))
        return 200, VALID + transformed

    monkeypatch.setattr(gate, "request", fake_request)
    results = gate.execute_metrics_checks("http://loopback")
    assert results["metrics.no_raw_paths"]["passed"] is False
    assert results["metrics.bounded_unmatched_paths"]["passed"] is False


@pytest.mark.parametrize("statuses,expected", [
    ([200] * 12 + [200, 429], True), ([500] + [200] * 11 + [200, 429], False),
    ([200] * 12 + [500, 429], False), ([200] * 12 + [0, 429], False),
])
def test_quota_requires_all_public_and_initial_protected_success(monkeypatch, statuses, expected):
    iterator = iter(statuses)
    monkeypatch.setattr(gate, "request", lambda *_args, **_kwargs: (next(iterator), ""))
    results = gate.execute_quota_checks("http://loopback", "quota.protected_rate_limited")
    assert all(v["passed"] for v in results.values()) is expected


def test_exact_revision_and_verified_historical_abbreviation():
    full = "a" * 40
    gate.validate_candidate_identity(full, full)
    gate.validate_candidate_identity(full, "a" * 7, full)
    with pytest.raises(gate.GateFailure):
        gate.validate_candidate_identity(full, "a" * 7)
    with pytest.raises(gate.GateFailure):
        gate.validate_candidate_identity(full, "b" * 40)


def _run_main(tmp_path, monkeypatch, docker_output, cleanup=None):
    evidence = tmp_path / "evidence.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--image", "candidate", "--platform", "linux/amd64",
        "--source-commit", "a" * 40, "--release-ref", "refs/heads/work", "--release-base", "main",
        "--evidence", str(evidence)])
    monkeypatch.setattr(gate, "docker_output", docker_output)
    if cleanup:
        monkeypatch.setattr(gate.subprocess, "run", cleanup)
    result = gate.main()
    return result, json.loads(evidence.read_text())


def test_missing_runtime_emits_sanitized_not_run_evidence(tmp_path, monkeypatch):
    result, report = _run_main(tmp_path, monkeypatch, lambda *_a: (_ for _ in ()).throw(FileNotFoundError("secret")),
                               lambda *_a, **_k: None)
    assert result == 1 and report["error_category"] == "runtime_missing"
    assert all(v["state"] == "not_run" for v in report["results"].values())
    assert "secret" not in json.dumps(report)


def test_inspect_timeout_preserves_evidence(tmp_path, monkeypatch):
    result, report = _run_main(tmp_path, monkeypatch,
        lambda *_a: (_ for _ in ()).throw(subprocess.TimeoutExpired("private", 1)), lambda *_a, **_k: None)
    assert result == 1 and report["error_category"] == "runtime_timeout"


def test_cleanup_failure_does_not_mask_original(tmp_path, monkeypatch):
    values = iter(["a" * 40, "amd64", "sha256:" + "b" * 64, "container-id"])
    times = iter([100, 146])
    monkeypatch.setattr(gate.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(gate, "request", lambda *_a, **_k: (0, ""))
    result, report = _run_main(tmp_path, monkeypatch,
        lambda *_a: next(values),
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("cleanup details")))
    assert result == 1
    assert report["error_category"] == "startup_timeout"
    assert report["cleanup"] == "failed"
    assert "details" not in json.dumps(report)


def test_platform_identity_mismatch_fails_before_checks(tmp_path, monkeypatch):
    values = iter(["a" * 40, "arm64"])
    result, report = _run_main(tmp_path, monkeypatch, lambda *_a: next(values), lambda *_a, **_k: None)
    assert result == 1 and report["error_category"] == "platform_identity_mismatch"
