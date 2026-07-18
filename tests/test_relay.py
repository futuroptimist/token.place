import pytest
import time
import threading
import base64
import json
from pathlib import Path
from flask import Flask
import sys
import os
from datetime import datetime, timedelta
import relay as relay_module
from utils.networking.relay_client import RelayClient

# Add project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from relay import app

# Import the global dictionaries from relay to inspect/manipulate state if needed
# Be cautious with direct manipulation in tests, prefer using API endpoints
from relay import (
    known_servers,
    client_inference_requests,
    client_pending_request_ids,
    client_terminal_request_ids,
    client_responses,
    streaming_sessions,
    streaming_sessions_by_client,
)

# Generate dummy keys for testing
# (You might want to use the generate_keys function from encrypt.py if needed)
DUMMY_SERVER_PUB_KEY = base64.b64encode(b"server_public_key_123").decode('utf-8')
DUMMY_CLIENT_PUB_KEY = base64.b64encode(b"client_public_key_456").decode('utf-8')

# Intentionally retained only as negative assertions for removed response aliases.
REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS = "legacyConfiguredUpstreamServers"
REMOVED_DIAGNOSTICS_CONFIGURED_UPSTREAM_ALIAS = "legacy_configured_upstream_servers"


@pytest.fixture
def client():
    """Create a Flask test client fixture."""
    app.config['TESTING'] = True
    previous_legacy_flag = os.environ.get("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES")
    os.environ["TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES"] = "1"
    # Reset state before each test
    known_servers.clear()
    relay_module.server_round_robin_next_index = 0
    relay_module.api_v1_filtered_round_robin_next_positions.clear()
    client_inference_requests.clear()
    client_pending_request_ids.clear()
    relay_module.client_pending_request_deadlines.clear()
    client_terminal_request_ids.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()
    relay_module.api_v1_recently_unregistered_servers.clear()
    relay_module.api_v1_control_tombstones.clear()
    for limiter in app.extensions.get("limiter", set()):
        storage = getattr(getattr(limiter, "limiter", None), "storage", None)
        if storage is None:
            continue
        if hasattr(storage, "reset"):
            storage.reset()
        elif hasattr(storage, "clear"):
            storage.clear()
    control_plane_limiter = app.config.get("relay_control_plane_rate_limiter")
    control_plane_storage = getattr(control_plane_limiter, "storage", None)
    if control_plane_storage is not None:
        if hasattr(control_plane_storage, "reset"):
            control_plane_storage.reset()
        elif hasattr(control_plane_storage, "clear"):
            control_plane_storage.clear()

    with app.test_client() as client:
        yield client

    if previous_legacy_flag is None:
        os.environ.pop("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", None)
    else:
        os.environ["TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES"] = previous_legacy_flag
    # Clean up state after test (optional, as fixture resets before)
    known_servers.clear()
    relay_module.server_round_robin_next_index = 0
    relay_module.api_v1_filtered_round_robin_next_positions.clear()
    client_inference_requests.clear()
    client_pending_request_ids.clear()
    relay_module.client_pending_request_deadlines.clear()
    client_terminal_request_ids.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()
    relay_module.api_v1_recently_unregistered_servers.clear()
    relay_module.api_v1_control_tombstones.clear()
    for limiter in app.extensions.get("limiter", set()):
        storage = getattr(getattr(limiter, "limiter", None), "storage", None)
        if storage is None:
            continue
        if hasattr(storage, "reset"):
            storage.reset()
        elif hasattr(storage, "clear"):
            storage.clear()
    control_plane_limiter = app.config.get("relay_control_plane_rate_limiter")
    control_plane_storage = getattr(control_plane_limiter, "storage", None)
    if control_plane_storage is not None:
        if hasattr(control_plane_storage, "reset"):
            control_plane_storage.reset()
        elif hasattr(control_plane_storage, "clear"):
            control_plane_storage.clear()


def test_operational_endpoints_are_not_rate_limited_by_public_quota(client):
    """Health, liveness, metrics, and diagnostics stay outside user API quotas."""

    paths = ("/healthz", "/livez", "/metrics", "/relay/diagnostics")

    for path in paths:
        responses = [client.get(path) for _ in range(105)]
        assert [response.status_code for response in responses] == [200] * 105
        assert 429 not in {response.status_code for response in responses}


def test_api_v1_register_and_poll_are_not_rate_limited_by_public_quota(client, monkeypatch):
    """Authenticated compute-provider heartbeats stay outside the public API quota."""

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", "relay-token")
    monkeypatch.setattr(relay_module, "SERVER_REGISTRATION_TOKENS", ["relay-token"])
    payload = {"server_public_key": DUMMY_SERVER_PUB_KEY}
    headers = {"X-Relay-Server-Token": "relay-token"}

    register_responses = [
        client.post("/api/v1/relay/servers/register", json=payload, headers=headers)
        for _ in range(65)
    ]
    assert {response.status_code for response in register_responses} == {200}

    poll_responses = [
        client.post("/api/v1/relay/servers/poll", json=payload, headers=headers)
        for _ in range(65)
    ]
    assert {response.status_code for response in poll_responses} == {200}


def test_api_v1_next_server_round_robins_new_client_selections(client):
    """The relay returns only the next live API v1 compute node; clients own affinity."""

    server_a = _server_key("sticky-node-a")
    server_b = _server_key("sticky-node-b")
    server_c = _server_key("sticky-node-c")
    _register_api_v1_server(client, server_a)
    _register_api_v1_server(client, server_b)
    _register_api_v1_server(client, server_c)

    assert [_next_api_v1_server_key(client) for _ in range(7)] == [
        server_a,
        server_b,
        server_c,
        server_a,
        server_b,
        server_c,
        server_a,
    ]


def test_two_api_v1_nodes_poll_and_round_robin_without_control_plane_429(client, monkeypatch):
    """Two nodes behind one source can poll while next-server rotation remains intact."""

    monkeypatch.setenv("TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS", "0")
    server_a = _server_key("rate-node-a")
    server_b = _server_key("rate-node-b")
    _register_api_v1_server(client, server_a)
    _register_api_v1_server(client, server_b)

    for _ in range(65):
        poll_a = client.post(
            '/api/v1/relay/servers/poll', json={'server_public_key': server_a}
        )
        poll_b = client.post(
            '/api/v1/relay/servers/poll', json={'server_public_key': server_b}
        )
        assert poll_a.status_code == 200
        assert poll_b.status_code == 200

    assert [_next_api_v1_server_key(client) for _ in range(4)] == [
        server_a,
        server_b,
        server_a,
        server_b,
    ]
    diagnostics = client.get('/relay/diagnostics')
    assert diagnostics.status_code == 200
    registered = diagnostics.get_json()['registered_compute_nodes']
    assert {node['server_public_key'] for node in registered} == {server_a, server_b}


def test_api_v1_response_submissions_do_not_use_public_quota(client):
    """Encrypted response submissions have a higher control-plane budget."""

    responses = [
        client.post(
            '/api/v1/relay/responses',
            json=_api_v1_response_payload(f'rate-response-{index}'),
        )
        for index in range(65)
    ]

    assert {response.status_code for response in responses} == {200}


def test_api_v1_client_relay_read_paths_are_not_rate_limited_by_public_quota(client):
    """Client discovery and response polling stay outside the public API quota."""

    _register_api_v1_server(client, DUMMY_SERVER_PUB_KEY)
    client_pending_request_ids[DUMMY_CLIENT_PUB_KEY] = {"request-1": time.time()}

    next_responses = [client.get("/api/v1/relay/servers/next") for _ in range(65)]
    assert {response.status_code for response in next_responses} == {200}

    retrieve_responses = [
        client.post(
            "/api/v1/relay/responses/retrieve",
            json={"client_public_key": DUMMY_CLIENT_PUB_KEY, "request_id": "request-1"},
        )
        for _ in range(65)
    ]
    assert {response.status_code for response in retrieve_responses} == {202}


def test_inference_endpoint_removed(client):
    """Ensure deprecated /inference endpoint is unavailable."""
    response = client.post("/inference", json={})
    assert response.status_code == 404

# --- Test /next_server ---

def test_next_server_no_servers(client):
    """Test /next_server when no servers are registered."""
    response = client.get("/next_server")
    assert response.status_code == 503
    data = response.get_json()
    assert 'error' in data
    assert data['error']['message'] == 'No servers available'
    assert data['error']['code'] == 503


def test_api_v1_next_server_no_registered_compute_nodes_message(client):
    """API v1 relay reports no registered compute nodes with a stable error code."""

    response = client.get("/api/v1/relay/servers/next")
    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["code"] == "no_registered_compute_nodes"
    assert (
        data["error"]["message"]
        == "No registered compute nodes are available on this relay."
    )


def test_next_server_one_server(client):
    """Test /next_server when one server is registered."""
    # Simulate server registration (directly modifying state for setup)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 10
    }

    response = client.get("/next_server")
    assert response.status_code == 200
    data = response.get_json()
    assert 'error' not in data
    assert 'server_public_key' in data
    assert data['server_public_key'] == DUMMY_SERVER_PUB_KEY


def test_next_server_evicts_stale_nodes(client):
    """Stale servers should be removed before /next_server selection."""
    stale_time = datetime.now() - timedelta(seconds=120)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": stale_time,
        "last_ping_duration": 10,
    }

    response = client.get("/next_server")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["error"]["message"] == "No servers available"
    assert DUMMY_SERVER_PUB_KEY not in known_servers


def _server_key(label):
    return base64.b64encode(f"server_public_key_{label}".encode()).decode("utf-8")


def _register_api_v1_server_without_capabilities(client, server_public_key):
    response = client.post(
        '/api/v1/relay/servers/register',
        json={'server_public_key': server_public_key},
    )
    assert response.status_code == 200
    return response


def _register_api_v1_server(client, server_public_key):
    return _register_api_v1_server_with_capabilities(client, server_public_key, _capabilities("8k-fast"))


def _capabilities(tier="8k-fast", models=None):
    tokens = 65536 if tier == "64k-full" else 8192
    return {
        "api_version": "v1",
        "supported_model_ids": models or ["qwen3-8b-instruct"],
        "active_context_tier": tier,
        "maximum_total_context_tokens": tokens,
        "default_output_token_reservation": 1024,
        "maximum_output_tokens": 2048,
        "max_concurrency": 1,
        "backend_class": "cuda" if tier == "64k-full" else "metal",
    }


def _register_api_v1_server_with_capabilities(client, server_public_key, capabilities):
    response = client.post(
        '/api/v1/relay/servers/register',
        json={'server_public_key': server_public_key, 'capabilities': capabilities},
    )
    assert response.status_code == 200
    return response


def _api_v1_registered_control_payload(client, server_public_key, *, capabilities=None):
    if capabilities is None:
        response = _register_api_v1_server_without_capabilities(client, server_public_key)
    else:
        response = _register_api_v1_server_with_capabilities(client, server_public_key, capabilities)
    payload = {'server_public_key': server_public_key}
    credential = response.get_json().get('control_credential')
    if credential:
        payload['control_credential'] = credential
    return payload


def _next_api_v1_server_key(client):
    response = client.get('/api/v1/relay/servers/next')
    assert response.status_code == 200
    return response.get_json()['server_public_key']


def test_api_v1_capability_registration_and_tier_selection(client):
    fast = _server_key("fast-cap")
    full = _server_key("full-cap")
    _register_api_v1_server_with_capabilities(client, fast, _capabilities("8k-fast"))
    _register_api_v1_server_with_capabilities(client, full, _capabilities("64k-full"))

    fast_response = client.get("/api/v1/relay/servers/next?context_tier=8k-fast")
    assert fast_response.status_code == 200
    fast_payload = fast_response.get_json()
    assert fast_payload["server_public_key"] == fast
    assert fast_payload["selected_context_tier"] == "8k-fast"
    assert fast_payload["selected_context_window_tokens"] == 8192

    full_response = client.get("/api/v1/relay/servers/next?context_tier=64k-full")
    assert full_response.status_code == 200
    assert full_response.get_json()["server_public_key"] == full


def test_api_v1_old_node_compatibility_and_64k_can_satisfy_8k(client):
    old = _server_key("old-node")
    full = _server_key("full-only")
    old_payload = _api_v1_registered_control_payload(client, old, capabilities=_capabilities("8k-fast"))
    _register_api_v1_server_with_capabilities(client, full, _capabilities("64k-full"))

    assert client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()["server_public_key"] == old
    client.post("/api/v1/relay/servers/unregister", json=old_payload)
    assert client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()["server_public_key"] == full



def test_api_v1_best_fit_prefers_8k_for_repeated_8k_requests(client):
    fast = _server_key("best-fit-fast")
    full = _server_key("best-fit-full")
    _register_api_v1_server_with_capabilities(client, fast, _capabilities("8k-fast"))
    _register_api_v1_server_with_capabilities(client, full, _capabilities("64k-full"))

    payloads = [client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json() for _ in range(4)]

    assert [payload["server_public_key"] for payload in payloads] == [fast] * 4
    assert {payload["selection_policy"] for payload in payloads} == {relay_module.API_V1_SELECTION_POLICY}
    assert {payload["spillover"] for payload in payloads} == {False}
    assert payloads[-1]["eligible_tier_counts"] == {"8k-fast": 1, "64k-full": 1}


def test_api_v1_best_fit_64k_requests_never_route_to_8k(client):
    fast = _server_key("best-fit-64-fast")
    full = _server_key("best-fit-64-full")
    _register_api_v1_server_with_capabilities(client, fast, _capabilities("8k-fast"))
    _register_api_v1_server_with_capabilities(client, full, _capabilities("64k-full"))

    payloads = [client.get("/api/v1/relay/servers/next?context_tier=64k-full").get_json() for _ in range(4)]

    assert [payload["server_public_key"] for payload in payloads] == [full] * 4
    assert {payload["selected_context_tier"] for payload in payloads} == {"64k-full"}


def test_api_v1_best_fit_spillover_when_smaller_tier_unavailable_or_saturated(client, monkeypatch):
    monkeypatch.setenv(relay_module.API_V1_MAX_QUEUE_DEPTH_ENV, "1")
    fast = _server_key("spillover-fast")
    full = _server_key("spillover-full")
    _register_api_v1_server_with_capabilities(client, full, _capabilities("64k-full"))

    no_fast_payload = client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()
    assert no_fast_payload["server_public_key"] == full
    assert no_fast_payload["spillover"] is True
    assert no_fast_payload["spillover_reason"] == "no_smaller_eligible_node_available"

    _register_api_v1_server_with_capabilities(client, fast, _capabilities("8k-fast"))
    healthy_payload = client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()
    assert healthy_payload["server_public_key"] == fast
    assert healthy_payload["spillover"] is False

    _queue_api_v1_request(client, server_public_key=fast, request_id="req-saturate-fast")
    saturated_payload = client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()
    assert saturated_payload["server_public_key"] == full
    assert saturated_payload["spillover"] is True


def test_api_v1_best_fit_least_loaded_within_same_tier(client):
    busy = _server_key("least-loaded-busy")
    idle = _server_key("least-loaded-idle")
    for server in (busy, idle):
        _register_api_v1_server_with_capabilities(client, server, _capabilities("8k-fast"))
    _queue_api_v1_request(client, server_public_key=busy, request_id="req-busy-queue")

    payload = client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()

    assert payload["server_public_key"] == idle
    assert payload["selected_queue_depth"] == 0
    assert payload["selected_load_score"] == 0


def test_api_v1_best_fit_least_in_flight_within_64k_tier(client):
    busy = _server_key("least-inflight-busy")
    idle = _server_key("least-inflight-idle")
    for server in (busy, idle):
        _register_api_v1_server_with_capabilities(client, server, _capabilities("64k-full"))
    known_servers[busy]["capabilities"]["max_concurrency"] = 2
    known_servers[busy]["api_v1_in_flight_requests"] = {
        "req-in-flight": {"expires_at": time.monotonic() + 60, "client_public_key": DUMMY_CLIENT_PUB_KEY}
    }

    payload = client.get("/api/v1/relay/servers/next?context_tier=64k-full").get_json()

    assert payload["server_public_key"] == idle
    assert payload["selected_in_flight_count"] == 0


