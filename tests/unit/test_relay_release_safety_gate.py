from __future__ import annotations

import importlib.util
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
    bounded_metrics = True
    public_exempt = True
    quota_used = 0
    paths: set[str] = set()

    def log_message(self, *_args):
        pass

    def _reply(self, status: int, body: str = "ok") -> None:
        self.send_response(status)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body.encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        public = self.path in {"/", "/api/v1/meta", "/api/v1/version"}
        if self.path == "/metrics":
            lines = ["# TYPE tokenplace_relay_requests_total counter", 'tokenplace_relay_requests_total{route="other"} 1']
            if not self.bounded_metrics:
                lines.append('flask_http_request_total{path="/raw-path"} 1')
                lines.extend(f'tokenplace_request_total{{path="{path}"}} 1' for path in sorted(self.paths))
            self._reply(200, "\n".join(lines) + "\n")
            return
        if self.path.startswith("/release-safety-unmatched-"):
            self.paths.add(self.path)
            self._reply(404)
            return
        if public and self.public_exempt:
            self._reply(200)
            return
        if type(self).quota_used:
            self._reply(429)
            return
        type(self).quota_used += 1
        self._reply(200)


@pytest.fixture()
def candidate_server():
    servers = []

    def start(*, bounded_metrics=True, public_exempt=True):
        handler = type("ConfiguredCandidate", (CandidateHandler,), {
            "bounded_metrics": bounded_metrics, "public_exempt": public_exempt,
            "quota_used": 0, "paths": set(),
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


def test_behavior_equivalent_recovery_passes_without_ancestry(candidate_server):
    results = gate.qualify(candidate_server())
    assert all(result["passed"] is True for result in results.values())


def test_historical_vulnerable_behavior_fails(candidate_server):
    with pytest.raises(gate.GateFailure, match="metrics.*quota.public_information_exempt"):
        gate.qualify(candidate_server(bounded_metrics=False, public_exempt=False))


def test_disabling_bounded_metrics_fails_independently(candidate_server):
    with pytest.raises(gate.GateFailure, match="metrics"):
        gate.qualify(candidate_server(bounded_metrics=False))


def test_disabling_public_quota_exemptions_fails_independently(candidate_server):
    with pytest.raises(gate.GateFailure, match="quota.public_information_exempt"):
        gate.qualify(candidate_server(public_exempt=False))


def test_protected_route_must_still_be_limited(candidate_server, monkeypatch):
    monkeypatch.setattr(gate, "request", lambda base, path, method="GET": (200, "tokenplace_metric 1\n"))
    with pytest.raises(gate.GateFailure, match="quota.protected_route_limited"):
        gate.qualify(candidate_server())


def test_missing_contract_check_fails_closed(candidate_server, tmp_path):
    contract = tmp_path / "contract.json"
    contract.write_text('{"schema_version": 1, "requirements": []}')
    with pytest.raises(gate.GateFailure, match="empty"):
        gate.qualify(candidate_server(), contract_path=contract)


def test_registry_digest_mismatch_fails_closed():
    expected = "sha256:" + "a" * 64
    with pytest.raises(gate.GateFailure, match="digest"):
        gate.validate_candidate_identity("abc1234", "abc1234", expected, '["repo@sha256:' + "b" * 64 + '"]')
