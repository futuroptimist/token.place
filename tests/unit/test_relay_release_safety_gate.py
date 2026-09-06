from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import relay_release_safety_gate as gate


CONTRACT = gate.load_contract(Path("config/relay_release_safety_contract.json"))


def _fixture_get(*, bounded_metrics: bool, public_exempt: bool, protected_limited: bool):
    unmatched: list[str] = []
    public_calls = protected_calls = 0

    def get(_base_url: str, path: str) -> tuple[int, str]:
        nonlocal public_calls, protected_calls
        if path == "/metrics":
            lines = ['tokenplace_http_requests_total{route="other"} 1']
            if not bounded_metrics:
                lines = [f'flask_http_request_total{{path="{item}"}} 1' for item in unmatched] or ["flask_exporter_info 1"]
            return 200, "\n".join(lines) + "\n"
        if path.startswith("/__release_safety_probe_"):
            unmatched.append(path)
            return 404, ""
        if path in ("/", "/api/v1/meta", "/api/v1/version"):
            public_calls += 1
            return (200 if public_exempt or public_calls <= 2 else 429), ""
        if path == "/api/v1/models":
            protected_calls += 1
            return (429 if protected_limited and protected_calls >= 3 else 200), ""
        raise AssertionError(path)

    return get


def test_recovered_behavior_passes_without_commit_ancestry(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_get", _fixture_get(bounded_metrics=True, public_exempt=True, protected_limited=True))
    assert all(gate.run_contract("http://candidate", CONTRACT).values())


@pytest.mark.parametrize(
    ("behavior", "failed_checks"),
    [
        ({"bounded_metrics": False, "public_exempt": False, "protected_limited": True}, {"metrics.no_flask_defaults", "metrics.no_raw_paths", "metrics.bounded_unmatched_paths", "quota.public_information_exempt"}),
        ({"bounded_metrics": False, "public_exempt": True, "protected_limited": True}, {"metrics.no_flask_defaults", "metrics.no_raw_paths", "metrics.bounded_unmatched_paths"}),
        ({"bounded_metrics": True, "public_exempt": False, "protected_limited": True}, {"quota.public_information_exempt"}),
        ({"bounded_metrics": True, "public_exempt": True, "protected_limited": False}, {"quota.protected_routes_limited"}),
    ],
)
def test_historical_and_independently_disabled_behaviors_fail(monkeypatch, behavior, failed_checks) -> None:
    monkeypatch.setattr(gate, "_get", _fixture_get(**behavior))
    results = gate.run_contract("http://candidate", CONTRACT)
    assert {check for check, passed in results.items() if not passed} == failed_checks


def test_contract_fails_closed_for_missing_checks(tmp_path: Path) -> None:
    contract = json.loads(Path("config/relay_release_safety_contract.json").read_text())
    contract["requirements"].pop()
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="contract/runner check mismatch"):
        gate.load_contract(path)


def test_candidate_digest_is_required() -> None:
    source = Path("scripts/relay_release_safety_gate.py").read_text()
    assert 'parser.add_argument("--candidate-digest", required=True)' in source
