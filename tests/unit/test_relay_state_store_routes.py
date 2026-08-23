"""Focused route wiring coverage for the in-memory API-v1 relay state store."""

from unittest.mock import Mock

import relay
from relay_state_store import RelayStateStoreError


def _claim_for_control(client, *, request_id="control-request"):
    node = "control-node"
    client_key = "control-client"
    registration = client.post("/api/v1/relay/servers/register", json={
        "server_public_key": node,
        "capabilities": {"supported_model_ids": ["qwen3-8b-instruct"], "active_context_tier": "8k-fast",
                         "maximum_total_context_tokens": 8192, "default_output_token_reservation": 1024,
                         "maximum_output_tokens": 1024, "max_concurrency": 1},
    }).get_json()
    selection = client.get("/api/v1/relay/servers/next", query_string={
        "client_public_key": client_key, "request_id": request_id, "cancel_token": "control-cancel",
    }).get_json()
    assert client.post("/api/v1/relay/requests", json={
        "server_public_key": node, "client_public_key": client_key, "request_id": request_id,
        "cancel_token": "control-cancel", "reservation_token": selection["reservation_token"],
        "request_deadline_epoch": selection["request_deadline_epoch"],
        "protocol": "tokenplace_api_v1_relay_e2ee", "version": 1,
        "ciphertext": "sealed-request", "cipherkey": "sealed-key", "iv": "sealed-iv",
    }).status_code == 200
    queued = relay._api_v1_store().queued_requests(node)[0]
    poll = client.post("/api/v1/relay/servers/poll", json={
        "server_public_key": node, "control_credential": registration["control_credential"],
    }).get_json()
    claim = relay._api_v1_store().active_claims(node)[0]
    return node, registration["control_credential"], poll, (queued, claim)


def test_api_v1_encrypted_journey_uses_authoritative_store():
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, client_key, request_id, cancellation = "route-node", "route-client", "route-request", "route-cancel"
    registration = client.post("/api/v1/relay/servers/register", json={
        "server_public_key": node,
        "capabilities": {"supported_model_ids": ["qwen3-8b-instruct"], "active_context_tier": "8k-fast",
                         "maximum_total_context_tokens": 8192, "default_output_token_reservation": 1024,
                         "maximum_output_tokens": 1024, "max_concurrency": 1},
    })
    assert registration.status_code == 200
    selection = client.get("/api/v1/relay/servers/next", query_string={
        "client_public_key": client_key, "request_id": request_id, "cancel_token": cancellation,
    }).get_json()
    request_payload = {
        "server_public_key": node, "client_public_key": client_key, "request_id": request_id,
        "cancel_token": cancellation, "reservation_token": selection["reservation_token"],
        "request_deadline_epoch": selection["request_deadline_epoch"], "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1, "ciphertext": "sealed-request", "cipherkey": "sealed-key", "iv": "sealed-iv",
    }
    assert client.post("/api/v1/relay/requests", json=request_payload).status_code == 200
    poll = client.post("/api/v1/relay/servers/poll", json={
        "server_public_key": node, "control_credential": registration.get_json()["control_credential"],
    })
    assert poll.status_code == 200
    assert poll.get_json()["ciphertext"] == "sealed-request"
    assert client.post("/api/v1/relay/responses", json={
        "server_public_key": node,
        "control_credential": registration.get_json()["control_credential"],
        "claim_generation": poll.get_json()["claim_generation"],
        "client_public_key": client_key, "request_id": request_id, "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1, "ciphertext": "sealed-response", "cipherkey": "sealed-key", "iv": "sealed-iv",
    }).status_code == 200
    retrieved = client.post("/api/v1/relay/responses/retrieve", json={
        "client_public_key": client_key, "request_id": request_id,
        "retrieval_credential": selection["reservation_token"],
    })
    assert retrieved.status_code == 200
    assert retrieved.get_json()["ciphertext"] == "sealed-response"
    assert relay.known_servers == {}
    assert relay.client_inference_requests == {}
    assert relay.client_responses == {}


def test_compat_control_resolves_and_renews_authoritative_claim(monkeypatch):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, claimed = _claim_for_control(client)
    store = relay._api_v1_store()
    monkeypatch.setattr(store, "claimed_request", Mock(return_value=claimed))
    renew = Mock(wraps=store.renew_claim_or_read_control)
    monkeypatch.setattr(store, "renew_claim_or_read_control", renew)

    response = client.post("/api/v1/relay/servers/control", json={
        "server_public_key": node, "request_id": poll["request_id"],
        "control_credential": credential,
    })

    assert response.status_code == 200
    assert response.get_json()["status"] == "active"
    renew.assert_called_once()
    assert renew.call_args.args[3:6] == (poll["client_public_key"], poll["request_id"], poll["claim_generation"])


def test_compat_control_forwards_acknowledgement_to_store(monkeypatch):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, claimed = _claim_for_control(client, request_id="control-ack")
    store = relay._api_v1_store()
    monkeypatch.setattr(store, "claimed_request", Mock(return_value=claimed))
    renew = Mock(wraps=store.renew_claim_or_read_control)
    monkeypatch.setattr(store, "renew_claim_or_read_control", renew)

    response = client.post("/api/v1/relay/servers/control", json={
        "server_public_key": node, "request_id": poll["request_id"],
        "control_credential": credential, "ack": True,
    })

    assert response.status_code == 200
    assert response.get_json()["status"] == "active"
    assert renew.call_args.kwargs == {"acknowledge": True}


def test_compat_control_reports_unavailable_without_legacy_state(monkeypatch):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, _ = _claim_for_control(client, request_id="control-missing")
    store = relay._api_v1_store()
    monkeypatch.setattr(store, "claimed_request", Mock(return_value=None))

    class ForbiddenLegacyState(dict):
        def __getattribute__(self, name):
            if name not in {"__class__", "__getattribute__"}:
                raise AssertionError("control route accessed legacy correctness state")
            return super().__getattribute__(name)

    for name in ("known_servers", "client_inference_requests", "client_responses",
                 "api_v1_control_tombstones"):
        monkeypatch.setattr(relay, name, ForbiddenLegacyState())
    with relay.app.test_request_context("/api/v1/relay/servers/control", method="POST", json={
            "server_public_key": node, "request_id": poll["request_id"],
            "control_credential": credential, "acknowledge": True,
    }):
        response, status_code = relay.api_v1_relay_servers_control()

    assert status_code == 200
    assert response.get_json()["status"] == "completed/unavailable"


def test_compat_control_bounds_claim_resolution_failure(monkeypatch):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, _ = _claim_for_control(client, request_id="control-failure")
    monkeypatch.setattr(relay._api_v1_store(), "claimed_request",
                        Mock(side_effect=RelayStateStoreError("sensitive backend detail")))

    response = client.post("/api/v1/relay/servers/control", json={
        "server_public_key": node, "request_id": poll["request_id"],
        "control_credential": credential,
    })

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {"message": "Relay state is temporarily unavailable", "code": "state_backend_unavailable"}
    }
    assert b"sensitive backend detail" not in response.data