def test_api_v1_scheduler_helper_uses_only_safe_metadata():
    payload = {
        relay_module.API_V1_SERVER_MARKER: True,
        "last_ping": datetime.now(),
        "last_ping_duration": 30,
        "capabilities": _capabilities("8k-fast", ["safe-model"]),
        "chat_history": "ciphertext-only-not-read",
        "messages": [{"content": "plaintext-looking-field-must-not-matter"}],
        "prompt": "must-not-matter",
    }

    candidate = relay_module._api_v1_scheduler_candidate(
        "safe-metadata-node",
        payload,
        requested_model="safe-model",
        requested_context_tier="8k-fast",
        registration_index=0,
        now_monotonic=time.monotonic(),
    )

    assert candidate is not None
    assert candidate["tier"] == "8k-fast"
    unsafe_keys = {"chat_history", "ciphertext", "messages", "prompt", "content", "text"}
    assert unsafe_keys.isdisjoint(candidate)
    assert "payload" not in candidate
    assert "selected_server" in candidate

    def assert_safe_nested(value):
        if isinstance(value, dict):
            assert unsafe_keys.isdisjoint(value)
            for nested in value.values():
                assert_safe_nested(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_safe_nested(nested)

    assert_safe_nested(candidate["selected_server"])


def test_api_v1_scheduler_helper_rejects_unsafe_boolean_state_flags(client):
    for flag in ("failed", "recovering", "draining", "unregistering"):
        payload = {
            relay_module.API_V1_SERVER_MARKER: True,
            "last_ping": datetime.now(),
            "last_ping_duration": 30,
            "capabilities": _capabilities("8k-fast", ["safe-model"]),
            flag: True,
        }

        candidate = relay_module._api_v1_scheduler_candidate(
            f"unsafe-flag-{flag}",
            payload,
            requested_model="safe-model",
            requested_context_tier="8k-fast",
            registration_index=0,
            now_monotonic=time.monotonic(),
        )

        assert candidate is None


def test_api_v1_scheduler_helper_rejects_non_v1_capabilities(client):
    payload = {
        relay_module.API_V1_SERVER_MARKER: True,
        "last_ping": datetime.now(),
        "last_ping_duration": 30,
        "capabilities": {**_capabilities("8k-fast", ["safe-model"]), "api_version": "v2"},
    }

    candidate = relay_module._api_v1_scheduler_candidate(
        "wrong-api-version",
        payload,
        requested_model="safe-model",
        requested_context_tier="8k-fast",
        registration_index=0,
        now_monotonic=time.monotonic(),
    )

    assert candidate is None


def test_api_v1_next_keeps_long_polling_server_eligible_when_last_ping_is_stale(client, monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    server = _server_key("long-poll-eligible")
    _register_api_v1_server_with_capabilities(client, server, _capabilities("8k-fast"))
    known_servers[server]["last_ping"] = datetime.now() - timedelta(seconds=5)
    known_servers[server]["last_ping_duration"] = 1
    known_servers[server]["polling_until_monotonic"] = time.monotonic() + 30

    response = client.get("/api/v1/relay/servers/next")

    assert response.status_code == 200
    assert response.get_json()["server_public_key"] == server


def test_api_v1_no_match_includes_safe_scheduler_metadata(client):
    server = _server_key("no-match-metadata")
    _register_api_v1_server_with_capabilities(client, server, _capabilities("8k-fast", ["model-a"]))

    response = client.get("/api/v1/relay/servers/next?model=model-b&context_tier=8k-fast")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["error"]["code"] == "no_matching_compute_node"
    assert payload["error"]["selection_policy"] == relay_module.API_V1_SELECTION_POLICY
    assert payload["error"]["requested_context_tier"] == "8k-fast"
    assert payload["error"]["requested_model"] == "model-b"
    assert payload["error"]["eligible_node_count"] == 0
    assert payload["error"]["eligible_tier_counts"] == {}


class _LockAssertingInFlightRequests(dict):
    def __init__(self, lock_probe, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock_probe = lock_probe
        self.values_checked_under_in_flight_lock = False

    def values(self):
        if self.lock_probe.locked:
            self.values_checked_under_in_flight_lock = True
        return super().values()


class _LockProbe:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.locked = False

    def __enter__(self):
        self.wrapped.acquire()
        self.locked = True
        return self

    def __exit__(self, *_args):
        self.locked = False
        self.wrapped.release()
        return False


def test_api_v1_in_flight_count_snapshots_under_lock_without_mutating_payload(client, monkeypatch):
    lock_probe = _LockProbe(relay_module.api_v1_in_flight_requests_lock)
    monkeypatch.setattr(relay_module, "api_v1_in_flight_requests_lock", lock_probe)
    in_flight = _LockAssertingInFlightRequests(lock_probe, {
        "req-active": {"expires_at": time.monotonic() + 60, "client_public_key": DUMMY_CLIENT_PUB_KEY},
        "req-expired": {"expires_at": time.monotonic() - 60, "client_public_key": DUMMY_CLIENT_PUB_KEY},
    })
    payload = {
        relay_module.API_V1_SERVER_MARKER: True,
        "last_ping": datetime.now(),
        "last_ping_duration": 30,
        "capabilities": _capabilities("8k-fast"),
        "api_v1_in_flight_requests": in_flight,
    }

    load = relay_module._api_v1_node_load_snapshot(
        "lock-snapshot-node",
        payload,
        now_monotonic=time.monotonic(),
    )

    assert load["in_flight_count"] == 1
    assert in_flight.values_checked_under_in_flight_lock is True
    assert set(payload["api_v1_in_flight_requests"]) == {"req-active", "req-expired"}


def test_evict_stale_servers_prunes_expired_in_flight_entries_after_deadline(client):
    server = _server_key("prune-expired-inflight")
    _register_api_v1_server_with_capabilities(client, server, _capabilities("8k-fast"))
    now = time.monotonic()
    known_servers[server]["api_v1_in_flight_requests"] = {
        "legacy-expired": {
            "expires_at": now - 30,
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
        },
        "deadline-expired": {
            "expires_at": now - 20,
            "request_deadline_monotonic": now - 1,
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
        },
        "renewable-owner-state": {
            "expires_at": now - 10,
            "request_deadline_monotonic": now + 60,
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
        },
        "active": {
            "expires_at": now + 60,
            "request_deadline_monotonic": now + 120,
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
        },
    }

    relay_module._evict_stale_servers()

    remaining = known_servers[server]["api_v1_in_flight_requests"]
    assert set(remaining) == {"renewable-owner-state", "active"}


def test_api_v1_malformed_capabilities_are_rejected_without_registration(client):
    server = _server_key("malformed-cap")
    response = client.post(
        "/api/v1/relay/servers/register",
        json={
            "server_public_key": server,
            "capabilities": {
                "active_context_tier": "64k-full",
                "supported_model_ids": ["llama-3.1-8b-instruct"],
                "maximum_total_context_tokens": 8192,
                "default_output_token_reservation": 1024,
                "maximum_output_tokens": 2048,
                "max_concurrency": 1,
            },
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_capabilities"
    assert server not in known_servers


def test_api_v1_selection_model_filter_round_robin_and_no_match(client):
    a = _server_key("tier-a")
    b = _server_key("tier-b")
    c = _server_key("other-model")
    _register_api_v1_server_with_capabilities(client, a, _capabilities("8k-fast", ["model-a"]))
    _register_api_v1_server_with_capabilities(client, b, _capabilities("8k-fast", ["model-a"]))
    _register_api_v1_server_with_capabilities(client, c, _capabilities("64k-full", ["model-b"]))

    selected = [
        client.get("/api/v1/relay/servers/next?model=model-a&context_tier=8k-fast").get_json()["server_public_key"]
        for _ in range(4)
    ]
    assert selected == [a, b, a, b]

    no_match = client.get("/api/v1/relay/servers/next?model=model-a&context_tier=64k-full")
    assert no_match.status_code == 503
    assert no_match.get_json()["error"]["code"] == "no_matching_compute_node"



def test_api_v1_missing_capabilities_are_not_qwen_capable(client):
    server = _server_key("missing-capabilities")
    _register_api_v1_server_without_capabilities(client, server)

    response = client.get("/api/v1/relay/servers/next?model=qwen3-8b-instruct&context_tier=8k-fast")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["error"]["code"] == "no_matching_compute_node"
    assert payload["error"]["requested_model"] == "qwen3-8b-instruct"


def test_api_v1_selection_resolves_old_llama_alias_to_qwen_and_skips_stale_llama_nodes(client):
    qwen = _server_key("qwen-capable")
    stale_llama = _server_key("stale-llama-only")
    _register_api_v1_server_with_capabilities(client, stale_llama, _capabilities("8k-fast", ["llama-3.1-8b-instruct"]))
    _register_api_v1_server_with_capabilities(client, qwen, _capabilities("8k-fast", ["qwen3-8b-instruct"]))

    response = client.get("/api/v1/relay/servers/next?model=llama-3.1-8b-instruct&context_tier=8k-fast")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["server_public_key"] == qwen
    assert payload["requested_model"] == "llama-3.1-8b-instruct"
    assert payload["resolved_model"] == "qwen3-8b-instruct"
    assert payload["selected_model_support"] == ["qwen3-8b-instruct"]

def test_api_v1_selection_reports_capacity_exhaustion_separately(client):
    server = _server_key("saturated-capacity")
    _register_api_v1_server_with_capabilities(client, server, _capabilities("8k-fast", ["model-a"]))
    known_servers[server]["api_v1_in_flight_requests"] = {
        "req-in-flight": {"expires_at": time.monotonic() + 60, "client_public_key": DUMMY_CLIENT_PUB_KEY}
    }

    response = client.get("/api/v1/relay/servers/next?model=model-a&context_tier=8k-fast")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["error"]["code"] == "no_available_capacity"
    assert payload["error"]["eligible_tier_counts"] == {"8k-fast": 1}
    assert payload["error"]["capacity_limited_node_count"] == 1
    assert payload["error"]["capacity_limited_tier_counts"] == {"8k-fast": 1}
    assert "at capacity" in payload["error"]["message"]


def test_api_v1_filtered_round_robin_key_ignores_load_score_changes(client):
    a = _server_key("rr-load-a")
    b = _server_key("rr-load-b")
    capabilities = _capabilities("8k-fast", ["model-a"])
    capabilities["max_concurrency"] = 4
    _register_api_v1_server_with_capabilities(client, a, dict(capabilities))
    _register_api_v1_server_with_capabilities(client, b, dict(capabilities))

    assert client.get("/api/v1/relay/servers/next?model=model-a").status_code == 200
    keys_after_idle_selection = set(relay_module.api_v1_filtered_round_robin_next_positions)
    known_servers[a]["api_v1_in_flight_requests"] = {
        "req-in-flight": {"expires_at": time.monotonic() + 60, "client_public_key": DUMMY_CLIENT_PUB_KEY}
    }
    known_servers[b]["api_v1_in_flight_requests"] = {
        "req-in-flight": {"expires_at": time.monotonic() + 60, "client_public_key": DUMMY_CLIENT_PUB_KEY}
    }

    assert client.get("/api/v1/relay/servers/next?model=model-a").status_code == 200

    assert set(relay_module.api_v1_filtered_round_robin_next_positions) == keys_after_idle_selection


def test_api_v1_filtered_round_robin_is_stable_across_alternating_filters(client):
    fast = _server_key("mixed-fast")
    full_a = _server_key("mixed-full-a")
    full_b = _server_key("mixed-full-b")
    _register_api_v1_server_with_capabilities(client, fast, _capabilities("8k-fast", ["fast-model"]))
    _register_api_v1_server_with_capabilities(client, full_a, _capabilities("64k-full", ["full-model"]))
    _register_api_v1_server_with_capabilities(client, full_b, _capabilities("64k-full", ["full-model"]))

    selections = [
        client.get("/api/v1/relay/servers/next?model=full-model&context_tier=64k-full").get_json()["server_public_key"],
        client.get("/api/v1/relay/servers/next?model=fast-model").get_json()["server_public_key"],
        client.get("/api/v1/relay/servers/next?model=full-model&context_tier=64k-full").get_json()["server_public_key"],
        client.get("/api/v1/relay/servers/next?model=fast-model").get_json()["server_public_key"],
    ]

    assert selections == [full_a, fast, full_b, fast]


def test_api_v1_no_match_does_not_reset_round_robin_cursor(client):
    a = _server_key("fair-a")
    b = _server_key("fair-b")
    c = _server_key("fair-c")
    for key in (a, b, c):
        _register_api_v1_server_with_capabilities(client, key, _capabilities("8k-fast"))

    assert client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()["server_public_key"] == a
    no_match = client.get("/api/v1/relay/servers/next?context_tier=64k-full")
    assert no_match.status_code == 503

    assert client.get("/api/v1/relay/servers/next?context_tier=8k-fast").get_json()["server_public_key"] == b


def test_api_v1_rejects_excessive_capability_model_ids(client):
    server = _server_key("too-many-models")
    capabilities = _capabilities(
        "8k-fast",
        [f"model-{index}" for index in range(relay_module.MAX_API_V1_MODEL_IDS_PER_NODE + 1)],
    )

    response = client.post(
        "/api/v1/relay/servers/register",
        json={"server_public_key": server, "capabilities": capabilities},
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_capabilities"
    assert server not in known_servers


def test_api_v1_poll_capabilities_null_preserves_registered_tier(client):
    server = _server_key("null-heartbeat")
    _register_api_v1_server_with_capabilities(client, server, _capabilities("64k-full"))

    response = client.post(
        "/api/v1/relay/servers/poll",
        json={"server_public_key": server, "capabilities": None},
    )

    assert response.status_code == 200
    assert known_servers[server]["capabilities"]["active_context_tier"] == "64k-full"


def test_api_v1_diagnostics_expose_only_normalized_capabilities(client):
    server = _server_key("diagnostic-cap")
    _register_api_v1_server_with_capabilities(
        client,
        server,
        {**_capabilities("64k-full"), "hostname": "private-host", "raw_vram_gb": 24},
    )

    diagnostics = client.get("/relay/diagnostics")
    assert diagnostics.status_code == 200
    node = diagnostics.get_json()["api_v1_registered_compute_nodes"][0]
    assert node["capabilities"] == _capabilities("64k-full")
    assert "hostname" not in json.dumps(node)
    assert "raw_vram" not in json.dumps(node)


def _queue_api_v1_request(client, *, server_public_key, request_id, client_public_key=None):
    response = client.post('/api/v1/relay/requests', json={
        'request_id': request_id,
        'client_public_key': client_public_key or f'{DUMMY_CLIENT_PUB_KEY}-{request_id}',
        'server_public_key': server_public_key,
        'chat_history': f'ciphertext-{request_id}',
        'cipherkey': f'cipherkey-{request_id}',
        'iv': f'iv-{request_id}',
    })
    assert response.status_code == 200
    return response

# --- Test /sink ---

def test_sink_register_new_server(client):
    """Test server registration via /sink."""
    payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert 'next_ping_in_x_seconds' in data
    assert DUMMY_SERVER_PUB_KEY in known_servers
    assert known_servers[DUMMY_SERVER_PUB_KEY]['public_key'] == DUMMY_SERVER_PUB_KEY

def test_sink_update_existing_server(client):
    """Test server ping update via /sink."""
    # Initial registration using datetime
    initial_ping_time = datetime.now() - timedelta(seconds=20)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': initial_ping_time,
        'last_ping_duration': 10
    }

    time.sleep(0.1) # Ensure time progresses slightly

    # Send update ping
    payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200

    assert DUMMY_SERVER_PUB_KEY in known_servers
    # Compare datetime objects
    assert known_servers[DUMMY_SERVER_PUB_KEY]['last_ping'] > initial_ping_time

def test_sink_invalid_payload(client):
    """Test /sink with missing public key."""
    response = client.post("/sink", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid public key'


def test_sink_drops_api_v1_only_queue(client):
    """Sink should drain stale API v1 plaintext entries without dispatching work."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {"api_v1_request": {"messages": [{"role": "user", "content": "stale"}]}},
        {"api_v1_request": {"messages": [{"role": "user", "content": "stale-2"}]}},
    ]

    response = client.post("/sink", json={"server_public_key": DUMMY_SERVER_PUB_KEY})
    assert response.status_code == 200
    payload = response.get_json()

    assert "next_ping_in_x_seconds" in payload
    assert "chat_history" not in payload
    assert client_inference_requests[DUMMY_SERVER_PUB_KEY] == []


def test_sink_skips_api_v1_and_returns_legacy_batch(client):
    """Sink should skip stale API v1 entries and still dispatch legacy E2EE work."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    legacy_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "legacy-ciphertext",
        "cipherkey": "legacy-cipherkey",
        "iv": "legacy-iv",
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {"api_v1_request": {"messages": [{"role": "user", "content": "stale"}]}},
        legacy_payload,
    ]

    response = client.post(
        "/sink",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY, "max_batch_size": 2},
    )
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["client_public_key"] == legacy_payload["client_public_key"]
    assert payload["chat_history"] == legacy_payload["chat_history"]
    assert payload["cipherkey"] == legacy_payload["cipherkey"]
    assert payload["iv"] == legacy_payload["iv"]
    assert payload["batch"] == [legacy_payload]
    assert client_inference_requests[DUMMY_SERVER_PUB_KEY] == []


def test_sink_returns_batch_when_requested(client):
    """Servers can opt into batched work retrieval via max_batch_size."""
    sink_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200

    for idx in range(3):
        faucet_payload = {
            "client_public_key": base64.b64encode(f"client_{idx}".encode()).decode(),
            "server_public_key": DUMMY_SERVER_PUB_KEY,
            "chat_history": f"encrypted_payload_{idx}",
            "cipherkey": f"cipher_{idx}",
            "iv": f"iv_{idx}",
        }
        faucet_response = client.post("/faucet", json=faucet_payload)
        assert faucet_response.status_code == 200

    batch_response = client.post(
        "/sink",
        json={"server_public_key": DUMMY_SERVER_PUB_KEY, "max_batch_size": 2},
    )
    assert batch_response.status_code == 200
    batch_data = batch_response.get_json()

    assert 'batch' in batch_data
    assert isinstance(batch_data['batch'], list)
    assert len(batch_data['batch']) == 2

    first_request, second_request = batch_data['batch']
    assert first_request['chat_history'] == "encrypted_payload_0"
    assert first_request['client_public_key'] == batch_data['client_public_key']
    assert second_request['chat_history'] == "encrypted_payload_1"

    remaining_queue = client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])
    assert len(remaining_queue) == 1
    assert remaining_queue[0]['chat_history'] == "encrypted_payload_2"


def test_two_servers_receive_only_addressed_work(client):
    """Queued work should remain isolated by server public key."""
    server_one = base64.b64encode(b"server_public_key_1").decode("utf-8")
    server_two = base64.b64encode(b"server_public_key_2").decode("utf-8")

    assert client.post("/sink", json={"server_public_key": server_one}).status_code == 200
    assert client.post("/sink", json={"server_public_key": server_two}).status_code == 200

    first_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": server_one,
        "chat_history": "work-for-server-one",
        "cipherkey": "cipher-one",
        "iv": "iv-one",
    }
    second_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": server_two,
        "chat_history": "work-for-server-two",
        "cipherkey": "cipher-two",
        "iv": "iv-two",
    }
    assert client.post("/faucet", json=first_payload).status_code == 200
    assert client.post("/faucet", json=second_payload).status_code == 200

    server_two_work = client.post("/sink", json={"server_public_key": server_two})
    assert server_two_work.status_code == 200
    assert server_two_work.get_json()["chat_history"] == "work-for-server-two"

    server_one_work = client.post("/sink", json={"server_public_key": server_one})
    assert server_one_work.status_code == 200
    assert server_one_work.get_json()["chat_history"] == "work-for-server-one"


