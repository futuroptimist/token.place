from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path("scripts/relay_release_safety_gate.py")
SPEC = importlib.util.spec_from_file_location("relay_release_safety_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class CandidateHandler(BaseHTTPRequestHandler):
    mode = "recovered"
    unmatched: set[str] = set()
    protected_requests = 0
    public_requests = 0

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        if self.path == "/metrics":
            lines = ["tokenplace_relay_requests_total{route=\"other\"} 1"]
            if self.mode in {"vulnerable", "metrics_disabled"}:
                lines.append('flask_http_request_total{method="GET",path="/metrics"} 1')
                lines.extend(
                    f'flask_http_request_duration_seconds_count{{path="{path}"}} 1'
                    for path in sorted(self.unmatched)
                )
            self._reply(200, "\n".join(lines) + "\n")
            return
        if self.path.startswith("/__release_safety_probe_"):
            self.unmatched.add(self.path)
            self._reply(404)
            return
        if self.path in {"/", "/api/v1/meta", "/api/v1/version"}:
            type(self).public_requests += 1
            limited = self.mode in {"vulnerable", "quota_disabled"} and type(self).public_requests > 1
            self._reply(429 if limited else 200)
            return
        if self.path == "/api/v1/models":
            type(self).protected_requests += 1
            limited = self.mode == "quota_disabled" or type(self).protected_requests > 1
            # quota_disabled models the dangerous broad limiter exemption.
            self._reply(200 if self.mode == "quota_disabled" else (429 if limited else 200))
            return
        self._reply(200)

    def _reply(self, status: int, body: str = "") -> None:
        self.send_response(status)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body.encode())

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def candidate_url():
    CandidateHandler.unmatched = set()
    CandidateHandler.protected_requests = 0
    CandidateHandler.public_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), CandidateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _failed(candidate_url: str, mode: str) -> set[str]:
    CandidateHandler.mode = mode
    contract = gate.load_contract(Path("config/relay_release_safety_contract.json"))
    results = gate.evaluate(candidate_url, contract)
    return {name for name, result in results.items() if not result["passed"]}


def test_behavior_equivalent_recovery_candidate_passes_without_ancestry(candidate_url):
    assert _failed(candidate_url, "recovered") == set()


def test_historical_vulnerable_candidate_fails_both_incident_contracts(candidate_url):
    failed = _failed(candidate_url, "vulnerable")
    assert "metrics.default_families_absent" in failed
    assert "metrics.request_path_labels_bounded" in failed
    assert "metrics.unmatched_path_series_bounded" in failed
    assert "quota.public_information_reads_exempt" in failed


def test_disabling_bounded_metrics_fails_closed(candidate_url):
    failed = _failed(candidate_url, "metrics_disabled")
    assert failed == {
        "metrics.default_families_absent",
        "metrics.request_path_labels_bounded",
        "metrics.unmatched_path_series_bounded",
    }


def test_disabling_quota_exemptions_and_protected_limit_fails_closed(candidate_url):
    failed = _failed(candidate_url, "quota_disabled")
    assert failed == {
        "quota.public_information_reads_exempt",
        "quota.protected_route_enforced",
    }


def test_contract_rejects_missing_check(tmp_path):
    contract = json.loads(Path("config/relay_release_safety_contract.json").read_text())
    contract["required_checks"].pop()
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="every implemented check"):
        gate.load_contract(path)


def test_image_identity_must_be_immutable_and_can_be_matched(monkeypatch):
    class Result:
        stdout = "sha256:" + "a" * 64 + "\n"

    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: Result())
    assert gate.inspect_image("docker", "candidate") == "sha256:" + "a" * 64


def test_image_identity_mismatch_fails_closed():
    with pytest.raises(RuntimeError, match="does not match"):
        gate.require_expected_image_id("sha256:" + "a" * 64, "sha256:" + "b" * 64)
