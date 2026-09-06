from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path("scripts/relay_release_safety_gate.py")
SPEC = importlib.util.spec_from_file_location("relay_release_safety_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class Candidate:
    def __init__(self, *, bounded_metrics: bool, public_exempt: bool) -> None:
        self.bounded_metrics = bounded_metrics
        self.public_exempt = public_exempt
        self.protected_calls = 0
        self.public_calls = 0
        self.paths: list[str] = []

    def fetch(self, path: str):
        if path == "/metrics":
            lines = ["tokenplace_instrumentation_up 1"]
            if not self.bounded_metrics:
                lines.extend(
                    f'flask_http_request_total{{path="{raw}"}} 1' for raw in self.paths
                )
            return gate.HttpResponse(200, "\n".join(lines) + "\n")
        if path.startswith("/__release_safety_"):
            self.paths.append(path)
            return gate.HttpResponse(404, "")
        if path in {"/", "/api/v1/meta", "/api/v1/version"}:
            self.public_calls += 1
            status = 200 if self.public_exempt or self.public_calls == 1 else 429
            return gate.HttpResponse(status, "")
        if path == "/api/v1/models":
            self.protected_calls += 1
            return gate.HttpResponse(200 if self.protected_calls == 1 else 429, "")
        raise AssertionError(path)


def failures(candidate: Candidate) -> set[str]:
    return {
        check_id
        for check_id, result in gate.evaluate_candidate(candidate.fetch).items()
        if result["passed"] is not True
    }


def test_behavior_equivalent_recovery_candidate_passes_without_ancestry() -> None:
    """The 6c39adc behavior passes; no Git ancestry is an input to evaluation."""
    assert failures(Candidate(bounded_metrics=True, public_exempt=True)) == set()


def test_historical_vulnerable_behavior_fails_both_incident_contracts() -> None:
    failed = failures(Candidate(bounded_metrics=False, public_exempt=False))
    assert "bounded_metrics.no_flask_defaults" in failed
    assert "bounded_metrics.no_raw_paths" in failed
    assert "bounded_metrics.series_growth" in failed
    assert "quota.public_information_exempt" in failed


@pytest.mark.parametrize(
    ("bounded_metrics", "public_exempt", "expected"),
    [
        (False, True, "bounded_metrics.no_flask_defaults"),
        (True, False, "quota.public_information_exempt"),
    ],
)
def test_disabling_either_recovery_behavior_fails_independently(
    bounded_metrics: bool, public_exempt: bool, expected: str
) -> None:
    assert expected in failures(
        Candidate(bounded_metrics=bounded_metrics, public_exempt=public_exempt)
    )


def test_protected_route_must_still_be_limited() -> None:
    candidate = Candidate(bounded_metrics=True, public_exempt=True)
    candidate.fetch = lambda path, original=candidate.fetch: (
        gate.HttpResponse(200, "") if path == "/api/v1/models" else original(path)
    )
    assert "quota.protected_route_limited" in failures(candidate)


def test_contract_fails_closed_when_requirement_is_missing_or_malformed(
    tmp_path: Path,
) -> None:
    contract = json.loads(Path("config/relay_release_safety_contract.json").read_text())
    contract["requirements"].pop()
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(gate.GateError, match="every implemented mandatory check"):
        gate.load_contract(path)


def test_digest_or_source_revision_mismatch_fails_before_container_run(
    monkeypatch,
) -> None:
    outputs = iter(["sha256:candidate", "different-source"])
    monkeypatch.setattr(gate, "_run", lambda *args, **kwargs: next(outputs))
    args = type(
        "Args",
        (),
        {
            "contract": Path("config/relay_release_safety_contract.json"),
            "runtime": "docker",
            "image": "candidate:test",
            "source_commit": "expected-source",
            "release_ref": "main",
            "release_base": "main",
        },
    )()
    with pytest.raises(gate.GateError, match="source revision"):
        gate.qualify(args)