def test_relay_api_v1_fails_closed(client):
    response = client.post(
        "/relay/api/v1/chat/completions",
        data="[",
        content_type="application/json",
    )

    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["type"] == "service_unavailable_error"
    assert data["error"]["code"] == "distributed_api_v1_relay_disabled"


def test_relay_api_v1_source_fails_closed(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }

    response = client.post(
        "/relay/api/v1/source",
        json={
            "request_id": "req-1",
            "message": {"role": "assistant", "content": "hello"},
        },
    )

    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["type"] == "service_unavailable_error"
    assert data["error"]["code"] == "distributed_api_v1_relay_disabled"


class _ApiV1TestLlmMixin:
    @staticmethod
    def tokenize(prompt, *args, **kwargs):
        if isinstance(prompt, bytes):
            prompt = prompt.decode("utf-8")
        return str(prompt).split()


class _RelayClientApiV1CryptoStub:
    def __init__(self, decrypted_payload):
        self.decrypted_payload = decrypted_payload
        self.last_encrypted_payload = None

    def decrypt_message(self, _request_data):
        return self.decrypted_payload

    def encrypt_message(self, payload, _client_pub_key):
        self.last_encrypted_payload = payload
        return {
            "chat_history": "ciphertext-only",
            "cipherkey": "cipher-key",
            "iv": "cipher-iv",
        }


def _build_relay_client_for_api_v1_tests(crypto_stub, model_manager=None):
    relay_client = RelayClient(
        base_url="https://relay.example",
        port=None,
        crypto_manager=crypto_stub,
        model_manager=model_manager or object(),
        include_configured_servers=False,
    )
    relay_client._api_v1_authoritative_context_admission = lambda **_kwargs: (True, None, 1)
    return relay_client


def test_relay_client_api_v1_envelope_uses_model_and_posts_ciphertext_only(monkeypatch):
    captured = {}
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-1",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {"temperature": 0.2, "max_tokens": 42},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    class _FakeLlmInstance(_ApiV1TestLlmMixin):
        @staticmethod
        def create_chat_completion(messages, **options):
            captured["messages"] = messages
            captured["options"] = options
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "bonjour"}},
                ]
            }

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(model):
            captured["model"] = model
            return model == "llama-3-8b-instruct:alignment"

        @staticmethod
        def get_llm_instance():
            return _FakeLlmInstance()

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(url, json, timeout, **_kwargs):
        assert url == "https://relay.example/api/v1/relay/responses"
        assert timeout == relay_client._request_timeout
        assert "chat_history" in json and "cipherkey" in json and "iv" in json
        assert json["request_id"] == "req-1"
        assert json["protocol"] == "tokenplace_api_v1_relay_e2ee"
        assert json["version"] == 1
        assert "messages" not in json
        assert "prompt" not in json
        assert "model" not in json
        assert "api_v1_response" not in json

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-1"
    assert encrypted_payload["api_v1_response"]["message"]["content"] == "bonjour"
    assert captured["model"] == "llama-3-8b-instruct:alignment"


def test_relay_client_rejects_invalid_client_public_key_encoding(monkeypatch):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-invalid-client-key",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    post_calls = {"count": 0}

    def fake_post(*_args, **_kwargs):
        post_calls["count"] += 1
        class _Response:
            status_code = 200
        return _Response()

    def fake_generate_response(_model, messages, **_options):
        return messages + [{"role": "assistant", "content": "bonjour"}]

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": "%%%not-base64%%%",
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is False
    assert post_calls["count"] == 0


def test_relay_client_api_v1_normalizes_client_public_key_binding(monkeypatch):
    normalized_client_key = DUMMY_CLIENT_PUB_KEY
    request_client_key = f"  {normalized_client_key}\n"
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": normalized_client_key,
        "request_id": "req-normalized-client-key",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(_model, messages, **_options):
        return messages + [{"role": "assistant", "content": "bonjour"}]

    def fake_post(_url, json, timeout, **_kwargs):
        assert timeout == relay_client._request_timeout
        assert json["client_public_key"] == normalized_client_key

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("api.v1.models.generate_response", fake_generate_response)
    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": request_client_key,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True


def test_relay_client_api_v1_posts_encrypted_model_unsupported_error(monkeypatch):
    from api.v1.models import ModelError

    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-unsupported",
        "api_v1_request": {
            "model": "unknown-model-id",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(*_args, **_kwargs):
        raise ModelError("Model 'unknown-model-id' not found", status_code=404, error_type="model_not_found")

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None
        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-unsupported"
    assert encrypted_payload["api_v1_response"]["error"]["code"] == "compute_node_model_unsupported"


def test_relay_client_api_v1_falls_back_to_runtime_model_when_catalog_model_unavailable(monkeypatch):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-runtime-fallback",
        "api_v1_request": {
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {"temperature": 0.2},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)

    class _FakeLlmInstance(_ApiV1TestLlmMixin):
        @staticmethod
        def create_chat_completion(messages, **_options):
            return {"choices": [{"message": {"role": "assistant", "content": "Paris"}}]}

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(model):
            return model == "llama-3-8b-instruct"

        @staticmethod
        def get_llm_instance():
            return _FakeLlmInstance()

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-runtime-fallback"
    assert encrypted_payload["api_v1_response"]["message"]["content"] == "Paris"


def test_relay_client_api_v1_posts_encrypted_internal_error_for_unexpected_exception(monkeypatch):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-internal",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {"temperature": 0.2},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(_model):
            return True

        @staticmethod
        def get_llm_instance():
            raise RuntimeError("backend crashed")

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-internal"
    assert encrypted_payload["api_v1_response"]["error"]["code"] == "compute_node_internal_error"


def test_relay_client_api_v1_source_post_failure_returns_false(monkeypatch):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-source-post-failure",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)

    def fake_generate_response(_model, messages, **_options):
        return messages + [{"role": "assistant", "content": "bonjour"}]

    def raising_post(*_args, **_kwargs):
        raise RuntimeError("relay /source unavailable")

    monkeypatch.setattr("utils.networking.relay_client.requests.post", raising_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is False


@pytest.mark.parametrize(
    ("generated_response",),
    [
        ([],),
        ([{"role": "assistant", "content": "ok"}, "bad-last-message"],),
    ],
)
def test_relay_client_api_v1_posts_encrypted_internal_error_for_invalid_inference_output(
    monkeypatch,
    generated_response,
):
    decrypted_payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "request_id": "req-invalid-inference-output",
        "api_v1_request": {
            "model": "llama-3-8b-instruct:alignment",
            "messages": [{"role": "user", "content": "hello"}],
            "options": {},
        },
    }
    crypto_stub = _RelayClientApiV1CryptoStub(decrypted_payload)
    class _FakeLlmInstance(_ApiV1TestLlmMixin):
        @staticmethod
        def create_chat_completion(*, messages, **_options):
            return generated_response

    class _RuntimeModelManager:
        @staticmethod
        def supports_api_v1_model(_model):
            return True

        @staticmethod
        def get_llm_instance():
            return _FakeLlmInstance()

    relay_client = _build_relay_client_for_api_v1_tests(
        crypto_stub,
        model_manager=_RuntimeModelManager(),
    )

    def fake_post(_url, json=None, timeout=None, **_kwargs):
        assert json is not None
        assert timeout is not None

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }
    assert relay_client.process_client_request(request_data) is True
    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-invalid-inference-output"
    assert encrypted_payload["api_v1_response"]["error"]["code"] == "compute_node_invalid_model_output"


