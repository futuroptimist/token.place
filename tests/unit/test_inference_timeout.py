from pathlib import Path

import relay
from api.v1.compute_provider import DistributedApiV1ComputeProvider
from utils.inference_timeout import (
    DEFAULT_INFERENCE_COMPLETION_TIMEOUT_SECONDS,
    DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS,
    INFERENCE_TIMEOUT_RESPONSE_GRACE_SECONDS,
)
from utils.networking.relay_client import RelayClient


ROOT = Path(__file__).resolve().parents[2]


def test_production_inference_timeout_defaults_are_consistent(monkeypatch):
    monkeypatch.delenv(relay.API_V1_REQUEST_DEADLINE_SECONDS_ENV, raising=False)
    assert DEFAULT_INFERENCE_COMPLETION_TIMEOUT_SECONDS == 480.0
    assert relay._api_v1_request_deadline_seconds() == 480.0
    assert RelayClient._api_v1_initial_deadline_from_metadata({}, now=0.0) == 480.0
    assert INFERENCE_TIMEOUT_RESPONSE_GRACE_SECONDS == 5.0
    assert DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS == 485.0
    assert DistributedApiV1ComputeProvider("https://relay.example").timeout_seconds == 485.0
    assert "RELAY_RESPONSE_POLL_TIMEOUT_MS = 485000" in (ROOT / "static/chat.js").read_text()


def test_completion_between_old_and_new_deadline_remains_valid(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr("utils.networking.relay_client.time.monotonic", lambda: now["value"])
    deadline = RelayClient._api_v1_initial_deadline_from_metadata({})
    now["value"] = 360.0
    # Deadline refreshes can only shorten the original deadline; a 360-second
    # completion therefore remains live without sleeping in the test.
    assert RelayClient._api_v1_deadline_after_response(deadline, {}) == 480.0
    assert now["value"] < deadline


def test_short_operational_timeouts_are_not_inference_budgets():
    assert relay.DEFAULT_SERVER_STALE_SECONDS == 30
    assert relay.DEFAULT_API_V1_POLL_WAIT_SECONDS == 10
    assert relay.DEFAULT_API_V1_LEASE_SECONDS == 30
