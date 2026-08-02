from pathlib import Path

import relay
from api.v1.compute_provider import DistributedApiV1ComputeProvider
from utils.inference_timeout import (
    DEFAULT_INFERENCE_TIMEOUT_SECONDS,
    DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS,
    INFERENCE_RESPONSE_GRACE_SECONDS,
)
from utils.networking import relay_client


def test_upgraded_inference_deadlines_preserve_legacy_300_second_boundary():
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert DEFAULT_INFERENCE_TIMEOUT_SECONDS == 480.0
    assert INFERENCE_RESPONSE_GRACE_SECONDS == 5.0
    assert DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS == 485.0
    assert relay.DEFAULT_API_V1_REQUEST_DEADLINE_SECONDS == 480.0
    assert relay_client._API_V1_COMPATIBILITY_REQUEST_DEADLINE_SECONDS == 300.0
    assert DistributedApiV1ComputeProvider("https://relay.example").timeout_seconds == 485.0
    assert "RELAY_RESPONSE_COMPATIBILITY_FALLBACK_MS = 485000" in chat_js
    assert "RELAY_RESPONSE_PROPAGATION_GRACE_MS = 5000" in chat_js
    assert "RELAY_RESPONSE_COMPATIBILITY_FALLBACK_MS = 300000" not in chat_js


def test_short_operational_timeouts_are_not_inference_budget_aliases():
    assert relay.DEFAULT_SERVER_STALE_SECONDS == 30
    assert relay.DEFAULT_API_V1_POLL_WAIT_SECONDS == 10
    assert relay.DEFAULT_API_V1_LEASE_SECONDS == 30
    assert relay_client._API_V1_CONTROL_ACK_TIMEOUT_SECONDS == 2.0
    assert relay_client._API_V1_CLEANUP_BUDGET_SECONDS == 5.0


def test_request_at_301_seconds_is_still_inside_authoritative_budget():
    admitted_at = 1_000.0
    relay_deadline = admitted_at + DEFAULT_INFERENCE_TIMEOUT_SECONDS

    assert admitted_at + 301.0 < relay_deadline
    assert admitted_at + 479.999 < relay_deadline
    assert admitted_at + 480.0 >= relay_deadline