def test_relay_client_submit_api_v1_error_response_posts_ciphertext_only(monkeypatch):
    crypto_stub = _RelayClientApiV1CryptoStub({})
    relay_client = _build_relay_client_for_api_v1_tests(crypto_stub)
    captured = {}

    def fake_post(url, json=None, timeout=None, **_kwargs):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout

        class _Response:
            status_code = 200

        return _Response()

    monkeypatch.setattr("utils.networking.relay_client.requests.post", fake_post)

    request_data = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-runtime-not-ready",
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "opaque",
        "cipherkey": "opaque",
        "iv": "opaque",
    }

    assert relay_client.submit_api_v1_error_response(
        request_data,
        code="compute_node_runtime_not_ready",
        message="API v1 model runtime is not ready",
    ) is True

    assert captured["url"] == "https://relay.example/api/v1/relay/responses"
    assert captured["timeout"] == relay_client._request_timeout
    posted = captured["payload"]
    assert posted["request_id"] == "req-runtime-not-ready"
    assert posted["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert posted["version"] == 1
    assert "chat_history" in posted and "cipherkey" in posted and "iv" in posted
    assert "api_v1_response" not in posted
    assert "API v1 model runtime is not ready" not in json.dumps(posted)

    encrypted_payload = crypto_stub.last_encrypted_payload
    assert encrypted_payload["request_id"] == "req-runtime-not-ready"
    assert encrypted_payload["api_v1_response"]["error"] == {
        "code": "compute_node_runtime_not_ready",
        "message": "API v1 model runtime is not ready",
    }


# --- Test /faucet ---

def test_faucet_submit_request(client):
    """Test submitting a valid inference request via /faucet."""
    # Register server first
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': time.time(),
        'last_ping_duration': 10
    }

    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector"
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['message'] == 'Request received'

    # Check internal state
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1
    queued_req = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert queued_req['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert queued_req['chat_history'] == "encrypted_chat_history_data"

def test_faucet_invalid_payload(client):
    """Test /faucet with missing fields."""
    # Register server
    known_servers[DUMMY_SERVER_PUB_KEY] = {'public_key': DUMMY_SERVER_PUB_KEY, 'last_ping': time.time(), 'last_ping_duration': 10}

    payload = { "server_public_key": DUMMY_SERVER_PUB_KEY } # Missing other fields
    response = client.post("/faucet", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error']['message'] == 'Invalid request data'

def test_faucet_unknown_server(client):
    """Test /faucet request to an unknown server."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": "unknown_server_key", # This server is not registered
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector"
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 404
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == {'message': 'Server with the specified public key not found', 'code': 404}


def test_relay_diagnostics_distinguishes_configured_and_live_nodes(client, monkeypatch):
    """Diagnostics should expose configured URLs and live compute registrations."""
    monkeypatch.delenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setitem(app.config, "relay_configured_servers", [
        "https://configured-one.example.com:8000",
        "https://configured-two.example.com:8000",
    ])
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {"chat_history": "pending", "client_public_key": "c", "cipherkey": "k", "iv": "i"}
    ]

    response = client.get("/relay/diagnostics")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["configured_upstream_servers"] == app.config["relay_configured_servers"]
    assert payload["active_upstream_servers"] == app.config["relay_configured_servers"]
    assert payload["required_upstream_servers"] == []
    assert REMOVED_DIAGNOSTICS_CONFIGURED_UPSTREAM_ALIAS not in payload
    assert payload["upstream_health_required"] is False
    assert payload["relay_only"] is False
    assert payload["total_registered_compute_nodes"] == 1
    assert payload["total_api_v1_registered_compute_nodes"] == 0
    assert payload["api_v1_registered_compute_nodes"] == []
    assert payload["registered_compute_nodes"][0]["server_public_key"] == DUMMY_SERVER_PUB_KEY
    assert payload["registered_compute_nodes"][0]["queue_depth"] == 1


def test_relay_diagnostics_counts_live_api_v1_compute_nodes(client):
    """Diagnostics should count live API v1 compute-node registrations."""
    server_a = _server_key("diagnostics-live-a")
    server_b = _server_key("diagnostics-live-b")

    _register_api_v1_server(client, server_a)
    _register_api_v1_server(client, server_b)

    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["total_registered_compute_nodes"] == 2
    assert payload["total_api_v1_registered_compute_nodes"] == 2
    assert {node["server_public_key"] for node in payload["registered_compute_nodes"]} == {server_a, server_b}
    assert {node["server_public_key"] for node in payload["api_v1_registered_compute_nodes"]} == {server_a, server_b}


def test_relay_diagnostics_separates_legacy_from_api_v1_compute_node_count(client):
    """Diagnostics should expose an API v1-eligible count for landing chat capacity."""
    api_v1_server_key = _server_key("diagnostics-api-v1-usable")
    legacy_server_key = _server_key("diagnostics-legacy-only")
    known_servers[api_v1_server_key] = {
        "public_key": api_v1_server_key,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    known_servers[legacy_server_key] = {
        "public_key": legacy_server_key,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }

    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["total_registered_compute_nodes"] == 2
    assert payload["total_api_v1_registered_compute_nodes"] == 1
    assert {node["server_public_key"] for node in payload["registered_compute_nodes"]} == {
        api_v1_server_key,
        legacy_server_key,
    }
    assert [node["server_public_key"] for node in payload["api_v1_registered_compute_nodes"]] == [
        api_v1_server_key
    ]


def test_relay_diagnostics_evicts_stale_compute_nodes_before_counting(client, monkeypatch):
    """Diagnostics should report only non-stale compute nodes after eviction."""
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS", "1")
    live_server_key = _server_key("diagnostics-live")
    stale_server_key = _server_key("diagnostics-stale")
    known_servers[live_server_key] = {
        "public_key": live_server_key,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    known_servers[stale_server_key] = {
        "public_key": stale_server_key,
        "last_ping": datetime.now() - timedelta(seconds=5),
        "last_ping_duration": 1,
        relay_module.API_V1_SERVER_MARKER: True,
    }

    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["total_registered_compute_nodes"] == 1
    assert payload["total_api_v1_registered_compute_nodes"] == 1
    assert [node["server_public_key"] for node in payload["registered_compute_nodes"]] == [live_server_key]
    assert [node["server_public_key"] for node in payload["api_v1_registered_compute_nodes"]] == [live_server_key]
    assert stale_server_key not in known_servers


def test_relay_diagnostics_empty_relay_returns_zero(client):
    """Diagnostics should return a stable zero count when no compute nodes are registered."""
    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["total_registered_compute_nodes"] == 0
    assert payload["total_api_v1_registered_compute_nodes"] == 0
    assert payload["registered_compute_nodes"] == []
    assert payload["api_v1_registered_compute_nodes"] == []


def test_relay_diagnostics_does_not_expose_private_material(client):
    """Diagnostics must not leak private relay-owned material from server state."""
    live_server_key = _server_key("diagnostics-private-material")
    known_servers[live_server_key] = {
        "public_key": live_server_key,
        "private_key": "PRIVATE_KEY_SHOULD_NOT_LEAK",
        "secret_token": "SECRET_TOKEN_SHOULD_NOT_LEAK",
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
        relay_module.API_V1_SERVER_MARKER: True,
    }

    response = client.get("/relay/diagnostics")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "PRIVATE_KEY_SHOULD_NOT_LEAK" not in body
    assert "SECRET_TOKEN_SHOULD_NOT_LEAK" not in body
    assert "private_key" not in body
    assert "secret_token" not in body


def test_relay_diagnostics_reports_explicit_upstream_env(client, monkeypatch):
    """Diagnostics should retain configured_upstream_servers for explicit upstream env config."""
    configured_servers = ["https://configured-one.example.com:8000"]
    monkeypatch.setitem(app.config, "relay_configured_servers", configured_servers)
    monkeypatch.setenv("TOKENPLACE_RELAY_UPSTREAM_URL", "https://gpu.example.com:5015")
    monkeypatch.setitem(app.config, "upstream_url", "https://gpu.example.com:5015")

    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["configured_upstream_servers"] == configured_servers
    assert REMOVED_DIAGNOSTICS_CONFIGURED_UPSTREAM_ALIAS not in payload
    assert payload["active_upstream_servers"] == ["https://gpu.example.com:5015"]
    assert payload["required_upstream_servers"] == []
    assert payload["relay_only"] is False
    assert payload["upstream_health_required"] is False


def test_relay_diagnostics_relay_only_reports_no_active_or_required_upstreams(client, monkeypatch):
    """Relay-only diagnostics should separate fallback config from readiness dependencies."""
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/relay/diagnostics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relay_only"] is True
    assert payload["upstream_health_required"] is False
    assert payload["active_upstream_servers"] == []
    assert payload["required_upstream_servers"] == []
    assert payload["configured_upstream_servers"] == ["https://token.place"]
    assert REMOVED_DIAGNOSTICS_CONFIGURED_UPSTREAM_ALIAS not in payload


def test_healthz_reports_configured_upstreams_and_live_queue_depth(client, monkeypatch):
    """Healthz should separate configured upstream URLs from live registered nodes."""
    monkeypatch.setitem(app.config, "gpu_host", None)
    configured_servers = [
        "https://configured-one.example.com:8000",
        "https://configured-two.example.com:8000",
    ]
    app.config["relay_configured_servers"] = configured_servers
    live_server_key = base64.b64encode(b"live_server_key").decode("utf-8")
    known_servers[live_server_key] = {
        "public_key": live_server_key,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    client_inference_requests[live_server_key] = [
        {
            "client_public_key": DUMMY_CLIENT_PUB_KEY,
            "chat_history": "pending-work",
            "cipherkey": "cipher",
            "iv": "iv",
        }
    ]

    monkeypatch.setenv("TOKEN_PLACE_RELAY_UPSTREAMS", ",".join(configured_servers))
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["configuredUpstreamServers"] == configured_servers
    assert payload["activeUpstreamServers"] == configured_servers
    assert payload["requiredUpstreamServers"] == []
    assert payload["upstreamHealthRequired"] is False
    assert payload["relayOnly"] is False
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload
    assert payload["registeredServers"][0]["server_public_key"] == live_server_key
    assert payload["registeredServers"][0]["age_seconds"] >= 0
    assert payload["registeredServers"][0]["queue_depth"] == 1
    assert "https://configured-one.example.com:8000" not in {
        node["server_public_key"] for node in payload["registeredServers"]
    }


def test_healthz_returns_draining_when_shutdown_flag_set(client):
    """healthz should switch to draining status and 503 during shutdown."""
    relay_module.DRAINING.set()
    try:
        response = client.get("/healthz")
    finally:
        relay_module.DRAINING.clear()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "0"
    payload = response.get_json()
    assert payload["status"] == "draining"
    assert payload["details"]["shutdown"] is True


def test_livez_remains_alive_when_draining(client):
    """livez should stay green so orchestrators can distinguish readiness from liveness."""
    from relay import DRAINING

    DRAINING.set()
    try:
        response = client.get("/livez")
    finally:
        DRAINING.clear()

    assert response.status_code == 200
    assert response.get_json()["status"] == "alive"


def test_healthz_default_allows_unresolvable_upstream_host(client, monkeypatch):
    """healthz should stay ready by default for relay-only deployments."""
    monkeypatch.setitem(app.config, "gpu_host", "definitely-not-resolvable.invalid")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])
    monkeypatch.setattr(relay_module, "_can_resolve_gpu_host", lambda _host: False)
    monkeypatch.delenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["gpuHost"] == "definitely-not-resolvable.invalid"
    assert payload["upstreamHealthRequired"] is False
    assert payload["relayOnly"] is True
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert payload["activeUpstreamServers"] == []
    assert payload["requiredUpstreamServers"] == []
    assert payload.get("details", {}).get("gpuHostResolution") != "failed"


def test_healthz_requires_upstream_health_when_env_enabled(client, monkeypatch):
    """healthz should degrade when upstream resolution is required and fails."""
    monkeypatch.setitem(app.config, "gpu_host", "definitely-not-resolvable.invalid")
    monkeypatch.setitem(
        app.config, "upstream_url", "http://definitely-not-resolvable.invalid:3000"
    )
    monkeypatch.setattr(relay_module, "_can_resolve_gpu_host", lambda _host: False)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "1")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["upstreamHealthRequired"] is True
    assert payload["relayOnly"] is False
    assert payload["activeUpstreamServers"] == ["https://token.place"]
    assert payload["requiredUpstreamServers"] == [
        "http://definitely-not-resolvable.invalid:3000"
    ]
    assert payload["details"]["gpuHostResolution"] == "failed"


def test_healthz_required_upstreams_report_checked_upstream_url(client, monkeypatch):
    """Required upstreams should name the checked URL, not the configured pool."""
    monkeypatch.setitem(app.config, "gpu_host", "gpu-server")
    monkeypatch.setitem(app.config, "upstream_url", "http://gpu-server:3000")
    monkeypatch.setattr(relay_module, "_can_resolve_gpu_host", lambda _host: True)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "1")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_UPSTREAMS", "https://node-a")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://node-a"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["activeUpstreamServers"] == ["https://node-a"]
    assert payload["requiredUpstreamServers"] == ["http://gpu-server:3000"]

    diagnostics_response = client.get("/relay/diagnostics")
    diagnostics_payload = diagnostics_response.get_json()

    assert diagnostics_response.status_code == 200
    assert diagnostics_payload["active_upstream_servers"] == ["https://node-a"]
    assert diagnostics_payload["required_upstream_servers"] == ["http://gpu-server:3000"]


def test_explicit_runtime_upstream_reports_active_without_required_dependency(
    client, monkeypatch
):
    """Env runtime upstream should be active without making fallback config required."""
    monkeypatch.setenv("TOKENPLACE_RELAY_UPSTREAM_URL", "https://gpu.example.test:3000")
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.setitem(app.config, "upstream_url", "https://gpu.example.test:3000")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relayOnly"] is False
    assert payload["upstreamHealthRequired"] is False
    assert payload["activeUpstreamServers"] == ["https://gpu.example.test:3000"]
    assert payload["requiredUpstreamServers"] == []
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload

    diagnostics_response = client.get("/relay/diagnostics")
    diagnostics_payload = diagnostics_response.get_json()

    assert diagnostics_response.status_code == 200
    assert diagnostics_payload["relay_only"] is False
    assert diagnostics_payload["upstream_health_required"] is False
    assert diagnostics_payload["active_upstream_servers"] == [
        "https://gpu.example.test:3000"
    ]
    assert diagnostics_payload["required_upstream_servers"] == []
    assert diagnostics_payload["configured_upstream_servers"] == ["https://token.place"]
    assert REMOVED_DIAGNOSTICS_CONFIGURED_UPSTREAM_ALIAS not in diagnostics_payload


def test_explicit_runtime_upstream_required_health_reports_checked_dependency(
    client, monkeypatch
):
    """Required health should name only the env upstream URL that readiness checks."""
    monkeypatch.setenv("TOKENPLACE_RELAY_UPSTREAM_URL", "https://gpu.example.test:3000")
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "1")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.setitem(app.config, "gpu_host", "gpu.example.test")
    monkeypatch.setitem(app.config, "upstream_url", "https://gpu.example.test:3000")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])
    monkeypatch.setattr(relay_module, "_can_resolve_gpu_host", lambda _host: True)

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relayOnly"] is False
    assert payload["upstreamHealthRequired"] is True
    assert payload["activeUpstreamServers"] == ["https://gpu.example.test:3000"]
    assert payload["requiredUpstreamServers"] == ["https://gpu.example.test:3000"]
    assert "https://token.place" not in payload["requiredUpstreamServers"]
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload

    diagnostics_response = client.get("/relay/diagnostics")
    diagnostics_payload = diagnostics_response.get_json()

    assert diagnostics_response.status_code == 200
    assert diagnostics_payload["active_upstream_servers"] == [
        "https://gpu.example.test:3000"
    ]
    assert diagnostics_payload["required_upstream_servers"] == [
        "https://gpu.example.test:3000"
    ]
    assert "https://token.place" not in diagnostics_payload["required_upstream_servers"]
    assert diagnostics_payload["configured_upstream_servers"] == ["https://token.place"]
    assert REMOVED_DIAGNOSTICS_CONFIGURED_UPSTREAM_ALIAS not in diagnostics_payload


def test_healthz_staging_relay_only_does_not_imply_prod_upstream(client, monkeypatch):
    """Staging relay-only health should be OK with zero registered external nodes."""
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://staging.token.place")
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setitem(app.config, "public_base_url", "https://staging.token.place")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["publicBaseUrl"] == "https://staging.token.place"
    assert payload["knownServers"] == 0
    assert payload["relayOnly"] is True
    assert payload["upstreamHealthRequired"] is False
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert payload["activeUpstreamServers"] == []
    assert payload["requiredUpstreamServers"] == []
    assert payload.get("details", {}).get("knownServers") == "empty"


def test_healthz_malformed_upstreams_env_keeps_default_configured(client, monkeypatch):
    """Malformed upstream list env should keep fallback default as configured-only."""
    monkeypatch.setenv("TOKEN_PLACE_RELAY_UPSTREAMS", '{"url":"https://ignored"}')
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://token.place"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relayOnly"] is True
    assert payload["activeUpstreamServers"] == []
    assert payload["requiredUpstreamServers"] == []
    assert payload["configuredUpstreamServers"] == ["https://token.place"]
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload


def test_healthz_custom_configured_servers_remain_configured(client, monkeypatch):
    """Custom configured server pools should be treated as explicit config."""
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAMS", raising=False)
    monkeypatch.delenv("PERSONAL_GAMING_PC_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.setenv("TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH", "0")
    monkeypatch.setitem(app.config, "relay_configured_servers", ["https://custom.upstream.example"])

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["relayOnly"] is False
    assert payload["activeUpstreamServers"] == ["https://custom.upstream.example"]
    assert payload["requiredUpstreamServers"] == []
    assert payload["configuredUpstreamServers"] == ["https://custom.upstream.example"]
    assert REMOVED_HEALTHZ_CONFIGURED_UPSTREAM_ALIAS not in payload


def test_relay_entrypoint_defaults_to_one_worker_and_multiple_threads():
    """Container entrypoint should keep one worker and default to thread concurrency."""
    entrypoint_path = Path(__file__).resolve().parents[1] / "docker" / "relay" / "entrypoint.sh"
    with entrypoint_path.open(encoding="utf-8") as file:
        content = file.read()
    assert 'WORKERS="${RELAY_WORKERS:-1}"' in content
    assert 'THREADS="${RELAY_THREADS:-4}"' in content

# --- Test /source ---

def test_source_submit_response(client):
    """Test server submitting a response via /source."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_encrypted_response_history",
        "cipherkey": "server_encrypted_aes_key",
        "iv": "server_iv"
    }
    response = client.post("/source", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['message'] == 'Response received and queued for client'

    # Check internal state
    assert DUMMY_CLIENT_PUB_KEY in client_responses
    queued_resp = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert queued_resp['chat_history'] == "server_encrypted_response_history"

def test_source_invalid_payload(client):
    """Test /source with missing fields."""
    payload = { "client_public_key": DUMMY_CLIENT_PUB_KEY } # Missing other fields
    response = client.post("/source", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid request data'

# --- Test /retrieve ---

def test_retrieve_get_response(client):
    """Test client retrieving a queued response via /retrieve."""
    # Queue a response first (directly modify state for setup)
    client_responses[DUMMY_CLIENT_PUB_KEY] = {
        'chat_history': "server_encrypted_response_history",
        'cipherkey': "server_encrypted_aes_key",
        'iv': "server_iv"
    }

    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200
    data = response.get_json()

    assert data['chat_history'] == "server_encrypted_response_history"
    assert data['cipherkey'] == "server_encrypted_aes_key"
    assert data['iv'] == "server_iv"

    # Check state - response should be removed after retrieval
    assert DUMMY_CLIENT_PUB_KEY not in client_responses

def test_retrieve_no_response_available(client):
    """Test /retrieve when no response is queued for the client."""
    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200 # Endpoint works, just no data
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'No response available for the given public key'

def test_retrieve_invalid_payload(client):
    """Test /retrieve with missing client public key."""
    response = client.post("/retrieve", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid request data'

# --- Integration Test ---

def test_full_relay_flow(client):
    """Test the full flow: register, faucet, sink poll, source, retrieve."""
    # 1. Server registers via /sink
    sink_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in known_servers

    # 2. Client requests inference via /faucet
    faucet_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "client_request_data",
        "cipherkey": "client_key_data",
        "iv": "client_iv_data"
    }
    response = client.post("/faucet", json=faucet_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    # 3. Server polls /sink and gets the request
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    sink_data = response.get_json()
    assert sink_data['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert sink_data['chat_history'] == "client_request_data"
    assert sink_data['cipherkey'] == "client_key_data"
    assert sink_data['iv'] == "client_iv_data"
    # Request should be removed from queue
    assert not client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])

    # 4. Server processes and submits response via /source
    source_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_response_data",
        "cipherkey": "server_key_data",
        "iv": "server_iv_data"
    }
    response = client.post("/source", json=source_payload)
    assert response.status_code == 200
    assert DUMMY_CLIENT_PUB_KEY in client_responses

    # 5. Client retrieves response via /retrieve
    retrieve_payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=retrieve_payload)
    assert response.status_code == 200
    retrieve_data = response.get_json()
    assert retrieve_data['chat_history'] == "server_response_data"
    assert retrieve_data['cipherkey'] == "server_key_data"
    assert retrieve_data['iv'] == "server_iv_data"


def test_streaming_state_lifecycle(client):
    """Streaming sessions should store and release chunk state per client."""
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': time.time(),
        'last_ping_duration': 10,
    }

    faucet_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector",
        "stream": True,
    }
    faucet_response = client.post("/faucet", json=faucet_payload)
    assert faucet_response.status_code == 200

    sink_response = client.post("/sink", json={"server_public_key": DUMMY_SERVER_PUB_KEY})
    assert sink_response.status_code == 200
    sink_data = sink_response.get_json()
    assert sink_data.get('stream') is True
    session_id = sink_data.get('stream_session_id')
    assert session_id
    assert session_id in streaming_sessions
    session_state = streaming_sessions[session_id]
    assert session_state['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert session_state['status'] == 'open'

    chunk_payload = {
        "session_id": session_id,
        "chunk": {"content": "hello"},
    }
    chunk_response = client.post("/stream/source", json=chunk_payload)
    assert chunk_response.status_code == 200

    retrieve_response = client.post(
        "/stream/retrieve", json={"client_public_key": DUMMY_CLIENT_PUB_KEY}
    )
    assert retrieve_response.status_code == 200
    retrieved = retrieve_response.get_json()
    assert retrieved["stream"] is True
    assert retrieved["session_id"] == session_id
    assert retrieved["chunks"] == [{"content": "hello"}]
    assert "final" not in retrieved

    final_payload = {
        "session_id": session_id,
        "chunk": {"content": "goodbye"},
        "final": True,
    }
    final_response = client.post("/stream/source", json=final_payload)
    assert final_response.status_code == 200

    final_retrieve = client.post(
        "/stream/retrieve", json={"client_public_key": DUMMY_CLIENT_PUB_KEY}
    )
    assert final_retrieve.status_code == 200
    final_data = final_retrieve.get_json()
    assert final_data["stream"] is True
    assert final_data["session_id"] == session_id
    assert final_data["chunks"] == [{"content": "goodbye"}]
    assert final_data["final"] is True

    assert session_id not in streaming_sessions
    assert DUMMY_CLIENT_PUB_KEY not in streaming_sessions_by_client
    # Response should be removed from queue
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_relay_route_contract_e2ee_flow(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200

    request_payload = {
        'request_id': 'req-123',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    }
    queued = client.post('/api/v1/relay/requests', json=request_payload)
    assert queued.status_code == 200

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    polled_payload = poll.get_json()
    assert polled_payload['chat_history'] == 'ciphertext-request'
    assert polled_payload['cipherkey'] == 'cipherkey-request'
    assert polled_payload['iv'] == 'iv-request'
    assert polled_payload['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert polled_payload['request_id'] == 'req-123'
    assert polled_payload['protocol'] == 'tokenplace_api_v1_relay_e2ee'
    assert polled_payload['version'] == 1

    response_payload = {
        'request_id': 'req-123',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response',
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    }
    source = client.post('/api/v1/relay/responses', json=response_payload)
    assert source.status_code == 200

    retrieved = client.post('/api/v1/relay/responses/retrieve', json={'client_public_key': DUMMY_CLIENT_PUB_KEY})
    assert retrieved.status_code == 200
    retrieved_payload = retrieved.get_json()
    assert retrieved_payload['chat_history'] == 'ciphertext-response'
    assert retrieved_payload['cipherkey'] == 'cipherkey-response'
    assert retrieved_payload['iv'] == 'iv-response'
    assert retrieved_payload['request_id'] == 'req-123'
    assert retrieved_payload['protocol'] == 'tokenplace_api_v1_relay_e2ee'


def test_queue_client_response_serializes_concurrent_updates(monkeypatch):
    class SlowSnapshotDict(dict):
        def __init__(self):
            super().__init__()
            self._active_lock = threading.Lock()
            self._active_gets = 0
            self.concurrent_get_seen = False

        def get(self, key, default=None):
            value = super().get(key, default)
            with self._active_lock:
                self._active_gets += 1
                if self._active_gets > 1:
                    self.concurrent_get_seen = True
            time.sleep(0.05)
            with self._active_lock:
                self._active_gets -= 1
            return value

    response_queue = SlowSnapshotDict()
    monkeypatch.setattr(relay_module, "client_responses", response_queue)
    envelopes = [
        {
            'request_id': 'req-1',
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'chat_history': 'ciphertext-response-1',
            'cipherkey': 'cipherkey-response-1',
            'iv': 'iv-response-1',
        },
        {
            'request_id': 'req-2',
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'chat_history': 'ciphertext-response-2',
            'cipherkey': 'cipherkey-response-2',
            'iv': 'iv-response-2',
        },
    ]
    threads = [
        threading.Thread(
            target=relay_module._queue_client_response,
            args=(DUMMY_CLIENT_PUB_KEY, envelope),
        )
        for envelope in envelopes
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert response_queue.concurrent_get_seen is False
    queued = response_queue[DUMMY_CLIENT_PUB_KEY]
    assert isinstance(queued, list)
    assert sorted(item['request_id'] for item in queued) == ['req-1', 'req-2']


def test_api_v1_response_retrieve_matches_request_id_without_dropping_other_responses(client):
    response_one = {
        'request_id': 'req-1',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response-1',
        'cipherkey': 'cipherkey-response-1',
        'iv': 'iv-response-1',
    }
    response_two = {
        'request_id': 'req-2',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response-2',
        'cipherkey': 'cipherkey-response-2',
        'iv': 'iv-response-2',
    }

    assert client.post('/api/v1/relay/responses', json=response_one).status_code == 200
    assert client.post('/api/v1/relay/responses', json=response_two).status_code == 200

    missing = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-missing'},
    )
    assert missing.status_code == 404
    assert len(client_responses[DUMMY_CLIENT_PUB_KEY]) == 2

    retrieved_two = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-2'},
    )
    assert retrieved_two.status_code == 200
    assert retrieved_two.get_json()['request_id'] == 'req-2'
    assert client_responses[DUMMY_CLIENT_PUB_KEY]['request_id'] == 'req-1'

    retrieved_one = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-1'},
    )
    assert retrieved_one.status_code == 200
    assert retrieved_one.get_json()['request_id'] == 'req-1'
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_response_retrieve_returns_pending_for_known_request_id(client):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    queued = client.post(
        '/api/v1/relay/requests',
        json={
            'request_id': 'req-pending',
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'server_public_key': DUMMY_SERVER_PUB_KEY,
            'chat_history': 'ciphertext-request',
            'cipherkey': 'cipherkey-request',
            'iv': 'iv-request',
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'version': 1,
        },
    )
    assert queued.status_code == 200

    pending = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-pending'},
    )
    assert pending.status_code == 202
    assert pending.get_json() == {'status': 'pending'}


