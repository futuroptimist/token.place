from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def _contract(requirements=None, schema_version=1):
    return {
        "schema_version": schema_version,
        "requirements": requirements if requirements is not None else [
            {"id": requirement, "description": f"Require {requirement}"}
            for requirement in sorted(gate.EXPECTED_IDS)
        ],
    }


@pytest.mark.parametrize(
    "contents,category",
    [
        (None, "contract_invalid"),
        ("{malformed", "contract_invalid"),
        (json.dumps(_contract(schema_version=2)), "contract_invalid"),
        (json.dumps(_contract([{"id": "duplicate", "description": "first"},
                               {"id": "duplicate", "description": "second"}])), "contract_invalid"),
        (json.dumps(_contract([{"id": "metrics.valid_instrumentation", "description": "only one"}])),
         "contract_mismatch"),
    ],
)
def test_load_contract_rejects_invalid_or_incomplete_contracts(tmp_path, contents, category):
    contract = tmp_path / "contract.json"
    if contents is not None:
        contract.write_text(contents, encoding="utf-8")
    with pytest.raises(gate.GateFailure, match=category):
        gate.load_contract(contract)


def test_load_contract_accepts_complete_contract(tmp_path):
    contract = tmp_path / "contract.json"
    document = _contract()
    contract.write_text(json.dumps(document), encoding="utf-8")
    assert gate.load_contract(contract) == document["requirements"]


def test_request_preserves_http_semantics_and_refuses_redirects():
    hits = {"success": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/success")
                self.end_headers()
                return
            if self.path == "/error":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"unavailable")
                return
            hits["success"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert gate.request(base_url, "/success") == (200, "ready")
        assert gate.request(base_url, "/error") == (503, "unavailable")
        hits["success"] = 0
        assert gate.request(base_url, "/redirect") == (302, "")
        assert hits["success"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    # The stopped loopback server provides a deterministic transport failure.
    assert gate.request(base_url, "/success") == (0, "")


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


def test_lazy_bounded_unmatched_collectors_are_not_per_path_growth(monkeypatch):
    scrapes = 0

    def fake_request(_base, path, method="GET"):
        nonlocal scrapes
        if path != "/metrics":
            return 404, ""
        scrapes += 1
        if scrapes <= 2:
            return 200, VALID
        count = 24 if scrapes == 3 else 48
        lazy = f'''tokenplace_http_requests_total{{method="GET",endpoint="unknown",route="other",status_class="4xx"}} {count}
tokenplace_http_request_duration_seconds_count{{method="GET",endpoint="unknown",route="other",status_class="4xx"}} {count}
tokenplace_http_request_duration_seconds_sum{{method="GET",endpoint="unknown",route="other",status_class="4xx"}} {count / 1000}
'''
        return 200, VALID + lazy

    monkeypatch.setattr(gate, "request", fake_request)
    results = gate.execute_metrics_checks("http://loopback")
    assert all(result["passed"] for result in results.values())
    assert results["metrics.bounded_unmatched_paths"] == {
        "passed": True,
        "first_batch_growth": 3,
        "second_batch_growth": 0,
    }


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


def _run_main(tmp_path, monkeypatch, docker_output, cleanup=None, registry_args=()):
    evidence = tmp_path / "evidence.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--image", "candidate", "--platform", "linux/amd64",
        "--source-commit", "a" * 40, "--release-ref", "refs/heads/work", "--release-base", "main",
        "--evidence", str(evidence), *registry_args])
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
    values = iter(["sha256:" + "b" * 64, "a" * 40, "amd64", "container-id"])
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
    values = iter(["sha256:" + "b" * 64, "a" * 40, "arm64"])
    result, report = _run_main(tmp_path, monkeypatch, lambda *_a: next(values), lambda *_a, **_k: None)
    assert result == 1 and report["error_category"] == "platform_identity_mismatch"


PLATFORM_DIGEST = "sha256:" + "c" * 64
INDEX_DIGEST = "sha256:" + "d" * 64
COORDINATE = f"ghcr.io/example/relay@{PLATFORM_DIGEST}"


def _manifest(digest=PLATFORM_DIGEST, platform="linux/amd64"):
    os_name, architecture = platform.split("/")
    return json.dumps({"manifests": [{
        "digest": digest, "platform": {"os": os_name, "architecture": architecture},
    }]})


