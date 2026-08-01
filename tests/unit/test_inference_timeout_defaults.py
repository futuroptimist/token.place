from pathlib import Path

import relay
from api.v1.compute_provider import DistributedApiV1ComputeProvider
from inference_timeout import (
    API_V1_TIMEOUT_PROPAGATION_GRACE_SECONDS,
    DEFAULT_API_V1_INFERENCE_TIMEOUT_SECONDS,
    DEFAULT_API_V1_OUTER_TIMEOUT_SECONDS,
)
from utils.networking import relay_client


def test_production_inference_timeout_defaults_are_consistent() -> None:
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert DEFAULT_API_V1_INFERENCE_TIMEOUT_SECONDS == 480.0
    assert API_V1_TIMEOUT_PROPAGATION_GRACE_SECONDS == 5.0
    assert DEFAULT_API_V1_OUTER_TIMEOUT_SECONDS == 485.0
    assert relay.DEFAULT_API_V1_REQUEST_DEADLINE_SECONDS == 480.0
    assert DistributedApiV1ComputeProvider("https://relay.example").timeout_seconds == 485.0
    assert relay_client._API_V1_COMPATIBILITY_REQUEST_DEADLINE_SECONDS == 480.0
    assert "RELAY_RESPONSE_POLL_TIMEOUT_MS = 485000" in chat_js


def test_duration_between_old_and_new_deadlines_remains_admissible() -> None:
    simulated_completion_seconds = 420.0

    assert 300.0 < simulated_completion_seconds < DEFAULT_API_V1_INFERENCE_TIMEOUT_SECONDS
    assert simulated_completion_seconds < DEFAULT_API_V1_OUTER_TIMEOUT_SECONDS


def test_short_operational_timeouts_are_not_inference_budgets() -> None:
    assert relay.DEFAULT_SERVER_STALE_SECONDS == 30
    assert relay.DEFAULT_API_V1_POLL_WAIT_SECONDS == 10
    assert relay.DEFAULT_API_V1_LEASE_SECONDS == 30