def test_api_v1_response_retrieve_stays_pending_for_long_running_valid_interval(client, monkeypatch):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    queued = client.post(
        '/api/v1/relay/requests',
        json={
            'request_id': 'req-long-running',
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'server_public_key': DUMMY_SERVER_PUB_KEY,
            'chat_history': 'ciphertext-request',
            'cipherkey': 'cipherkey-request',
            'iv': 'iv-request',
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'version': 1,
        },
    )
    assert queued.status_code == 200

    pending_entry = client_pending_request_ids[DUMMY_CLIENT_PUB_KEY]['req-long-running']
    queued_at = pending_entry['queued_at'] if isinstance(pending_entry, dict) else pending_entry
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 300.0)
    monkeypatch.setattr(relay_module.time, 'time', lambda: queued_at + 299.0)

    pending = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-long-running'},
    )
    assert pending.status_code == 202
    assert pending.get_json() == {'status': 'pending'}


def test_api_v1_response_retrieve_returns_terminal_after_unregistered_server_drops_queue(client):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    queued = client.post(
        '/api/v1/relay/requests',
        json={
            'request_id': 'req-abandoned',
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'server_public_key': DUMMY_SERVER_PUB_KEY,
            'chat_history': 'ciphertext-request',
            'cipherkey': 'cipherkey-request',
            'iv': 'iv-request',
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'version': 1,
        },
    )
    assert queued.status_code == 200

    pending = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-abandoned'},
    )
    assert pending.status_code == 202
    assert pending.get_json() == {'status': 'pending'}

    unregistered = client.post('/api/v1/relay/servers/unregister', json={'server_public_key': DUMMY_SERVER_PUB_KEY, 'control_credential': register.get_json()['control_credential']})
    assert unregistered.status_code == 200

    unknown = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-abandoned'},
    )
    assert unknown.status_code == 410
    assert unknown.get_json()['error']['status'] == 'cancelled'
    assert unknown.get_json()['error']['reason'] == 'server_unregistered'


def test_api_v1_response_retrieve_request_id_mismatch_keeps_single_response(client):
    response_payload = {
        'request_id': 'req-1',
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response',
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    }
    assert client.post('/api/v1/relay/responses', json=response_payload).status_code == 200

    mismatch = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-other'},
    )
    assert mismatch.status_code == 404
    assert client_responses[DUMMY_CLIENT_PUB_KEY]['request_id'] == 'req-1'

    retrieved = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-1'},
    )
    assert retrieved.status_code == 200
    assert retrieved.get_json()['request_id'] == 'req-1'
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_relay_plaintext_messages_are_rejected(client):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    plaintext = 'PLAINTEXT_SENTINEL_DO_NOT_STORE'
    payload = {
        'request_id': 'req-no-messages',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-only',
        'cipherkey': 'cipherkey-only',
        'iv': 'iv-only',
        'messages': [{'role': 'user', 'content': plaintext}],
        'prompt': plaintext,
    }
    response = client.post('/api/v1/relay/requests', json=payload)
    assert response.status_code == 400
    assert "forbidden; send ciphertext envelope only" in response.get_json()["error"]["message"]
    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests


def test_api_v1_relay_requests_requires_client_public_key(client):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    response = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-missing-client-key',
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'ciphertext': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })

    assert response.status_code == 400
    assert response.get_json() == {'error': {'message': 'Missing client public key', 'code': 400}}


def test_api_v1_register_and_poll_do_not_delegate_to_legacy_sink(client, monkeypatch):
    def _sink_should_not_be_called():
        raise AssertionError('legacy sink() should not be called by API v1 register/poll')

    monkeypatch.setattr('relay.sink', _sink_should_not_be_called)

    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200

    queued = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-no-sink-delegation',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'ciphertext': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })
    assert queued.status_code == 200

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    polled = poll.get_json()
    assert polled['request_id'] == 'req-no-sink-delegation'


def test_api_v1_register_advertises_configured_poll_wait(client, monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS', '30')
    response = register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['next_ping_in_x_seconds'] == 30
    assert payload['poll_wait_seconds'] == 30.0


def test_api_v1_poll_skips_legacy_queue_items_and_claims_e2ee_only(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200

    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [
        {
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'chat_history': 'legacy-plaintext',
            'cipherkey': 'legacy-key',
            'iv': 'legacy-iv',
        },
        {
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'chat_history': 'ciphertext-request',
            'cipherkey': 'cipherkey-request',
            'iv': 'iv-request',
            'request_id': 'req-e2ee-only',
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'version': 1,
            'e2ee_v1': True,
        },
    ]

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    payload = poll.get_json()
    assert payload['request_id'] == 'req-e2ee-only'
    assert payload['chat_history'] == 'ciphertext-request'
    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests


def test_api_v1_relay_response_plaintext_is_rejected(client):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    plaintext = 'PLAINTEXT_RESPONSE_SENTINEL_DO_NOT_STORE'
    response_payload = {
        'request_id': 'req-response-no-plaintext',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response-only',
        'cipherkey': 'cipherkey-response-only',
        'iv': 'iv-response-only',
        'messages': [{'role': 'assistant', 'content': plaintext}],
        'prompt': plaintext,
        'assistant_output': plaintext,
        'tool_arguments': plaintext,
        'model_output_text': plaintext,
    }
    source = client.post('/api/v1/relay/responses', json=response_payload)
    assert source.status_code == 400
    assert "forbidden; send ciphertext envelope only" in source.get_json()["error"]["message"]
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


def test_api_v1_relay_chat_completions_fail_closed_and_queue_unchanged(client):
    client_inference_requests.clear()
    response = client.post('/relay/api/v1/chat/completions', json={
        'model': 'x',
        'messages': [{'role': 'user', 'content': 'should-not-queue'}],
    })
    assert response.status_code == 503
    assert client_inference_requests == {}


def test_api_v1_register_does_not_dequeue_requests(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200

    request_payload = {
        'request_id': 'req-register-heartbeat',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    }
    queued = client.post('/api/v1/relay/requests', json=request_payload)
    assert queued.status_code == 200

    heartbeat = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert heartbeat.status_code == 200

    # Register/heartbeat should not claim work.
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    claimed = poll.get_json()
    assert claimed['request_id'] == 'req-register-heartbeat'
    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests or len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 0


def test_api_v1_poll_requires_registration_token_when_configured(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 10,
    }
    client_inference_requests[DUMMY_SERVER_PUB_KEY] = [{
        'request_id': 'req-auth',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
        'e2ee_v1': True,
    }]

    monkeypatch.setattr(relay_module, 'SERVER_REGISTRATION_TOKENS', ['expected-token'])

    unauthorized = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert unauthorized.status_code == 401
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    authorized = client.post(
        '/api/v1/relay/servers/poll',
        json=server_payload,
        headers={'X-Relay-Server-Token': 'expected-token'},
    )
    assert authorized.status_code == 200
    assert authorized.get_json()['request_id'] == 'req-auth'


def test_legacy_relay_routes_return_410_by_default(client, monkeypatch):
    """Legacy relay routes fail closed with 410 unless compatibility is enabled."""
    monkeypatch.delenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", raising=False)

    checks = [
        ("get", "/next_server", None),
        ("post", "/sink", {"server_public_key": DUMMY_SERVER_PUB_KEY}),
        ("post", "/faucet", {"client_public_key": DUMMY_CLIENT_PUB_KEY, "server_public_key": DUMMY_SERVER_PUB_KEY, "chat_history": "x", "cipherkey": "y", "iv": "z"}),
        ("post", "/source", {"client_public_key": DUMMY_CLIENT_PUB_KEY, "server_public_key": DUMMY_SERVER_PUB_KEY, "chat_history": "x", "cipherkey": "y", "iv": "z"}),
        ("post", "/retrieve", {"client_public_key": DUMMY_CLIENT_PUB_KEY}),
    ]

    for method, route, payload in checks:
        if method == "get":
            response = client.get(route)
        else:
            response = client.post(route, json=payload)
        assert response.status_code == 410
        body = response.get_json()
        assert body["error"]["code"] == "legacy_relay_endpoint_deprecated"


def test_legacy_next_server_can_be_enabled_with_compatibility_flag(client, monkeypatch):
    """Compatibility flag restores legacy next_server behavior where still supported."""
    monkeypatch.setenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", "1")

    known_servers[DUMMY_SERVER_PUB_KEY] = {
        "public_key": DUMMY_SERVER_PUB_KEY,
        "last_ping": datetime.now(),
        "last_ping_duration": 10,
    }
    response = client.get("/next_server")
    assert response.status_code == 200
    assert response.get_json()["server_public_key"] == DUMMY_SERVER_PUB_KEY


def test_api_v1_provider_envelope_is_queued_polled_responded_and_retrieved_ciphertext_only(client):
    register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    request_plaintext = 'PLAINTEXT_REQUEST_SENTINEL_DO_NOT_STORE'
    request_payload = {
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'request_id': 'req-provider-style',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'ciphertext': 'ciphertext-request-provider-style',
        'cipherkey': 'cipherkey-request-provider-style',
        'iv': 'iv-request-provider-style',
    }

    queued = client.post('/api/v1/relay/requests', json=request_payload)
    assert queued.status_code == 200
    relay_state = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert relay_state['protocol'] == 'tokenplace_api_v1_relay_e2ee'
    assert relay_state['version'] == 1
    assert relay_state['request_id'] == 'req-provider-style'
    assert relay_state['e2ee_v1'] is True
    assert 'messages' not in relay_state
    assert request_plaintext not in json.dumps(relay_state)

    polled = client.post(
        '/api/v1/relay/servers/poll',
        json={'server_public_key': DUMMY_SERVER_PUB_KEY},
    )
    assert polled.status_code == 200
    polled_payload = polled.get_json()
    assert polled_payload['protocol'] == 'tokenplace_api_v1_relay_e2ee'
    assert polled_payload['version'] == 1
    assert polled_payload['request_id'] == 'req-provider-style'
    assert polled_payload['chat_history'] == 'ciphertext-request-provider-style'
    assert request_plaintext not in json.dumps(polled_payload)

    response_plaintext = 'PLAINTEXT_RESPONSE_SENTINEL_DO_NOT_STORE'
    response_payload = {
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'request_id': 'req-provider-style',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'ciphertext': 'ciphertext-response-provider-style',
        'cipherkey': 'cipherkey-response-provider-style',
        'iv': 'iv-response-provider-style',
    }
    submitted = client.post('/api/v1/relay/responses', json=response_payload)
    assert submitted.status_code == 200
    queued_response = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert queued_response['protocol'] == 'tokenplace_api_v1_relay_e2ee'
    assert queued_response['request_id'] == 'req-provider-style'
    assert 'api_v1_response' not in queued_response
    assert response_plaintext not in json.dumps(queued_response)

    retrieved = client.post(
        '/api/v1/relay/responses/retrieve',
        json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': 'req-provider-style'},
    )
    assert retrieved.status_code == 200
    retrieved_payload = retrieved.get_json()
    assert retrieved_payload['request_id'] == 'req-provider-style'
    assert retrieved_payload['chat_history'] == 'ciphertext-response-provider-style'
    assert response_plaintext not in json.dumps(retrieved_payload)


def test_api_v1_poll_clears_popped_work_if_server_unregistered_before_dispatch(client, monkeypatch):
    server_payload = _api_v1_registered_control_payload(client, DUMMY_SERVER_PUB_KEY, capabilities=_capabilities('8k-fast'))

    queued = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-requeue-on-unregister-race',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })
    assert queued.status_code == 200

    original_pop = relay_module._pop_next_api_v1_request

    def _pop_then_unregister(public_key):
        popped = original_pop(public_key)
        if popped is not None:
            relay_module._record_api_v1_server_unregistered(public_key)
            relay_module._remove_known_server(public_key)
        return popped

    monkeypatch.setattr(relay_module, '_pop_next_api_v1_request', _pop_then_unregister)

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 404

    assert DUMMY_SERVER_PUB_KEY not in client_inference_requests
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids

    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': 'req-requeue-on-unregister-race',
    })
    assert retrieved.status_code == 410
    assert retrieved.get_json()['error'] == {
        'message': 'Request cancelled',
        'code': 'cancelled',
        'status': 'cancelled',
        'reason': 'server_unregistered',
    }


def test_api_v1_poll_long_wait_dispatches_when_request_arrives(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY, 'capabilities': _capabilities('8k-fast')}
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS', '0.5')

    result = {}

    def _poll():
        with app.test_client() as polling_client:
            response = polling_client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
            result['status'] = response.status_code
            result['json'] = response.get_json()

    poll_thread = threading.Thread(target=_poll)
    poll_thread.start()
    time.sleep(0.05)

    queued = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-long-poll-dispatch',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })
    assert queued.status_code == 200

    poll_thread.join(timeout=1.0)
    assert not poll_thread.is_alive()
    assert result['status'] == 200
    assert result['json']['request_id'] == 'req-long-poll-dispatch'
    assert '_queued_at' not in result['json']


def test_api_v1_poll_long_wait_timeout_returns_no_work(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY, 'capabilities': _capabilities('8k-fast')}
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS', '0.01')

    started = time.monotonic()
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    elapsed = time.monotonic() - started
    assert poll.status_code == 200
    payload = poll.get_json()
    assert payload['message'] == 'No requests available'
    assert payload['next_ping_in_x_seconds'] == 0
    assert payload['poll_wait_seconds'] == 0.01
    assert elapsed >= 0.008


def test_api_v1_poll_delivers_fifo_for_multiple_requests(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY, 'capabilities': _capabilities('8k-fast')}
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200

    for request_id in ("req-fifo-1", "req-fifo-2"):
        queued = client.post('/api/v1/relay/requests', json={
            'request_id': request_id,
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'server_public_key': DUMMY_SERVER_PUB_KEY,
            'chat_history': f'ciphertext-{request_id}',
            'cipherkey': 'cipherkey-request',
            'iv': 'iv-request',
        })
        assert queued.status_code == 200

    first = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    second = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()['request_id'] == 'req-fifo-1'
    assert second.get_json()['request_id'] == 'req-fifo-2'


def test_api_v1_poll_refreshes_server_lease(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200
    time.sleep(0.6)
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    time.sleep(0.6)
    assert client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY}).status_code == 200


