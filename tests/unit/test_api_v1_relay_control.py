import time

import pytest

import relay


@pytest.fixture
def relay_client(monkeypatch):
    relay.app.config["TESTING"] = True
    monkeypatch.setattr(relay, "SERVER_REGISTRATION_TOKENS", ["token"])
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.client_pending_request_ids.clear()
    relay.client_terminal_request_ids.clear()
    relay.client_terminal_outcomes.clear()
    relay.api_v1_control_tombstones.clear()
    with relay.app.test_client() as client:
        yield client
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.client_pending_request_ids.clear()
    relay.client_terminal_request_ids.clear()
    relay.client_terminal_outcomes.clear()
    relay.api_v1_control_tombstones.clear()


def _headers():
    return {"X-Relay-Server-Token": "token"}


def _register(client, key="server-a"):
    res = client.post(
        "/api/v1/relay/servers/register",
        json={
            "server_public_key": key,
            "capabilities": {
                "api_version": "v1",
                "supported_model_ids": ["qwen3-8b-instruct"],
                "active_context_tier": "8k-fast",
                "maximum_total_context_tokens": 8192,
                "default_output_token_reservation": 1024,
                "maximum_output_tokens": 2048,
                "max_concurrency": 1,
            },
        },
        headers=_headers(),
    )
    assert res.status_code == 200


def _request(client, server="server-a", request_id="req-1", cancel_token="cancel"):
    res = client.post(
        "/api/v1/relay/requests",
        json={
            "server_public_key": server,
            "client_public_key": "client-a",
            "request_id": request_id,
            "cancel_token": cancel_token,
            "ciphertext": "sealed",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
    )
    assert res.status_code == 200
    body = res.get_json()
    assert "deadline_unix_ms" in body
    return body


def _poll(client, server="server-a"):
    res = client.post(
        "/api/v1/relay/servers/poll",
        json={"server_public_key": server},
        headers=_headers(),
    )
    assert res.status_code == 200
    return res.get_json()


def test_queued_cancellation_preserves_client_timeout_and_rejects_late_response(
    relay_client,
):
    _register(relay_client)
    _request(relay_client)
    cancel = relay_client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": "client-a",
            "request_id": "req-1",
            "cancel_token": "cancel",
            "status": "cancelled",
            "reason": "client_timeout",
        },
    )
    assert cancel.status_code == 200
    retrieve = relay_client.post(
        "/api/v1/relay/responses/retrieve",
        json={"client_public_key": "client-a", "request_id": "req-1"},
    )
    assert retrieve.status_code == 410
    assert retrieve.get_json()["error"]["reason"] == "client_timeout"
    late = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-a",
            "request_id": "req-1",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
        headers=_headers(),
    )
    assert late.status_code == 410


def test_in_flight_cancellation_visible_only_to_owner_and_ack_cleans_tombstone(
    relay_client,
):
    _register(relay_client, "server-a")
    _register(relay_client, "server-b")
    _request(relay_client)
    assert _poll(relay_client)["request_id"] == "req-1"
    wrong = relay_client.post(
        "/api/v1/relay/requests/control",
        json={"server_public_key": "server-b", "request_id": "req-1"},
        headers=_headers(),
    )
    assert wrong.status_code == 200
    assert wrong.get_json()["status"] == "completed/unavailable"
    cancel = relay_client.post(
        "/api/v1/relay/requests/cancel",
        json={
            "client_public_key": "client-a",
            "request_id": "req-1",
            "cancel_token": "cancel",
            "status": "cancelled",
            "reason": "client_timeout",
        },
    )
    assert cancel.status_code == 200
    owner = relay_client.post(
        "/api/v1/relay/requests/control",
        json={"server_public_key": "server-a", "request_id": "req-1"},
        headers=_headers(),
    )
    assert owner.status_code == 200
    assert owner.get_json()["status"] == "cancelled"
    ack = relay_client.post(
        "/api/v1/relay/requests/control",
        json={
            "server_public_key": "server-a",
            "request_id": "req-1",
            "acknowledge": True,
        },
        headers=_headers(),
    )
    assert ack.status_code == 200
    assert ack.get_json()["status"] == "cancelled"
    gone = relay_client.post(
        "/api/v1/relay/requests/control",
        json={"server_public_key": "server-a", "request_id": "req-1"},
        headers=_headers(),
    )
    assert gone.get_json()["status"] == "completed/unavailable"


def test_unsigned_control_fails_closed(relay_client):
    _register(relay_client)
    res = relay_client.post(
        "/api/v1/relay/requests/control",
        json={"server_public_key": "server-a", "request_id": "req-1"},
    )
    assert res.status_code == 401


def test_absolute_deadline_expiry_and_lease_renewal_without_deadline_extension(
    relay_client, monkeypatch
):
    monkeypatch.setenv(relay.API_V1_REQUEST_DEADLINE_SECONDS_ENV, "1")
    _register(relay_client)
    request_meta = _request(relay_client)
    _poll(relay_client)
    entry = relay.known_servers["server-a"]["api_v1_in_flight_requests"]["req-1"]
    original_deadline = entry["deadline_unix_ms"]
    entry["expires_at"] = time.monotonic() + 0.1
    active = relay_client.post(
        "/api/v1/relay/requests/control",
        json={"server_public_key": "server-a", "request_id": "req-1"},
        headers=_headers(),
    )
    assert active.status_code == 200
    assert active.get_json()["status"] == "active"
    assert (
        relay.known_servers["server-a"]["api_v1_in_flight_requests"]["req-1"][
            "deadline_unix_ms"
        ]
        == original_deadline
        == request_meta["deadline_unix_ms"]
    )
    relay.known_servers["server-a"]["api_v1_in_flight_requests"]["req-1"][
        "deadline_unix_ms"
    ] = int((time.time() - 1) * 1000)
    expired = relay_client.post(
        "/api/v1/relay/requests/control",
        json={"server_public_key": "server-a", "request_id": "req-1"},
        headers=_headers(),
    )
    assert expired.status_code == 200
    assert expired.get_json()["status"] == "expired"
    late = relay_client.post(
        "/api/v1/relay/responses",
        json={
            "client_public_key": "client-a",
            "request_id": "req-1",
            "ciphertext": "sealed-response",
            "cipherkey": "sealed-key",
            "iv": "sealed-iv",
            "protocol": "e2ee_v1",
        },
        headers=_headers(),
    )
    assert late.status_code == 410


def test_old_clients_can_ignore_deadline_metadata_queue_depth_and_live_nodes_unchanged(
    relay_client,
):
    _register(relay_client)
    _request(relay_client)
    diagnostics = relay_client.get("/relay/diagnostics").get_json()
    assert diagnostics["total_api_v1_registered_compute_nodes"] == 1
    assert len(relay.client_inference_requests["server-a"]) == 1
    dispatched = _poll(relay_client)
    assert dispatched["ciphertext"] == "sealed"
    assert "deadline_unix_ms" in dispatched
    assert relay.client_inference_requests.get("server-a") is None