def test_registry_identity_binds_local_image_and_index(monkeypatch):
    values = iter([json.dumps([COORDINATE]), _manifest()])
    monkeypatch.setattr(gate, "docker_output", lambda *_args: next(values))
    gate.validate_registry_identity("candidate", "linux/amd64", COORDINATE, INDEX_DIGEST, PLATFORM_DIGEST)


@pytest.mark.parametrize("coordinate,index_digest,platform_digest", [
    (COORDINATE, "malformed", PLATFORM_DIGEST),
    ("ghcr.io/example/relay@sha256:" + "e" * 64, INDEX_DIGEST, PLATFORM_DIGEST),
    ("repo@wrong", INDEX_DIGEST, PLATFORM_DIGEST),
])
def test_malformed_or_contradictory_registry_identity_fails_without_docker(
    monkeypatch, coordinate, index_digest, platform_digest,
):
    monkeypatch.setattr(gate, "docker_output", lambda *_args: pytest.fail("docker identity lookup ran"))
    with pytest.raises(gate.GateFailure):
        gate.validate_registry_identity("candidate", "linux/amd64", coordinate, index_digest, platform_digest)


def test_registry_identity_rejects_local_image_mismatch(monkeypatch):
    monkeypatch.setattr(gate, "docker_output", lambda *_args: json.dumps([]))
    with pytest.raises(gate.GateFailure, match="local_image_identity_mismatch"):
        gate.validate_registry_identity("candidate", "linux/amd64", COORDINATE, INDEX_DIGEST, PLATFORM_DIGEST)


@pytest.mark.parametrize("manifest", [_manifest(digest="sha256:" + "e" * 64), _manifest(platform="linux/arm64")])
def test_registry_identity_rejects_missing_or_wrong_platform_index_member(monkeypatch, manifest):
    values = iter([json.dumps([COORDINATE]), manifest])
    monkeypatch.setattr(gate, "docker_output", lambda *_args: next(values))
    with pytest.raises(gate.GateFailure, match="index_platform_mismatch"):
        gate.validate_registry_identity("candidate", "linux/amd64", COORDINATE, INDEX_DIGEST, PLATFORM_DIGEST)


@pytest.mark.parametrize("values,registry_args,category", [
    (["sha256:" + "b" * 64, "a" * 40, "amd64"], ("--registry-coordinate", "repo@wrong", "--index-digest", INDEX_DIGEST,
      "--platform-digest", PLATFORM_DIGEST), "registry_identity_mismatch"),
    (["sha256:" + "b" * 64, "a" * 40, "amd64", "[]"], ("--registry-coordinate", COORDINATE, "--index-digest", INDEX_DIGEST,
      "--platform-digest", PLATFORM_DIGEST), "local_image_identity_mismatch"),
    (["sha256:" + "b" * 64, "a" * 40, "amd64", json.dumps([COORDINATE]), _manifest(platform="linux/arm64")],
     ("--registry-coordinate", COORDINATE, "--index-digest", INDEX_DIGEST,
      "--platform-digest", PLATFORM_DIGEST), "index_platform_mismatch"),
])
def test_registry_mismatch_persists_not_run_evidence_before_probes(
    tmp_path, monkeypatch, values, registry_args, category,
):
    monkeypatch.setattr(gate, "request", lambda *_args, **_kwargs: pytest.fail("probe ran"))
    result, report = _run_main(
        tmp_path, monkeypatch, lambda *_args: values.pop(0),
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0), registry_args,
    )
    assert result == 1 and report["error_category"] == category
    assert report["passed"] is False
    assert all(item["state"] == "not_run" for item in report["results"].values())


