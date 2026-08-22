"""Focused route wiring coverage for the memory RelayStateStore authority."""
from __future__ import annotations

import relay


def _journey(client):
    base = {"protocol": "tokenplace_api_v1_relay_e2ee", "version": 1, "ciphertext": "opaque", "cipherkey": "wrapped", "iv": "nonce"}
    registered = client.post("/api/v1/relay/servers/register", json={"server_public_key": "node-a"})
    assert registered.status_code == 200
    credential = registered.json["control_credential"]
    selected = client.get("/api/v1/relay/servers/next", query_string={"client_public_key": "client-a", "request_id": "request-a", "cancel_token": "cancel-a"})
    assert selected.status_code == 200
    request = {**base, "server_public_key": "node-a", "client_public_key": "client-a", "request_id": "request-a", "cancel_token": "cancel-a", "reservation_token": selected.json["reservation_token"], "request_deadline_epoch": selected.json["request_deadline_epoch"]}
    assert client.post("/api/v1/relay/requests", json=request).status_code == 200
    assert client.post("/api/v1/relay/requests", json=request).status_code == 200
    claimed = client.post("/api/v1/relay/servers/poll", json={"server_public_key": "node-a", "control_credential": credential, "consumer_identity": "worker-a"})
    assert claimed.status_code == 200
    response = {**base, "server_public_key": "node-a", "client_public_key": "client-a", "request_id": "request-a", "control_credential": credential, "consumer_identity": "worker-a", "claim_generation": claimed.json["claim_generation"]}
    assert client.post("/api/v1/relay/responses", json=response).status_code == 200
    retrieved = client.post("/api/v1/relay/responses/retrieve", json={"client_public_key": "client-a", "request_id": "request-a", "retrieval_credential": selected.json["reservation_token"]})
    assert retrieved.status_code == 200
    assert retrieved.json["ciphertext"] == "opaque"
    acknowledged = client.post("/api/v1/relay/responses/retrieve", json={"client_public_key": "client-a", "request_id": "request-a", "retrieval_credential": selected.json["reservation_token"], "acknowledgement_token": retrieved.json["acknowledgement_token"]})
    assert acknowledged.json == {"status": "acknowledged"}


def test_api_v1_routes_use_injected_memory_store(monkeypatch):
    monkeypatch.setattr(relay, "RELAY_STATE_STORE", None)
    _journey(relay.app.test_client())
    assert not relay.known_servers
    assert not relay.client_inference_requests
    assert not relay.client_responses


def test_store_failure_is_bounded_and_does_not_fall_back(monkeypatch):
    class FailedStore:
        def get(self, _node):
            raise RuntimeError("credential=secret ciphertext=plaintext-sentinel")

    monkeypatch.setattr(relay, "RELAY_STATE_STORE", FailedStore())
    response = relay.app.test_client().post("/api/v1/relay/servers/register", json={"server_public_key": "node-a"})
    assert response.status_code == 503
    rendered = response.get_data(as_text=True)
    assert "secret" not in rendered and "sentinel" not in rendered
    assert not relay.known_servers
