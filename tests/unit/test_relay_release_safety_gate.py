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
    unmatched = []

    def fake_request(_base, path, method="GET"):
        if path != "/metrics":
            unmatched.append(path)
            return 404, ""
        return 200, VALID

    monkeypatch.setattr(gate, "request", fake_request)
    assert all(v["passed"] for v in gate.execute_metrics_checks("http://loopback").values())
    assert len(unmatched) == 2 * gate.UNMATCHED_BATCH_SIZE
    assert len(set(unmatched[:gate.UNMATCHED_BATCH_SIZE])) >= 1024
    assert len(set(unmatched[gate.UNMATCHED_BATCH_SIZE:])) >= 1024


def test_metrics_phase_ceilings_exceed_complete_probe_count():
    complete_probe_count = 2 * gate.UNMATCHED_BATCH_SIZE + gate.METRICS_SCRAPE_COUNT
    assert int(gate.METRICS_RATE_LIMIT.split("/", 1)[0]) > complete_probe_count
    assert int(gate.METRICS_DAILY_QUOTA.split("/", 1)[0]) > complete_probe_count


def test_lazy_bounded_unmatched_collectors_are_not_per_path_growth(monkeypatch):
    scrapes = 0

    def fake_request(_base, path, method="GET"):
        nonlocal scrapes
        if path != "/metrics":
            return 404, ""
        scrapes += 1
        if scrapes <= 2:
            return 200, VALID
        count = gate.UNMATCHED_BATCH_SIZE if scrapes == 3 else 2 * gate.UNMATCHED_BATCH_SIZE
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
        "batch_size": 1024,
        "total_unmatched_requests": 2048,
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
        count = 0 if scrapes < 3 else (
            gate.UNMATCHED_BATCH_SIZE if scrapes == 3 else 2 * gate.UNMATCHED_BATCH_SIZE
        )
        transformed = "".join(f'hits{{endpoint="digest-{number:064x}"}} 1\n' for number in range(count))
        return 200, VALID + transformed

    monkeypatch.setattr(gate, "request", fake_request)
    results = gate.execute_metrics_checks("http://loopback")
    assert results["metrics.no_raw_paths"]["passed"] is False
    assert results["metrics.bounded_unmatched_paths"]["passed"] is False


def test_capped_hashed_labels_fail_even_when_second_batch_stops_growing(monkeypatch):
    scrapes = 0

    def fake_request(_base, path, method="GET"):
        nonlocal scrapes
        if path != "/metrics":
            return 404, ""
        scrapes += 1
        capped = "" if scrapes < 3 else "".join(
            f'hits{{endpoint="{number:064x}"}} 1\n' for number in range(32)
        )
        return 200, VALID + capped

    monkeypatch.setattr(gate, "request", fake_request)
    results = gate.execute_metrics_checks("http://loopback")
    assert results["metrics.no_raw_paths"]["passed"] is False
    assert results["metrics.bounded_unmatched_paths"]["passed"] is True
    assert results["metrics.bounded_unmatched_paths"]["first_batch_growth"] == 32
    assert results["metrics.bounded_unmatched_paths"]["second_batch_growth"] == 0


@pytest.mark.parametrize("statuses,expected", [
    ([200] * 13 + [429], True), ([500] + [200] * 12 + [429], False),
    ([200] * 14, False), ([200] * 12 + [429, 429], False),
])
def test_public_exemption_uses_exact_get_and_head_paths(monkeypatch, statuses, expected):
    iterator = iter(statuses)
    calls = []

    def fake_request(_base, path, method="GET", body=None):
        calls.append((path, method, body))
        return next(iterator), ""

    monkeypatch.setattr(gate, "request", fake_request)
    results = gate.execute_public_exemption_check("http://loopback")
    assert all(v["passed"] for v in results.values()) is expected
    assert calls[-2:] == [(gate.PROTECTED_READ_PATH, "GET", None)] * 2