def test_api_v1_stale_server_expires_without_poll_heartbeat(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS', '1')
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200
    known_servers[DUMMY_SERVER_PUB_KEY]['last_ping'] = datetime.now() - timedelta(seconds=2)
    expired = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert expired.status_code == 404


def test_api_v1_poll_long_wait_ignores_unrelated_server_wakeups(client, monkeypatch):
    server_one = base64.b64encode(b"server_public_key_1").decode("utf-8")
    server_two = base64.b64encode(b"server_public_key_2").decode("utf-8")
    assert client.post('/api/v1/relay/servers/register', json={'server_public_key': server_one}).status_code == 200
    assert client.post('/api/v1/relay/servers/register', json={'server_public_key': server_two}).status_code == 200
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS', '0.25')

    result = {}

    def _poll_server_one():
        with app.test_client() as polling_client:
            response = polling_client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_one})
            result['status'] = response.status_code
            result['json'] = response.get_json()

    poll_thread = threading.Thread(target=_poll_server_one)
    poll_thread.start()
    time.sleep(0.05)

    queued = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-for-server-two',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': server_two,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })
    assert queued.status_code == 200

    time.sleep(0.05)
    assert poll_thread.is_alive()

    poll_server_two = client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_two})
    assert poll_server_two.status_code == 200
    assert poll_server_two.get_json()['request_id'] == 'req-for-server-two'

    poll_thread.join(timeout=0.5)
    assert not poll_thread.is_alive()
    assert result['status'] == 200
    assert result['json']['message'] == 'No requests available'


def test_api_v1_poll_long_wait_wakes_on_shared_queue_legacy_compat_enqueue(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY, 'capabilities': _capabilities('8k-fast')}
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS', '0.5')

    result = {}

    def _poll():
        with app.test_client() as polling_client:
            response = polling_client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
            result['status'] = response.status_code
            result['json'] = response.get_json()

    poll_thread = threading.Thread(target=_poll)
    poll_thread.start()
    time.sleep(0.05)

    queued = client.post('/faucet', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'legacy-ciphertext-request',
        'cipherkey': 'legacy-cipherkey-request',
        'iv': 'legacy-iv-request',
    })
    assert queued.status_code == 200

    poll_thread.join(timeout=1.0)
    assert not poll_thread.is_alive()
    assert result['status'] == 200
    assert result['json']['chat_history'] == 'legacy-ciphertext-request'


def test_api_v1_next_selects_single_fresh_api_v1_node(client):
    server_key = _server_key('single_fresh_api_v1')
    _register_api_v1_server(client, server_key)

    selections = [_next_api_v1_server_key(client) for _ in range(3)]

    assert selections == [server_key, server_key, server_key]


def test_api_v1_next_round_robins_two_registered_compute_nodes(client):
    server_a = _server_key('round_robin_a')
    server_b = _server_key('round_robin_b')
    _register_api_v1_server(client, server_a)
    _register_api_v1_server(client, server_b)

    selections = [_next_api_v1_server_key(client) for _ in range(4)]

    assert selections == [server_a, server_b, server_a, server_b]


def test_api_v1_next_round_robins_three_registered_compute_nodes(client):
    server_a = _server_key('round_robin_three_a')
    server_b = _server_key('round_robin_three_b')
    server_c = _server_key('round_robin_three_c')
    control_payloads = {}
    for server_key in (server_a, server_b, server_c):
        control_payloads[server_key] = _api_v1_registered_control_payload(client, server_key, capabilities=_capabilities('8k-fast'))

    selections = [_next_api_v1_server_key(client) for _ in range(6)]

    assert selections == [server_a, server_b, server_c, server_a, server_b, server_c]


def test_api_v1_next_ignores_legacy_sink_registered_nodes(client):
    legacy_server = _server_key('legacy_only')
    api_server_a = _server_key('api_filter_a')
    api_server_b = _server_key('api_filter_b')

    legacy_registration = client.post('/sink', json={'server_public_key': legacy_server})
    assert legacy_registration.status_code == 200
    _register_api_v1_server(client, api_server_a)
    _register_api_v1_server(client, api_server_b)

    selections = [_next_api_v1_server_key(client) for _ in range(4)]

    assert selections == [api_server_a, api_server_b, api_server_a, api_server_b]
    assert legacy_server not in selections


def test_api_v1_round_robin_preserves_next_node_after_selected_server_unregisters(client):
    server_a = _server_key('cursor_selected_removed_a')
    server_b = _server_key('cursor_selected_removed_b')
    server_c = _server_key('cursor_selected_removed_c')
    control_payloads = {}
    for server_key in (server_a, server_b, server_c):
        control_payloads[server_key] = _api_v1_registered_control_payload(client, server_key, capabilities=_capabilities('8k-fast'))

    assert _next_api_v1_server_key(client) == server_a

    unregistered = client.post('/api/v1/relay/servers/unregister', json=control_payloads[server_a])
    assert unregistered.status_code == 200
    assert unregistered.get_json()['removed'] is True

    assert [_next_api_v1_server_key(client) for _ in range(4)] == [
        server_b,
        server_c,
        server_b,
        server_c,
    ]


def test_api_v1_round_robin_preserves_next_node_after_earlier_server_eviction(client, monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS', '1')
    server_a = _server_key('cursor_evicted_a')
    server_b = _server_key('cursor_evicted_b')
    server_c = _server_key('cursor_evicted_c')
    for server_key in (server_a, server_b, server_c):
        _register_api_v1_server(client, server_key)

    assert _next_api_v1_server_key(client) == server_a
    known_servers[server_a]['last_ping'] = datetime.now() - timedelta(seconds=5)

    assert [_next_api_v1_server_key(client) for _ in range(4)] == [
        server_b,
        server_c,
        server_b,
        server_c,
    ]
    assert server_a not in known_servers


def test_api_v1_round_robin_does_not_skip_after_next_cursor_target_expires(client, monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS', '1')
    server_a = _server_key('cursor_target_expired_a')
    server_b = _server_key('cursor_target_expired_b')
    server_c = _server_key('cursor_target_expired_c')
    for server_key in (server_a, server_b, server_c):
        _register_api_v1_server(client, server_key)

    assert _next_api_v1_server_key(client) == server_a
    known_servers[server_b]['last_ping'] = datetime.now() - timedelta(seconds=5)

    assert [_next_api_v1_server_key(client) for _ in range(4)] == [
        server_c,
        server_a,
        server_c,
        server_a,
    ]
    assert server_b not in known_servers



def test_api_v1_poll_marks_claimed_request_terminal_if_server_removed(client, monkeypatch):
    server_key = _server_key('poll_removed')
    request_id = 'req-poll-removed'
    _register_api_v1_server(client, server_key)
    relay_module._mark_request_pending(DUMMY_CLIENT_PUB_KEY, request_id, cancel_token='proof')
    client_inference_requests[server_key] = [{
        'request_id': request_id,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
        'e2ee_v1': True,
    }]

    def pop_and_remove(public_key):
        queued_requests = client_inference_requests.get(public_key, [])
        claimed = queued_requests.pop(0) if queued_requests else None
        if not queued_requests:
            client_inference_requests.pop(public_key, None)
        relay_module._remove_known_server(public_key)
        return claimed

    terminalized_without_server_lock = []
    original_cancel = relay_module._cancel_api_v1_request

    def cancel_without_server_lock(*args, **kwargs):
        terminalized_without_server_lock.append(
            not relay_module.server_round_robin_lock._is_owned()
        )
        return original_cancel(*args, **kwargs)

    monkeypatch.setattr(relay_module, '_pop_next_api_v1_request', pop_and_remove)
    monkeypatch.setattr(relay_module, '_cancel_api_v1_request', cancel_without_server_lock)

    response = client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_key})

    assert response.status_code == 404
    assert response.get_json()['error']['code'] == 404
    assert terminalized_without_server_lock == [True]
    assert server_key not in known_servers
    assert server_key not in client_inference_requests
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids

    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })
    assert retrieved.status_code == 410
    assert retrieved.get_json()['error'] == {
        'message': 'Request expired',
        'code': 'expired',
        'status': 'expired',
        'reason': 'provider_timeout',
    }

    late_response = client.post('/api/v1/relay/responses', json={
        'request_id': request_id,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response',
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    })
    assert late_response.status_code == 410
    assert late_response.get_json()['error']['status'] == 'expired'
    assert DUMMY_CLIENT_PUB_KEY not in client_responses


class _LockCheckingKnownServers(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values_checked_under_server_lock = False

    def values(self):
        if relay_module.server_round_robin_lock._is_owned():
            self.values_checked_under_server_lock = True
        return super().values()


def test_api_v1_cancel_token_lookup_scans_known_servers_under_registry_lock(client, monkeypatch):
    request_id = 'req-cancel-token-lock-scan'
    checking_servers = _LockCheckingKnownServers({
        DUMMY_SERVER_PUB_KEY: {
            'public_key': DUMMY_SERVER_PUB_KEY,
            'last_ping': datetime.now(),
            'last_ping_duration': 60,
            relay_module.API_V1_SERVER_MARKER: True,
            'api_v1_in_flight_requests': {
                request_id: {
                    'expires_at': time.monotonic() + 60,
                    'client_public_key': DUMMY_CLIENT_PUB_KEY,
                    'cancel_token': 'proof',
                },
            },
        },
    })
    monkeypatch.setattr(relay_module, 'known_servers', checking_servers)

    token = relay_module._cancel_token_for_queued_or_in_flight_request(
        DUMMY_CLIENT_PUB_KEY,
        request_id,
    )

    assert token == 'proof'
    assert checking_servers.values_checked_under_server_lock is True


def test_api_v1_cancel_scans_known_servers_under_registry_lock(client, monkeypatch):
    request_id = 'req-cancel-lock-scan'
    checking_servers = _LockCheckingKnownServers({
        DUMMY_SERVER_PUB_KEY: {
            'public_key': DUMMY_SERVER_PUB_KEY,
            'last_ping': datetime.now(),
            'last_ping_duration': 60,
            relay_module.API_V1_SERVER_MARKER: True,
            'api_v1_in_flight_requests': {
                request_id: {
                    'expires_at': time.monotonic() + 60,
                    'client_public_key': DUMMY_CLIENT_PUB_KEY,
                    'cancel_token': 'proof',
                },
            },
        },
    })
    monkeypatch.setattr(relay_module, 'known_servers', checking_servers)
    relay_module._mark_request_pending(DUMMY_CLIENT_PUB_KEY, request_id, cancel_token='proof')

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
        'status': 'cancelled',
        'reason': 'requester_cancelled',
        'cancel_token': 'proof',
    })

    assert cancelled.status_code == 200
    assert checking_servers.values_checked_under_server_lock is True
    assert 'api_v1_in_flight_requests' not in checking_servers[DUMMY_SERVER_PUB_KEY]


def test_api_v1_request_enqueue_rejects_legacy_only_server_without_queue_entry(client):
    legacy_server = _server_key('enqueue_legacy_only')
    legacy_registration = client.post('/sink', json={'server_public_key': legacy_server})
    assert legacy_registration.status_code == 200

    response = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-legacy-only',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': legacy_server,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })

    assert response.status_code == 404
    assert legacy_server not in client_inference_requests
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids


def test_api_v1_request_enqueue_rejects_removed_server_without_queue_entry(client):
    server_key = _server_key('enqueue_removed')
    control_payload = _api_v1_registered_control_payload(client, server_key, capabilities=_capabilities('8k-fast'))
    assert client.post('/api/v1/relay/servers/unregister', json=control_payload).status_code == 200

    response = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-removed-server',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': server_key,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })

    assert response.status_code == 404
    assert server_key not in client_inference_requests
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids


