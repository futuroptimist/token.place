"""Focused route wiring coverage for the in-memory API-v1 relay state store."""

from unittest.mock import Mock

import pytest

import relay
from relay_state_store import RelayStateStoreError


def _queue_for_owner(client, *, request_id="control-request"):
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
    return node, registration["control_credential"], queued, selection["reservation_token"]


def _claim_for_control(client, *, request_id="control-request"):
    node, credential, queued, reservation_token = _queue_for_owner(
        client, request_id=request_id
    )
    poll = client.post("/api/v1/relay/servers/poll", json={
        "server_public_key": node, "control_credential": credential,
    }).get_json()
    claim = relay._api_v1_store().active_claims(node)[0]
    return node, credential, poll, (queued, claim, reservation_token)


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


@pytest.mark.parametrize("credential", [None, "", 7, "incorrect-proof"])
def test_existing_registration_requires_caller_control_credential(credential):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node = "renew-owner-node"
    original_capabilities = {
        "supported_model_ids": ["qwen3-8b-instruct"], "active_context_tier": "8k-fast",
        "maximum_total_context_tokens": 8192, "default_output_token_reservation": 1024,
        "maximum_output_tokens": 1024, "max_concurrency": 1,
    }
    registration = client.post("/api/v1/relay/servers/register", json={
        "server_public_key": node, "capabilities": original_capabilities,
    }).get_json()
    changed_capabilities = {**original_capabilities, "supported_model_ids": ["different-model"]}
    payload = {"server_public_key": node, "capabilities": changed_capabilities}
    if credential is not None:
        payload["control_credential"] = credential

    rejected = client.post("/api/v1/relay/servers/register", json=payload)

    assert rejected.status_code == 403
    assert relay._api_v1_store().get(node).capabilities.supported_model_ids == ("qwen3-8b-instruct",)
    payload["control_credential"] = registration["control_credential"]
    assert client.post("/api/v1/relay/servers/register", json=payload).status_code == 200
    assert relay._api_v1_store().get(node).capabilities.supported_model_ids == ("different-model",)


@pytest.mark.parametrize("credential", [None, "", 7, "incorrect-proof"])
def test_poll_requires_caller_control_credential_without_claiming(credential):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, correct_credential, _, _ = _queue_for_owner(
        client, request_id="poll-owner-proof"
    )
    store = relay._api_v1_store()
    payload = {"server_public_key": node}
    if credential is not None:
        payload["control_credential"] = credential

    rejected = client.post("/api/v1/relay/servers/poll", json=payload)

    assert rejected.status_code == 403
    assert store.active_claims(node) == ()
    assert len(store.queued_requests(node)) == 1
    payload["control_credential"] = correct_credential
    accepted = client.post("/api/v1/relay/servers/poll", json=payload)
    assert accepted.status_code == 200
    assert accepted.get_json()["request_id"] == "poll-owner-proof"


@pytest.mark.parametrize("credential", [None, "", 7, "incorrect-proof"])
def test_response_requires_caller_control_credential_without_completion(credential):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, correct_credential, poll, _ = _claim_for_control(client, request_id="response-owner-proof")
    payload = {
        "server_public_key": node, "claim_generation": poll["claim_generation"],
        "client_public_key": poll["client_public_key"], "request_id": poll["request_id"],
        "protocol": "tokenplace_api_v1_relay_e2ee", "version": 1,
        "ciphertext": "sealed-response", "cipherkey": "sealed-key", "iv": "sealed-iv",
    }
    if credential is not None:
        payload["control_credential"] = credential

    rejected = client.post("/api/v1/relay/responses", json=payload)

    assert rejected.status_code == 403
    assert len(relay._api_v1_store().active_claims(node)) == 1
    payload["control_credential"] = correct_credential
    assert client.post("/api/v1/relay/responses", json=payload).status_code == 200
    assert relay._api_v1_store().active_claims(node) == ()


@pytest.mark.parametrize("retrieval_credential", [None, "invalid-proof"])
def test_response_retrieve_rejects_missing_or_invalid_proof(retrieval_credential):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    _, _, poll, _ = _claim_for_control(client, request_id="retrieve-proof")
    payload = {"client_public_key": poll["client_public_key"], "request_id": poll["request_id"]}
    if retrieval_credential is not None:
        payload["retrieval_credential"] = retrieval_credential

    response = client.post("/api/v1/relay/responses/retrieve", json=payload)

    assert response.status_code == 403
    assert response.get_json() == {
        "error": {"message": "Missing or invalid retrieval proof", "code": 403}
    }
    assert b"invalid-proof" not in response.data
    assert b"sealed-request" not in response.data