def test_public_and_quota_evidence_records_exact_privacy_safe_statuses(monkeypatch):
    statuses = iter([200] * 13 + [429])
    monkeypatch.setattr(gate, "request", lambda *_args, **_kwargs: (next(statuses), ""))
    public = gate.execute_public_exemption_check("http://loopback")["quota.public_information_exempt"]
    assert public["sentinel_status_codes"] == [200, 429]

    statuses = iter([400, 429])
    mutation = gate.execute_quota_check(
        "http://loopback", "quota.test", mutating=True,
    )["quota.test"]
    assert mutation["status_codes"] == [400, 429]


def test_registered_sentinel_observes_real_flask_limiter_routing():
    from flask import Flask, request
    from flask_limiter import Limiter

    app = Flask(__name__)
    public = set(gate.PUBLIC_INFORMATION_PATHS)
    Limiter(
        key_func=lambda: "release-safety-client",
        app=app,
        default_limits=["1/minute"],
        default_limits_exempt_when=lambda: request.method in {"GET", "HEAD"} and request.path in public,
        storage_uri="memory://",
    )
    for index, path in enumerate(gate.PUBLIC_INFORMATION_PATHS):
        app.add_url_rule(path, f"public-{index}", lambda: "public", methods=["GET", "HEAD"])
    app.add_url_rule(gate.PROTECTED_READ_PATH, "models", lambda: "models")

    client = app.test_client()
    assert [client.get(path).status_code for path in public for _ in range(2)] == [200] * 6
    assert [client.head(path).status_code for path in public for _ in range(2)] == [200] * 6
    assert [client.get(gate.PROTECTED_READ_PATH).status_code for _ in range(2)] == [200, 429]


def test_unrouted_requests_do_not_prove_real_flask_limiter_enforcement():
    from flask import Flask
    from flask_limiter import Limiter

    app = Flask(__name__)
    Limiter(key_func=lambda: "client", app=app, default_limits=["1/minute"], storage_uri="memory://")
    client = app.test_client()
    assert [client.get("/unrouted").status_code for _ in range(2)] == [404, 404]


@pytest.mark.parametrize("payload,expected", [('{"passed": true}', True), ('{"passed": false}', False),
                                                ("not-json", False)])
def test_in_image_public_predicate_inspection_fails_closed(monkeypatch, payload, expected):
    monkeypatch.setattr(gate, "docker_output", lambda *args: payload)
    assert gate.inspect_public_exemption_predicate("candidate") is expected