def test_api_v1_round_robin_request_queueing_preserves_per_server_isolation(client):
    server_a = _server_key('queue_a')
    server_b = _server_key('queue_b')
    _register_api_v1_server(client, server_a)
    _register_api_v1_server(client, server_b)

    selected_servers = []
    for idx in range(4):
        selected_server = _next_api_v1_server_key(client)
        selected_servers.append(selected_server)
        _queue_api_v1_request(
            client,
            server_public_key=selected_server,
            request_id=f'req-round-robin-{idx}',
        )

    assert selected_servers == [server_a, server_b, server_b, server_a]

    first_a = client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_a})
    second_a = client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_a})
    first_b = client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_b})
    second_b = client.post('/api/v1/relay/servers/poll', json={'server_public_key': server_b})

    assert [first_a.status_code, second_a.status_code, first_b.status_code, second_b.status_code] == [200] * 4
    assert first_a.get_json()['request_id'] == 'req-round-robin-0'
    assert second_a.get_json()['request_id'] == 'req-round-robin-3'
    assert first_b.get_json()['request_id'] == 'req-round-robin-1'
    assert second_b.get_json()['request_id'] == 'req-round-robin-2'


def test_api_v1_round_robin_skips_expired_and_unregistered_nodes(client, monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS', '1')
    server_a = _server_key('skip_a')
    server_b = _server_key('skip_b')
    server_c = _server_key('skip_c')
    control_payloads = {}
    for server_key in (server_a, server_b, server_c):
        control_payloads[server_key] = _api_v1_registered_control_payload(client, server_key, capabilities=_capabilities('8k-fast'))

    assert [_next_api_v1_server_key(client) for _ in range(3)] == [server_a, server_b, server_c]

    known_servers[server_b]['last_ping'] = datetime.now() - timedelta(seconds=5)
    assert [_next_api_v1_server_key(client) for _ in range(4)] == [server_a, server_c, server_a, server_c]
    assert server_b not in known_servers

    unregistered = client.post('/api/v1/relay/servers/unregister', json=control_payloads[server_a])
    assert unregistered.status_code == 200
    assert unregistered.get_json()['removed'] is True
    assert [_next_api_v1_server_key(client) for _ in range(2)] == [server_c, server_c]


def test_api_v1_reregistered_round_robin_node_reenters_at_end(client):
    server_a = _server_key('reregister_a')
    server_b = _server_key('reregister_b')
    server_c = _server_key('reregister_c')
    control_payloads = {
        server_a: _api_v1_registered_control_payload(client, server_a, capabilities=_capabilities('8k-fast')),
        server_b: _api_v1_registered_control_payload(client, server_b, capabilities=_capabilities('8k-fast')),
        server_c: _api_v1_registered_control_payload(client, server_c, capabilities=_capabilities('8k-fast')),
    }

    assert client.post('/api/v1/relay/servers/unregister', json=control_payloads[server_b]).status_code == 200
    _register_api_v1_server(client, server_b)

    assert [_next_api_v1_server_key(client) for _ in range(6)] == [
        server_a,
        server_c,
        server_b,
        server_a,
        server_c,
        server_b,
    ]


def test_api_v1_round_robin_selection_is_concurrency_safe(client):
    server_a = _server_key('concurrent_a')
    server_b = _server_key('concurrent_b')
    _register_api_v1_server(client, server_a)
    _register_api_v1_server(client, server_b)

    selections = []
    lock = threading.Lock()

    def select_next():
        with app.test_client() as thread_client:
            selected = _next_api_v1_server_key(thread_client)
        with lock:
            selections.append(selected)

    threads = [threading.Thread(target=select_next) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    assert len(selections) == 20
    assert selections.count(server_a) == 10
    assert selections.count(server_b) == 10


def test_api_v1_next_keeps_in_flight_server_alive_then_expires(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS', '3')
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200

    queued = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-inflight-1',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })
    assert queued.status_code == 200

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == 'req-inflight-1'

    time.sleep(1.2)
    next_response = client.get('/api/v1/relay/servers/next')
    assert next_response.status_code == 503
    assert next_response.get_json()['error']['code'] == 'no_matching_compute_node'

    time.sleep(2.1)
    expired = client.get('/api/v1/relay/servers/next')
    assert expired.status_code == 503




def test_api_v1_next_does_not_keep_stale_server_alive_after_in_flight_response_removed(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS', '10')
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200

    queued = client.post('/api/v1/relay/requests', json={
        'request_id': 'req-race-finished',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    })
    assert queued.status_code == 200

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == 'req-race-finished'

    # Complete/remove the only in-flight request, then force stale lease.
    response = client.post('/api/v1/relay/responses', json={
        'request_id': 'req-race-finished',
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response',
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    })
    assert response.status_code == 200

    known_servers[DUMMY_SERVER_PUB_KEY]['last_ping'] = datetime.now() - timedelta(seconds=5)

    next_response = client.get('/api/v1/relay/servers/next')
    assert next_response.status_code == 503


def test_api_v1_next_keeps_server_alive_while_any_in_flight_request_remains(client, monkeypatch):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    monkeypatch.setenv('TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS', '1')
    monkeypatch.setenv('TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS', '3')
    assert client.post('/api/v1/relay/servers/register', json=server_payload).status_code == 200

    for request_id in ('req-inflight-a', 'req-inflight-b'):
        queued = client.post('/api/v1/relay/requests', json={
            'request_id': request_id,
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'server_public_key': DUMMY_SERVER_PUB_KEY,
            'chat_history': f'ciphertext-{request_id}',
            'cipherkey': 'cipherkey-request',
            'iv': 'iv-request',
        })
        assert queued.status_code == 200

    first_poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    second_poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert first_poll.status_code == 200
    assert second_poll.status_code == 200

    first_request_id = first_poll.get_json()['request_id']
    second_request_id = second_poll.get_json()['request_id']
    assert {first_request_id, second_request_id} == {'req-inflight-a', 'req-inflight-b'}

    response = client.post('/api/v1/relay/responses', json={
        'request_id': second_request_id,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'chat_history': 'ciphertext-response',
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    })
    assert response.status_code == 200

    time.sleep(1.2)
    next_response = client.get('/api/v1/relay/servers/next')
    assert next_response.status_code == 503
    assert next_response.get_json()['error']['code'] == 'no_matching_compute_node'


def test_api_v1_unregister_removes_known_server_and_next_skips_it(client):
    server_payload = _api_v1_registered_control_payload(client, DUMMY_SERVER_PUB_KEY, capabilities=_capabilities('8k-fast'))
    assert client.get('/api/v1/relay/servers/next').status_code == 200

    unregistered = client.post('/api/v1/relay/servers/unregister', json=server_payload)

    assert unregistered.status_code == 200
    assert unregistered.get_json()['removed'] is True
    diagnostics = client.get('/relay/diagnostics').get_json()
    assert diagnostics['total_registered_compute_nodes'] == 0
    next_response = client.get('/api/v1/relay/servers/next')
    assert next_response.status_code == 503


def test_api_v1_unregister_is_idempotent_when_server_already_gone(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}

    first = client.post('/api/v1/relay/servers/unregister', json=server_payload)
    second = client.post('/api/v1/relay/servers/unregister', json=server_payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()['removed'] is False
    assert second.get_json()['removed'] is False


def test_api_v1_unregister_cancels_in_flight_request_promptly(client):
    server_payload = _api_v1_registered_control_payload(client, DUMMY_SERVER_PUB_KEY)
    request_id = 'req-inflight-unregister'
    assert client.post('/api/v1/relay/requests', json={
        'request_id': request_id,
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
    }).status_code == 200
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == request_id

    unregistered = client.post('/api/v1/relay/servers/unregister', json=server_payload)

    assert unregistered.status_code == 200
    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })
    assert retrieved.status_code == 410
    assert retrieved.get_json()['error']['status'] == 'cancelled'
    assert retrieved.get_json()['error']['reason'] == 'server_unregistered'



def _api_v1_request_payload(request_id, *, client_public_key=DUMMY_CLIENT_PUB_KEY, cancel_token="cancel-proof"):
    return {
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'client_public_key': client_public_key,
        'request_id': request_id,
        'protocol': 'tokenplace_api_v1_relay_e2ee',
        'version': 1,
        'chat_history': 'ciphertext-request',
        'cipherkey': 'cipherkey-request',
        'iv': 'iv-request',
        'cancel_token': cancel_token,
    }


def _api_v1_response_payload(request_id, *, client_public_key=DUMMY_CLIENT_PUB_KEY, ciphertext='ciphertext-response'):
    return {
        'client_public_key': client_public_key,
        'request_id': request_id,
        'chat_history': ciphertext,
        'cipherkey': 'cipherkey-response',
        'iv': 'iv-response',
    }


def test_api_v1_expired_pending_response_submission_returns_gone(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 1.0)
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_SECONDS_ENV, '1')
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    queued = client.post('/api/v1/relay/requests', json=_api_v1_request_payload('req-expired-late-response'))
    assert queued.status_code == 200
    original_time = time.time
    original_monotonic = time.monotonic
    monkeypatch.setattr(relay_module.time, 'time', lambda: original_time() + 2.0)
    monkeypatch.setattr(relay_module.time, 'monotonic', lambda: original_monotonic() + 2.0)

    response = client.post('/api/v1/relay/responses', json=_api_v1_response_payload('req-expired-late-response'))

    assert response.status_code == 410
    error = response.get_json()['error']
    assert error['code'] == 'expired'
    assert DUMMY_CLIENT_PUB_KEY not in client_responses
    retrieve = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': 'req-expired-late-response',
    })
    assert retrieve.status_code == 410
    assert retrieve.get_json()['error']['code'] == 'expired'


def test_api_v1_queued_response_before_ttl_survives_delayed_retrieve(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 1.0)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-response-before-ttl'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id)).status_code == 200
    response = client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id))
    assert response.status_code == 200
    original_time = time.time
    monkeypatch.setattr(relay_module.time, 'time', lambda: original_time() + 2.0)

    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })

    assert retrieved.status_code == 200
    assert retrieved.get_json()['chat_history'] == 'ciphertext-response'
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids
    assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids


def test_api_v1_queued_response_before_ttl_survives_diagnostics_cleanup(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 1.0)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-response-diagnostics-cleanup'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id)).status_code == 200
    assert client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id)).status_code == 200
    original_time = time.time
    monkeypatch.setattr(relay_module.time, 'time', lambda: original_time() + 2.0)

    diagnostics = client.get('/relay/diagnostics')
    assert diagnostics.status_code == 200
    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })

    assert retrieved.status_code == 200
    assert retrieved.get_json()['chat_history'] == 'ciphertext-response'
    assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids


def test_api_v1_late_duplicate_response_preserves_accepted_response(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 1.0)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-late-duplicate-response'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id)).status_code == 200
    assert client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id, ciphertext='accepted')).status_code == 200
    original_time = time.time
    monkeypatch.setattr(relay_module.time, 'time', lambda: original_time() + 2.0)

    duplicate = client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id, ciphertext='late-duplicate'))
    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })

    assert duplicate.status_code == 200
    assert duplicate.get_json()['message'] == 'Response already queued for client'
    assert retrieved.status_code == 200
    assert retrieved.get_json()['chat_history'] == 'accepted'
    assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids


def test_api_v1_cancel_requires_matching_cancel_token(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload('req-cancel-auth', cancel_token='proof')).status_code == 200

    denied = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': 'req-cancel-auth',
        'status': 'cancelled',
        'reason': 'requester_cancelled',
        'cancel_token': 'wrong-proof',
    })

    assert denied.status_code == 403
    assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids


def test_api_v1_cancel_sanitizes_status_and_reason(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload('req-sanitize-cancel', cancel_token='proof')).status_code == 200

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': 'req-sanitize-cancel',
        'status': 'evil_status',
        'reason': 'leaky reason',
        'cancel_token': 'proof',
    })
    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': 'req-sanitize-cancel',
    })

    assert cancelled.status_code == 200
    assert cancelled.get_json()['status'] == 'cancelled'
    assert retrieved.status_code == 410
    error = retrieved.get_json()['error']
    assert error['code'] == 'cancelled'
    assert error['reason'] == 'cancelled'


def test_api_v1_response_accepted_first_survives_later_cancellation(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-response-wins-before-cancel'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id, cancel_token='proof')).status_code == 200
    assert client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id)).status_code == 200

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
        'status': 'cancelled',
        'reason': 'requester_cancelled',
        'cancel_token': 'proof',
    })
    retrieved = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })

    assert cancelled.status_code == 200
    assert retrieved.status_code == 200
    assert retrieved.get_json()['request_id'] == request_id
    assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids


def test_api_v1_response_after_in_flight_cancel_is_rejected_and_queue_depth_zero(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-dispatched-cancelled'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id, cancel_token='proof')).status_code == 200
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == request_id

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
        'status': 'cancelled',
        'reason': 'requester_cancelled',
        'cancel_token': 'proof',
    })
    response = client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id))

    assert cancelled.status_code == 200
    assert response.status_code == 410
    assert response.get_json()['error']['code'] == 'cancelled'
    diagnostics = client.get('/relay/diagnostics').get_json()
    assert diagnostics['registered_compute_nodes'][0]['queue_depth'] == 0


def test_api_v1_cancel_only_clears_matching_client_in_flight_entry(client):
    other_server_key = base64.b64encode(b"server_public_key_other").decode('utf-8')
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
        'api_v1_in_flight_requests': {
            'shared-req': {'expires_at': time.monotonic() + 60, 'client_public_key': DUMMY_CLIENT_PUB_KEY, 'cancel_token': 'matching-proof'}
        },
    }
    known_servers[other_server_key] = {
        'public_key': other_server_key,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
        'api_v1_in_flight_requests': {
            'shared-req': {'expires_at': time.monotonic() + 60, 'client_public_key': 'other-client', 'cancel_token': 'other-proof'}
        },
    }
    relay_module._mark_request_pending(DUMMY_CLIENT_PUB_KEY, 'shared-req', cancel_token='matching-proof')

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': 'shared-req',
        'status': 'cancelled',
        'reason': 'requester_cancelled',
        'cancel_token': 'matching-proof',
    })

    assert cancelled.status_code == 200
    assert 'api_v1_in_flight_requests' not in known_servers[DUMMY_SERVER_PUB_KEY]
    assert 'shared-req' in known_servers[other_server_key]['api_v1_in_flight_requests']
    assert 'other-client' not in client_terminal_request_ids


def test_api_v1_pending_ttl_cleanup_runs_without_retrieve(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 1.0)
    request_id = 'req-cleanup-without-retrieve'
    client_pending_request_ids.setdefault(DUMMY_CLIENT_PUB_KEY, {})[request_id] = time.time()
    original_time = time.time
    monkeypatch.setattr(relay_module.time, 'time', lambda: original_time() + 2.0)

    diagnostics = client.get('/relay/diagnostics')

    assert diagnostics.status_code == 200
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids
    assert client_terminal_request_ids[DUMMY_CLIENT_PUB_KEY][request_id]['status'] == 'expired'


def test_api_v1_terminal_records_are_pruned_without_retrieve(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'TERMINAL_REQUEST_TTL_SECONDS', 1.0)
    relay_module._mark_request_terminal(DUMMY_CLIENT_PUB_KEY, 'req-terminal-pruned', status='cancelled')
    assert DUMMY_CLIENT_PUB_KEY in client_terminal_request_ids
    original_time = time.time
    monkeypatch.setattr(relay_module.time, 'time', lambda: original_time() + 2.0)

    diagnostics = client.get('/relay/diagnostics')

    assert diagnostics.status_code == 200
    assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids


def test_api_v1_control_owner_sees_in_flight_cancel_and_ack_cleans_up(client):
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    wrong_server = _server_key('wrong-control-server')
    owner_register = client.post('/api/v1/relay/servers/register', json=server_payload)
    wrong_register = client.post('/api/v1/relay/servers/register', json={'server_public_key': wrong_server})
    assert owner_register.status_code == 200
    assert wrong_register.status_code == 200
    server_payload = server_payload | {'control_credential': owner_register.get_json()['control_credential']}
    wrong_server_payload = {'server_public_key': wrong_server, 'control_credential': wrong_register.get_json()['control_credential']}
    request_id = 'req-control-cancel'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id, cancel_token='proof')).status_code == 200
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == request_id
    assert 'request_ttl_seconds' in poll.get_json()

    wrong_before = client.post('/api/v1/relay/servers/control', json=wrong_server_payload | {'request_id': request_id})
    assert wrong_before.status_code == 200
    assert wrong_before.get_json()['status'] == 'completed/unavailable'

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
        'status': 'cancelled',
        'reason': 'client_timeout',
        'cancel_token': 'proof',
    })
    assert cancelled.status_code == 200

    wrong_after = client.post('/api/v1/relay/servers/control', json=wrong_server_payload | {'request_id': request_id})
    owner = client.post('/api/v1/relay/servers/control', json=server_payload | {'request_id': request_id})
    ack = client.post('/api/v1/relay/servers/control', json=server_payload | {'request_id': request_id, 'acknowledge': True})
    after_ack = client.post('/api/v1/relay/servers/control', json=server_payload | {'request_id': request_id})

    assert wrong_after.get_json()['status'] == 'completed/unavailable'
    assert owner.status_code == 200
    assert owner.get_json()['status'] == 'cancelled'
    assert owner.get_json()['request_ttl_seconds'] >= 0
    assert ack.get_json()['status'] == 'cancelled'
    assert after_ack.get_json()['status'] == 'completed/unavailable'
    retrieved = client.post('/api/v1/relay/responses/retrieve', json={'client_public_key': DUMMY_CLIENT_PUB_KEY, 'request_id': request_id})
    assert retrieved.status_code == 410
    assert retrieved.get_json()['error']['reason'] == 'client_timeout'


def test_api_v1_reregister_backfills_public_key_for_owner_tombstones(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    register = client.post(
        '/api/v1/relay/servers/register',
        json={'server_public_key': DUMMY_SERVER_PUB_KEY},
    )
    assert register.status_code == 200
    credential = register.get_json()['control_credential']
    assert known_servers[DUMMY_SERVER_PUB_KEY]['public_key'] == DUMMY_SERVER_PUB_KEY

    request_id = 'req-reregister-tombstone-owner'
    assert client.post(
        '/api/v1/relay/requests',
        json=_api_v1_request_payload(request_id, cancel_token='proof'),
    ).status_code == 200
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == request_id

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
        'status': 'cancelled',
        'reason': 'requester_cancelled',
        'cancel_token': 'proof',
    })
    control = client.post('/api/v1/relay/servers/control', json={
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'request_id': request_id,
        'control_credential': credential,
    })

    assert cancelled.status_code == 200
    assert control.status_code == 200
    assert control.get_json()['status'] == 'cancelled'


def test_api_v1_control_requires_owner_proof_without_registration_tokens(client):
    register = register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert register.status_code == 200

    response = client.post(
        '/api/v1/relay/servers/control',
        json={'server_public_key': DUMMY_SERVER_PUB_KEY, 'request_id': 'req'},
    )

    assert response.status_code == 403


def test_api_v1_control_requires_registration_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(relay_module, 'SERVER_REGISTRATION_TOKENS', ['secret-token'])
    register = client.post(
        '/api/v1/relay/servers/register',
        json={'server_public_key': DUMMY_SERVER_PUB_KEY},
        headers={'X-Relay-Server-Token': 'secret-token'},
    )
    assert register.status_code == 200
    credential = register.get_json()['control_credential']

    unsigned = client.post('/api/v1/relay/servers/control', json={'server_public_key': DUMMY_SERVER_PUB_KEY, 'request_id': 'req'})
    missing_owner_proof = client.post(
        '/api/v1/relay/servers/control',
        json={'server_public_key': DUMMY_SERVER_PUB_KEY, 'request_id': 'req'},
        headers={'X-Relay-Server-Token': 'secret-token'},
    )
    signed = client.post(
        '/api/v1/relay/servers/control',
        json={'server_public_key': DUMMY_SERVER_PUB_KEY, 'request_id': 'req', 'control_credential': credential},
        headers={'X-Relay-Server-Token': 'secret-token'},
    )

    assert unsigned.status_code == 401
    assert missing_owner_proof.status_code == 403
    assert signed.status_code == 200


def test_api_v1_control_renews_lease_without_extending_absolute_deadline(client, monkeypatch):
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_SECONDS_ENV, '5')
    monkeypatch.setenv(relay_module.API_V1_IN_FLIGHT_TTL_SECONDS_ENV, '30')
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200
    control_payload = server_payload | {'control_credential': register.get_json()['control_credential']}
    request_id = 'req-lease-no-deadline-extension'
    queued = client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id))
    assert queued.status_code == 200
    queued_ttl = queued.get_json()['request_ttl_seconds']
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200
    initial = known_servers[DUMMY_SERVER_PUB_KEY]['api_v1_in_flight_requests'][request_id]
    initial_deadline = initial['request_deadline_monotonic']

    control = client.post('/api/v1/relay/servers/control', json=control_payload | {'request_id': request_id})
    renewed = known_servers[DUMMY_SERVER_PUB_KEY]['api_v1_in_flight_requests'][request_id]

    assert control.status_code == 200
    assert control.get_json()['status'] == 'active'
    assert renewed['request_deadline_monotonic'] == initial_deadline
    assert renewed['expires_at'] <= initial_deadline
    assert control.get_json()['request_ttl_seconds'] <= queued_ttl


def test_expire_stale_pending_requests_uses_deadline_index_for_legacy_float_entries(client):
    request_id = 'req-deadline-index-only'
    relay_module._mark_request_pending(
        DUMMY_CLIENT_PUB_KEY,
        request_id,
        deadline_monotonic=time.monotonic() - 1.0,
    )

    relay_module._expire_stale_pending_requests()

    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids
    terminal = relay_module._get_terminal_request(DUMMY_CLIENT_PUB_KEY, request_id)
    assert terminal['status'] == 'expired'


def test_pending_entry_deadline_is_authoritative_over_legacy_ttl(monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 1.0)
    future_deadline = time.monotonic() + 30.0
    assert relay_module._pending_request_entry_is_expired(
        {'queued_at': time.time() - 3600, 'request_deadline_monotonic': future_deadline},
        now=time.time(),
    ) is False


def test_pending_entry_uses_deadline_even_when_legacy_ttl_disabled(monkeypatch):
    monkeypatch.setattr(relay_module, 'PENDING_REQUEST_TTL_SECONDS', 0.0)
    assert relay_module._pending_request_entry_is_expired(
        {'queued_at': time.time(), 'request_deadline_monotonic': time.monotonic() - 0.1},
    ) is True


def test_api_v1_request_deadline_seconds_rejects_non_finite_and_hard_clamps(monkeypatch):
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_MIN_SECONDS_ENV, 'nan')
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_MAX_SECONDS_ENV, 'inf')
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_SECONDS_ENV, '999999')
    assert relay_module._api_v1_request_deadline_seconds() == relay_module.HARD_MAX_API_V1_REQUEST_DEADLINE_SECONDS


def test_api_v1_poll_drops_expired_queue_head_and_dispatches_next(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload('req-expired-head')).status_code == 200
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload('req-valid-next')).status_code == 200
    queued = client_inference_requests[DUMMY_SERVER_PUB_KEY]
    queued[0]['_request_deadline_monotonic'] = time.monotonic() - 1.0
    queued[1]['_request_deadline_monotonic'] = time.monotonic() + 30.0

    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    assert poll.status_code == 200
    assert poll.get_json()['request_id'] == 'req-valid-next'
    expired_terminal = relay_module._get_terminal_request(DUMMY_CLIENT_PUB_KEY, 'req-expired-head')
    assert expired_terminal is not None
    assert expired_terminal['status'] == 'expired'


def test_api_v1_absolute_deadline_expiry_rejects_late_response(client, monkeypatch):
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_SECONDS_ENV, '1')
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200
    control_payload = server_payload | {'control_credential': register.get_json()['control_credential']}
    request_id = 'req-absolute-deadline'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id)).status_code == 200
    assert client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY}).status_code == 200
    original_monotonic = time.monotonic
    monkeypatch.setattr(relay_module.time, 'monotonic', lambda: original_monotonic() + 2.0)

    control = client.post('/api/v1/relay/servers/control', json=control_payload | {'request_id': request_id})
    late_response = client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id))

    assert control.status_code == 200
    assert control.get_json()['status'] == 'expired'
    assert late_response.status_code == 410
    assert late_response.get_json()['error']['code'] == 'expired'



def test_api_v1_control_expiry_returns_expired_when_tombstone_ack_races(client, monkeypatch):
    monkeypatch.setenv(relay_module.API_V1_REQUEST_DEADLINE_SECONDS_ENV, '1')
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    assert register.status_code == 200
    control_payload = server_payload | {'control_credential': register.get_json()['control_credential']}
    request_id = 'req-expiry-ack-race'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id)).status_code == 200
    assert client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY}).status_code == 200
    in_flight_entry = known_servers[DUMMY_SERVER_PUB_KEY]['api_v1_in_flight_requests'][request_id]
    in_flight_entry['request_deadline_monotonic'] = time.monotonic() - 1.0
    in_flight_entry['expires_at'] = time.monotonic() + 60.0
    original_mark_terminal = relay_module._mark_request_terminal

    def mark_terminal_and_ack_tombstone(*args, **kwargs):
        result = original_mark_terminal(*args, **kwargs)
        relay_module.api_v1_control_tombstones.pop(
            relay_module._control_tombstone_key(DUMMY_SERVER_PUB_KEY, request_id),
            None,
        )
        return result

    monkeypatch.setattr(relay_module, '_mark_request_terminal', mark_terminal_and_ack_tombstone)

    control = client.post('/api/v1/relay/servers/control', json=control_payload | {'request_id': request_id})

    assert control.status_code == 200
    assert control.get_json()['status'] == 'expired'


def test_prune_api_v1_stale_in_flight_entries_expires_with_owner_tombstone(client):
    register = register = client.post('/api/v1/relay/servers/register', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert register.status_code == 200
    credential = register.get_json()['control_credential']
    request_id = 'req-prune-expired-in-flight'
    relay_module._mark_request_pending(
        DUMMY_CLIENT_PUB_KEY,
        request_id,
        cancel_token='proof',
        deadline_monotonic=time.monotonic() - 1.0,
    )
    known_servers[DUMMY_SERVER_PUB_KEY]['api_v1_in_flight_requests'] = {
        request_id: {
            'expires_at': time.monotonic() - 1.0,
            'started_at_monotonic': time.monotonic() - 3.0,
            'client_public_key': DUMMY_CLIENT_PUB_KEY,
            'cancel_token': 'proof',
            'request_deadline_monotonic': time.monotonic() - 1.0,
        }
    }

    removed = relay_module._prune_api_v1_stale_in_flight_entries(
        known_servers[DUMMY_SERVER_PUB_KEY],
        now_monotonic=time.monotonic(),
    )
    control = client.post('/api/v1/relay/servers/control', json={
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'request_id': request_id,
        'control_credential': credential,
    })

    assert removed == 1
    assert 'api_v1_in_flight_requests' not in known_servers[DUMMY_SERVER_PUB_KEY]
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids
    assert DUMMY_CLIENT_PUB_KEY not in relay_module.client_pending_request_deadlines
    assert relay_module._get_terminal_request(DUMMY_CLIENT_PUB_KEY, request_id)['status'] == 'expired'
    assert control.status_code == 200
    assert control.get_json()['status'] == 'expired'


def test_api_v1_cancel_and_response_race_has_single_winner(client, monkeypatch):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-cancel-response-race'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id, cancel_token='proof')).status_code == 200
    assert client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY}).status_code == 200
    barrier = threading.Barrier(2)
    results = {}

    def submit_response():
        with app.test_client() as race_client:
            barrier.wait(timeout=5)
            results['response'] = race_client.post('/api/v1/relay/responses', json=_api_v1_response_payload(request_id)).status_code

    def cancel_request():
        with app.test_client() as race_client:
            barrier.wait(timeout=5)
            results['cancel'] = race_client.post('/api/v1/relay/requests/cancel', json={
                'client_public_key': DUMMY_CLIENT_PUB_KEY,
                'request_id': request_id,
                'status': 'cancelled',
                'reason': 'requester_cancelled',
                'cancel_token': 'proof',
            }).status_code

    threads = [threading.Thread(target=submit_response), threading.Thread(target=cancel_request)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert results['cancel'] == 200
    assert results['response'] in {200, 410}

    retrieve = client.post('/api/v1/relay/responses/retrieve', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
    })
    if results['response'] == 200:
        assert retrieve.status_code == 200
        assert DUMMY_CLIENT_PUB_KEY not in client_terminal_request_ids
    else:
        assert retrieve.status_code == 410
        assert retrieve.get_json()['error']['code'] == 'cancelled'
    assert DUMMY_CLIENT_PUB_KEY not in client_pending_request_ids
    assert 'api_v1_in_flight_requests' not in known_servers[DUMMY_SERVER_PUB_KEY]


def test_api_v1_control_next_poll_seconds_has_positive_floor(client, monkeypatch):
    monkeypatch.setenv(relay_module.API_V1_POLL_WAIT_SECONDS_ENV, '0')
    server_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    register = client.post('/api/v1/relay/servers/register', json=server_payload)
    control_payload = server_payload | {'control_credential': register.get_json()['control_credential']}
    assert register.status_code == 200
    response = client.post('/api/v1/relay/servers/control', json=control_payload | {'request_id': 'missing'})
    assert response.status_code == 200
    assert response.get_json()['next_poll_seconds'] >= 1.0


def test_api_v1_queued_cancellation_and_old_client_compatibility(client):
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': datetime.now(),
        'last_ping_duration': 60,
        relay_module.API_V1_SERVER_MARKER: True,
    }
    request_id = 'req-queued-cancel-deadline-compat'
    queued = client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id, cancel_token='proof'))
    assert queued.status_code == 200
    assert queued.get_json()['request_ttl_seconds'] > 0

    cancelled = client.post('/api/v1/relay/requests/cancel', json={
        'client_public_key': DUMMY_CLIENT_PUB_KEY,
        'request_id': request_id,
        'status': 'cancelled',
        'reason': 'client_timeout',
        'cancel_token': 'proof',
    })
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})

    assert cancelled.status_code == 200
    assert poll.status_code == 200
    assert poll.get_json()['message'] == 'No requests available'


def test_api_v1_unregister_requires_exact_owner_control_credential(client):
    owner_payload = _api_v1_registered_control_payload(client, DUMMY_SERVER_PUB_KEY)
    wrong_server = _server_key('unregister-wrong-owner')
    wrong_payload = _api_v1_registered_control_payload(client, wrong_server)
    request_id = 'req-unregister-owner-proof'
    assert client.post('/api/v1/relay/requests', json=_api_v1_request_payload(request_id, cancel_token='proof')).status_code == 200
    poll = client.post('/api/v1/relay/servers/poll', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    assert poll.status_code == 200

    unsigned = client.post('/api/v1/relay/servers/unregister', json={'server_public_key': DUMMY_SERVER_PUB_KEY})
    wrong = client.post('/api/v1/relay/servers/unregister', json={
        'server_public_key': DUMMY_SERVER_PUB_KEY,
        'control_credential': wrong_payload['control_credential'],
    })

    assert unsigned.status_code == 403
    assert wrong.status_code == 403
    assert DUMMY_SERVER_PUB_KEY in known_servers
    assert request_id in known_servers[DUMMY_SERVER_PUB_KEY]['api_v1_in_flight_requests']
    assert client.post('/api/v1/relay/servers/control', json=owner_payload | {'request_id': request_id}).get_json()['status'] == 'active'

    removed = client.post('/api/v1/relay/servers/unregister', json=owner_payload)
    assert removed.status_code == 200
    assert removed.get_json()['removed'] is True
    assert DUMMY_SERVER_PUB_KEY not in known_servers
    control = client.post('/api/v1/relay/servers/control', json=owner_payload | {'request_id': request_id})
    assert control.status_code == 200
    assert control.get_json()['status'] == 'cancelled'