def test_main_runs_inspected_image_id_not_mutable_alias(tmp_path, monkeypatch):
    image_id = "sha256:" + "b" * 64
    calls = []
    immutable_values = iter(["a" * 40, "amd64"])
    phase_values = iter(["metrics", "rate", "daily"])
    monkeypatch.setattr(gate, "request", lambda *_args, **_kwargs: (200, ""))
    monkeypatch.setattr(gate, "execute_metrics_checks", lambda _base: {
        key: {"passed": True} for key in gate.EXPECTED_IDS if key.startswith("metrics.")
    })
    monkeypatch.setattr(gate, "execute_quota_checks", lambda _base, limit_id: {
        "quota.public_information_exempt": {"passed": True}, limit_id: {"passed": True},
    })

    def output(*args):
        calls.append(args)
        if args[:3] == ("image", "inspect", "candidate"):
            return image_id  # The alias now resolves to a replacement image.
        if args[:3] == ("image", "inspect", image_id):
            return next(immutable_values)
        if args[0] == "run":
            return next(phase_values)
        pytest.fail(f"unexpected mutable-alias inspection: {args}")

    result, report = _run_main(
        tmp_path, monkeypatch, output,
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    assert result == 0 and report["image_id"] == image_id
    assert [call[-1] for call in calls if call[0] == "run"] == [image_id] * 3


def test_replacement_alias_registry_identity_cannot_validate_original(tmp_path, monkeypatch):
    image_id = "sha256:" + "b" * 64

    def output(*args):
        if args[:3] == ("image", "inspect", "candidate"):
            return image_id
        if args[:3] == ("image", "inspect", image_id):
            if args[-1] == "{{json .RepoDigests}}":
                return "[]"
            return "a" * 40 if "revision" in args[-1] else "amd64"
        pytest.fail(f"replacement alias was inspected: {args}")

    monkeypatch.setattr(gate, "request", lambda *_args, **_kwargs: pytest.fail("probe ran"))
    result, report = _run_main(
        tmp_path, monkeypatch, output,
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
        ("--registry-coordinate", COORDINATE, "--index-digest", INDEX_DIGEST,
         "--platform-digest", PLATFORM_DIGEST),
    )
    assert result == 1 and report["error_category"] == "local_image_identity_mismatch"
    assert all(item["state"] == "not_run" for item in report["results"].values())


def _run_qualified_main(tmp_path, monkeypatch, exemptions=(True, True), missing=None, cleanup_code=0,
                        protected=(True, True)):
    values = iter(["sha256:" + "b" * 64, "a" * 40, "amd64", "metrics", "rate", "daily"])
    monkeypatch.setattr(gate, "request", lambda *_a, **_k: (200, ""))
    monkeypatch.setattr(gate, "execute_metrics_checks", lambda _base: {
        key: {"passed": True} for key in gate.EXPECTED_IDS if key.startswith("metrics.")
    })
    quota_calls = 0

    def quota_results(_base, limit_id):
        nonlocal quota_calls
        phase = "rate" if quota_calls == 0 else "daily"
        result = {limit_id: {"passed": protected[quota_calls]}}
        if missing != phase:
            result["quota.public_information_exempt"] = {"passed": exemptions[quota_calls]}
        quota_calls += 1
        return result

    monkeypatch.setattr(gate, "execute_quota_checks", quota_results)

    def cleanup(*_args, **_kwargs):
        return subprocess.CompletedProcess([], cleanup_code)

    return _run_main(tmp_path, monkeypatch, lambda *_a: next(values), cleanup)


@pytest.mark.parametrize("exemptions", [(False, True), (True, False)])
def test_public_exemption_failure_is_retained_across_phases(tmp_path, monkeypatch, exemptions):
    result, report = _run_qualified_main(tmp_path, monkeypatch, exemptions=exemptions)
    public = report["results"]["quota.public_information_exempt"]
    assert result == 1 and report["error_category"] == "mandatory_check_failed"
    assert public["state"] == "failed"
    assert {phase: item["passed"] for phase, item in public["phases"].items()} == {
        "rate": exemptions[0], "daily": exemptions[1]
    }


@pytest.mark.parametrize("missing", ["rate", "daily"])
def test_missing_public_exemption_phase_fails_closed(tmp_path, monkeypatch, missing):
    result, report = _run_qualified_main(tmp_path, monkeypatch, missing=missing)
    public = report["results"]["quota.public_information_exempt"]
    assert result == 1 and public["passed"] is False and public["state"] == "failed"
    assert set(public["phases"]) == ({"daily"} if missing == "rate" else {"rate"})


def test_both_public_exemption_phases_pass(tmp_path, monkeypatch):
    result, report = _run_qualified_main(tmp_path, monkeypatch)
    public = report["results"]["quota.public_information_exempt"]
    assert result == 0 and report["passed"] is True
    assert public["state"] == "passed" and set(public["phases"]) == {"rate", "daily"}


def test_nonzero_cleanup_fails_successful_qualification(tmp_path, monkeypatch):
    result, report = _run_qualified_main(tmp_path, monkeypatch, cleanup_code=1)
    assert result == 1 and report["passed"] is False
    assert report["cleanup"] == "failed" and report["error_category"] == "cleanup_failed"


def test_nonzero_cleanup_preserves_qualification_failure_category(tmp_path, monkeypatch):
    result, report = _run_qualified_main(tmp_path, monkeypatch, cleanup_code=1, protected=(False, True))
    assert result == 1 and report["cleanup"] == "failed"
    assert report["error_category"] == "mandatory_check_failed"