@pytest.mark.parametrize("mutating,first", [(False, 200), (True, 400)])
def test_protected_and_non_mutating_invalid_mutation_quota_probes(monkeypatch, mutating, first):
    statuses = iter([first, 429])
    calls = []

    def fake_request(_base, path, method="GET", body=None):
        calls.append((path, method, body))
        return next(statuses), ""

    monkeypatch.setattr(gate, "request", fake_request)
    result = gate.execute_quota_check("http://loopback", "quota.test", mutating=mutating)["quota.test"]
    assert result["passed"] is True
    if mutating:
        assert calls == [(gate.MUTATING_PATH, "POST", b"{}") for _ in range(2)]
    else:
        assert calls == [(gate.PROTECTED_READ_PATH, "GET", None) for _ in range(2)]


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
    phase_values = iter(["metrics", "public", "protected-rate", "protected-daily",
                         "mutating-rate", "mutating-daily"])
    monkeypatch.setattr(gate, "request", lambda *_args, **_kwargs: (200, ""))
    monkeypatch.setattr(gate, "execute_metrics_checks", lambda _base: {
        key: {"passed": True} for key in gate.EXPECTED_IDS if key.startswith("metrics.")
    })
    monkeypatch.setattr(gate, "execute_public_exemption_check", lambda _base: {
        "quota.public_information_exempt": {"passed": True},
    })
    monkeypatch.setattr(gate, "inspect_public_exemption_predicate", lambda _container: True)
    monkeypatch.setattr(gate, "execute_quota_check", lambda _base, limit_id, **_kwargs: {
        limit_id: {"passed": True},
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
    assert [call[-1] for call in calls if call[0] == "run"] == [image_id] * 6


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


def _run_qualified_main(tmp_path, monkeypatch, public=True, missing=None, cleanup_code=0,
                        failed_id=None):
    values = iter(["sha256:" + "b" * 64, "a" * 40, "amd64", "metrics", "public",
                   "protected-rate", "protected-daily", "mutating-rate", "mutating-daily"])
    monkeypatch.setattr(gate, "request", lambda *_a, **_k: (200, ""))
    monkeypatch.setattr(gate, "execute_metrics_checks", lambda _base: {
        key: {"passed": True} for key in gate.EXPECTED_IDS if key.startswith("metrics.")
    })
    monkeypatch.setattr(gate, "execute_public_exemption_check", lambda _base: (
        {} if missing == "quota.public_information_exempt" else
        {"quota.public_information_exempt": {"passed": public}}
    ))
    monkeypatch.setattr(gate, "execute_quota_check", lambda _base, limit_id, **_kwargs: (
        {} if missing == limit_id else {limit_id: {"passed": limit_id != failed_id}}
    ))
    monkeypatch.setattr(gate, "inspect_public_exemption_predicate", lambda _container: True)

    def cleanup(*_args, **_kwargs):
        return subprocess.CompletedProcess([], cleanup_code)

    return _run_main(tmp_path, monkeypatch, lambda *_a: next(values), cleanup)


def test_public_exemption_failure_is_mandatory(tmp_path, monkeypatch):
    result, report = _run_qualified_main(tmp_path, monkeypatch, public=False)
    public = report["results"]["quota.public_information_exempt"]
    assert result == 1 and report["error_category"] == "mandatory_check_failed"
    assert public["state"] == "failed"


@pytest.mark.parametrize("missing", sorted(gate.EXPECTED_IDS - {
    "metrics.valid_instrumentation", "metrics.no_flask_defaults", "metrics.no_raw_paths",
    "metrics.bounded_unmatched_paths",
}))
def test_missing_quota_result_fails_closed(tmp_path, monkeypatch, missing):
    result, report = _run_qualified_main(tmp_path, monkeypatch, missing=missing)
    assert result == 1 and report["results"][missing]["passed"] is False
    assert report["results"][missing]["state"] == "not_run"


def test_all_independent_quota_phases_pass(tmp_path, monkeypatch):
    result, report = _run_qualified_main(tmp_path, monkeypatch)
    public = report["results"]["quota.public_information_exempt"]
    assert result == 0 and report["passed"] is True
    assert public["state"] == "passed"


@pytest.mark.parametrize("failed_id", [
    "quota.mutating_rate_limited", "quota.mutating_daily_limited",
])
def test_false_mutating_quota_result_fails_qualification(tmp_path, monkeypatch, failed_id):
    result, report = _run_qualified_main(tmp_path, monkeypatch, failed_id=failed_id)
    assert result == 1 and report["error_category"] == "mandatory_check_failed"
    assert report["results"][failed_id] == {"passed": False, "state": "failed"}


def test_nonzero_cleanup_fails_successful_qualification(tmp_path, monkeypatch):
    result, report = _run_qualified_main(tmp_path, monkeypatch, cleanup_code=1)
    assert result == 1 and report["passed"] is False
    assert report["cleanup"] == "failed" and report["error_category"] == "cleanup_failed"


def test_nonzero_cleanup_preserves_qualification_failure_category(tmp_path, monkeypatch):
    result, report = _run_qualified_main(
        tmp_path, monkeypatch, cleanup_code=1, failed_id="quota.protected_rate_limited"
    )
    assert result == 1 and report["cleanup"] == "failed"
    assert report["error_category"] == "mandatory_check_failed"
