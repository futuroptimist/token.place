"""Focused route wiring coverage for the in-memory API-v1 relay state store."""

import relay


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