@pytest.mark.parametrize("protocol", ["tokenplace_api_v1_relay_e2ee", "e2ee_v1"])
def test_progress_protocol_is_stored_canonically_and_retrieved_once(protocol):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, (_, _, retrieval_credential) = _claim_for_control(
        client, request_id=f"progress-{protocol}"
    )
    progress = {
        "server_public_key": node, "client_public_key": poll["client_public_key"],
        "request_id": poll["request_id"], "control_credential": credential,
        "protocol": protocol, "version": 1, "ciphertext": "progress-secret",
        "cipherkey": "progress-key", "iv": "progress-iv",
    }

    accepted = client.post("/api/v1/relay/progress", json=progress)
    first = client.post("/api/v1/relay/responses/retrieve", json={
        "client_public_key": poll["client_public_key"], "request_id": poll["request_id"],
        "retrieval_credential": retrieval_credential,
    })
    second = client.post("/api/v1/relay/responses/retrieve", json={
        "client_public_key": poll["client_public_key"], "request_id": poll["request_id"],
        "retrieval_credential": retrieval_credential,
    })

    assert accepted.status_code == 202
    assert accepted.get_json() == {"message": "Encrypted progress accepted"}
    assert first.status_code == 202
    assert first.get_json()["encrypted_progress"]["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert "encrypted_progress" not in second.get_json()
    assert credential not in str(first.get_json())


@pytest.mark.parametrize("updates", [
    {"protocol": "plaintext_v1"},
    {"version": 2},
    {"ciphertext": ""},
    {"cipherkey": None},
    {"iv": 7},
])
def test_invalid_progress_is_rejected_without_store_mutation(updates):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, (_, _, retrieval_credential) = _claim_for_control(
        client, request_id="invalid-progress"
    )
    progress = {
        "server_public_key": node, "client_public_key": poll["client_public_key"],
        "request_id": poll["request_id"], "control_credential": credential,
        "protocol": "tokenplace_api_v1_relay_e2ee", "version": 1,
        "ciphertext": "progress-secret", "cipherkey": "progress-key", "iv": "progress-iv",
    }
    progress.update(updates)

    rejected = client.post("/api/v1/relay/progress", json=progress)
    retrieved = client.post("/api/v1/relay/responses/retrieve", json={
        "client_public_key": poll["client_public_key"], "request_id": poll["request_id"],
        "retrieval_credential": retrieval_credential,
    })

    assert rejected.status_code == 400
    assert rejected.get_json() == {
        "error": {"message": "Invalid encrypted progress schema", "code": 400}
    }
    assert "encrypted_progress" not in retrieved.get_json()
    for secret in (credential, "progress-secret", "progress-key", "progress-iv"):
        assert secret not in rejected.get_data(as_text=True)


def test_compat_control_resolves_and_renews_authoritative_claim(monkeypatch):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    client = relay.app.test_client()
    node, credential, poll, _ = _claim_for_control(client)
    store = relay._api_v1_store()
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
    node, credential, poll, _ = _claim_for_control(client, request_id="control-ack")
    store = relay._api_v1_store()
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
    node, credential, _, _ = _claim_for_control(client, request_id="control-missing")

    class ForbiddenLegacyState(dict):
        def __getattribute__(self, name):
            if name not in {"__class__", "__getattribute__"}:
                raise AssertionError("control route accessed legacy correctness state")
            return super().__getattribute__(name)

    for name in ("known_servers", "client_inference_requests", "client_responses",
                 "api_v1_control_tombstones"):
        monkeypatch.setattr(relay, name, ForbiddenLegacyState())
    with relay.app.test_request_context("/api/v1/relay/servers/control", method="POST", json={
            "server_public_key": node, "request_id": "missing-request",
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


def test_diagnostics_deduplicates_store_identity_over_legacy_state():
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    client = relay.app.test_client()
    node, _, _, _ = _queue_for_owner(client, request_id="diagnostic-overlap")
    relay.known_servers[node] = {
        "last_ping": None,
        "last_ping_duration": 99,
        "capabilities": {"source": "legacy"},
    }
    relay.client_inference_requests[node] = [{"legacy": "queue"}] * 4

    response = client.get("/relay/diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    matching = [
        item for item in payload["registered_compute_nodes"]
        if item["server_public_key"] == node
    ]
    assert len(matching) == 1
    assert matching[0]["queue_depth"] == 1
    assert matching[0]["in_flight_count"] == 0
    assert matching[0]["capabilities"]["backend_class"] == "unknown"


def test_api_v1_only_diagnostics_never_use_legacy_state():
    relay._reset_api_v1_relay_state_store()
    relay.known_servers.clear()
    relay.known_servers["legacy-api-v1-ghost"] = {
        relay.API_V1_SERVER_MARKER: True,
        "last_ping": None,
    }

    assert relay._live_server_diagnostics(api_v1_only=True) == []


@pytest.mark.parametrize("method_name", ["list", "queued_requests", "active_claims"])
@pytest.mark.parametrize("endpoint", ["/healthz", "/relay/diagnostics"])
def test_operational_endpoints_bound_store_snapshot_failures(
    monkeypatch, method_name, endpoint
):
    relay.app.config["TESTING"] = True
    relay._reset_api_v1_relay_state_store()
    relay.known_servers.clear()
    client = relay.app.test_client()
    if method_name != "list":
        _queue_for_owner(client, request_id=f"failure-{method_name}")
    secret = "sensitive-store-failure"
    monkeypatch.setattr(
        relay._api_v1_store(),
        method_name,
        Mock(side_effect=RelayStateStoreError(secret)),
    )

    response = client.get(endpoint)

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "message": "Relay state is temporarily unavailable",
            "code": "state_backend_unavailable",
        }
    }
    assert secret.encode() not in response.data
