"""Backend contract tests for the bounded registration/lease state slice."""

from __future__ import annotations

import hashlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace

import pytest

# isort: off
from relay_state_store import ComputeNodeCapabilities
from relay_state_store import ComputeNodeRegistration
from relay_state_store import ClaimRecord
from relay_state_store import ClaimResult
from relay_state_store import EncryptedRequestEnvelope
from relay_state_store import EncryptedResponseEnvelope
from relay_state_store import InMemoryRelayStateStore
from relay_state_store import RelayStateCapacityExceeded
from relay_state_store import RelayStateCredentialMismatch
from relay_state_store import RelayStateConflict
from relay_state_store import RelayStateInvalidReservation
from relay_state_store import RelayStateNoCapacity
from relay_state_store import RelayStateStore
from relay_state_store import RelayStateStoreConfig
from relay_state_store import RelayStateStoreError
from relay_state_store import ResponseAcceptanceResult
from relay_state_store import ResponseRetrievalResult
from relay_state_store import SchedulerNodeState

# isort: on


class EpochClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def acknowledgement_key(label: str = "primary") -> bytes:
    """Deterministic non-secret test material; production callers must inject a secret."""
    return hashlib.sha256(f"relay-state-test:{label}".encode()).digest()


@pytest.fixture(params=["memory"])
def store_factory(request):
    """Factory seam to parameterize over real Valkey in a future slice."""

    def make(*, clock=None, **config_overrides):
        assert request.param == "memory"
        config = RelayStateStoreConfig(
            namespace="testing.cluster-a", **config_overrides
        )
        return InMemoryRelayStateStore(
            config,
            acknowledgement_key=acknowledgement_key(),
            epoch_time=clock or EpochClock(),
        )

    return make


@pytest.fixture
def capabilities() -> ComputeNodeCapabilities:
    return ComputeNodeCapabilities(
        supported_model_ids=("qwen3-8b-instruct",),
        active_context_tier="8k-fast",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=1024,
        maximum_output_tokens=2048,
        max_concurrency=2,
        backend_class="cuda",
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def digest_with_domain(value: str, domain: bytes) -> str:
    return hashlib.sha256(domain + value.encode()).hexdigest()


def envelope(ciphertext="ciphertext"):
    return EncryptedRequestEnvelope(
        protocol="tokenplace_api_v1_relay_e2ee",
        version=1,
        ciphertext=ciphertext,
        cipherkey="cipherkey",
        iv="iv",
    )


def response_envelope(ciphertext="sealed-response"):
    return EncryptedResponseEnvelope(
        protocol="tokenplace_api_v1_relay_e2ee",
        version=1,
        ciphertext=ciphertext,
        cipherkey="response-cipherkey",
        iv="response-iv",
    )


def reserve(store, request_id="request-a", **overrides):
    values = {
        "client_public_key": "client-key",
        "request_id": request_id,
        "requested_model_id": "qwen3-8b-instruct",
        "requested_context_tier": "8k-fast",
        "request_deadline_epoch": 1_700_000_100.0,
    }
    values.update(overrides)
    return store.select_and_reserve(**values)


def enqueue(store, selection, request_id="request-a", **overrides):
    values = {
        "client_public_key": "client-key",
        "request_id": request_id,
        "reservation_token": selection.reservation_token,
        "selected_node_id": selection.selected_node_id,
        "requested_model_id": selection.requested_model_id,
        "requested_context_tier": selection.requested_context_tier,
        "request_deadline_epoch": selection.request_deadline_epoch,
        "envelope": envelope(),
    }
    values.update(overrides)
    return store.enqueue_encrypted_request(**values)


def registered_store(
    store_factory, capabilities, clock=None, node_id="node-a", **config_overrides
):
    clock = clock or EpochClock()
    store = store_factory(clock=clock, claim_ttl_seconds=10, **config_overrides)
    store.register(node_id, capabilities, digest("owner"))
    return store, clock


def queued_work(store, request_id="request-a", **overrides):
    selection = reserve(store, request_id=request_id, **overrides)
    return enqueue(store, selection, request_id=request_id), selection


def claimed_work(store, request_id="request-a", consumer="worker-a", **overrides):
    queued_work(store, request_id, **overrides)
    return store.claim_queued_request("node-a", digest("owner"), consumer)


def accept_response(store, claim, **overrides):
    values = {
        "node_id": "node-a",
        "control_credential_digest": digest("owner"),
        "consumer_identity": "worker-a",
        "client_public_key": "client-key",
        "request_id": claim.request_id,
        "generation": claim.generation,
        "envelope": response_envelope(),
    }
    values.update(overrides)
    return store.accept_encrypted_response(**values)


def test_response_acceptance_atomically_finalizes_claim(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    claim = claimed_work(store)

    result = accept_response(store, claim)

    assert result == ResponseAcceptanceResult(
        "response_ready", claim.generation, 1_700_000_000.0, 1_700_000_300.0, True
    )
    assert store.active_claims("node-a") == ()
    assert store.queued_requests("node-a") == ()
    response = store.response_records()[0]
    assert response.client_public_key == "client-key"
    assert response.request_id == "request-a"
    assert response.envelope == response_envelope()
    assert response.status == "response_ready"
    assert store.terminal_records()[0].outcome == "completed"
    assert reserve(store, "request-b").created
    with pytest.raises(RelayStateConflict, match="terminal"):
        reserve(store)


def test_response_retry_is_once_only_and_conflicts_fail_closed(
    store_factory, capabilities
):
    store, _ = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    first = accept_response(store, claim)
    retry = accept_response(store, claim)
    assert first.new_outcome is True
    assert retry == replace(first, new_outcome=False)
    before = (store.response_records(), store.terminal_records())
    with pytest.raises(RelayStateConflict):
        accept_response(store, claim, envelope=response_envelope("different"))
    assert (store.response_records(), store.terminal_records()) == before


def test_response_retry_after_unregister_uses_retained_terminal(
    store_factory, capabilities
):
    store, clock = registered_store(
        store_factory,
        capabilities,
        response_replay_ttl_seconds=1,
        terminal_retention_seconds=2,
    )
    claim = claimed_work(store)
    first = accept_response(store, claim)
    assert store.unregister("node-a", digest("owner"))
    clock.value += 1

    assert store.response_records() == ()
    assert accept_response(store, claim) == replace(first, new_outcome=False)

    store.register("node-a", capabilities, digest("replacement"))
    with pytest.raises(RelayStateConflict):
        accept_response(store, claim, control_credential_digest=digest("replacement"))


def test_response_retry_after_registration_expiry_uses_retained_terminal(
    store_factory, capabilities
):
    store, clock = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    first = accept_response(store, claim)
    clock.value += store.config.lease_ttl_seconds

    assert accept_response(store, claim) == replace(first, new_outcome=False)


@pytest.mark.parametrize(
    "override,error",
    [
        ({"node_id": "node-b"}, RelayStateCredentialMismatch),
        ({"control_credential_digest": digest("wrong")}, RelayStateCredentialMismatch),
        ({"consumer_identity": "wrong-worker"}, RelayStateCredentialMismatch),
        ({"client_public_key": "wrong-client"}, RelayStateConflict),
        ({"request_id": "wrong-request"}, RelayStateConflict),
        ({"generation": 999}, RelayStateConflict),
    ],
)
def test_response_enforces_fenced_identity_and_owner(
    store_factory, capabilities, override, error
):
    store, _ = registered_store(store_factory, capabilities)
    store.register("node-b", capabilities, digest("node-b"))
    claim = claimed_work(store)
    with pytest.raises(error):
        accept_response(store, claim, **override)
    assert store.response_records() == ()
    assert len(store.active_claims("node-a")) == 1


def test_missing_expired_claim_and_request_deadline_fail_without_mutation(
    store_factory, capabilities
):
    store, clock = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    clock.value = claim.lease_expires_at_epoch
    with pytest.raises(RelayStateConflict, match="expired"):
        accept_response(store, claim)
    store, clock = registered_store(store_factory, capabilities)
    claim = claimed_work(store, request_deadline_epoch=clock.value + 5)
    clock.value = claim.request_deadline_epoch
    with pytest.raises(RelayStateConflict, match="expired"):
        accept_response(store, claim)
    assert store.response_records() == store.terminal_records() == ()


def test_simultaneous_responses_have_one_new_outcome(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: accept_response(store, claim), range(8)))
    assert sum(result.new_outcome for result in results) == 1
    assert len(store.response_records()) == len(store.terminal_records()) == 1


def test_response_racing_renewal_leaves_coherent_terminal_state(
    store_factory, capabilities
):
    store, _ = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        response_future = pool.submit(accept_response, store, claim)
        renewal_future = pool.submit(
            store.renew_claim,
            "node-a",
            digest("owner"),
            "worker-a",
            "client-key",
            "request-a",
            claim.generation,
        )
    assert response_future.result().new_outcome
    assert renewal_future.result().state in {"continued", "missing_or_expired"}
    assert store.active_claims("node-a") == ()
    assert len(store.response_records()) == len(store.terminal_records()) == 1


def test_terminal_fences_unregister_and_node_id_reuse(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    accept_response(store, claim)
    assert store.unregister("node-a", digest("owner"))
    store.register("node-a", capabilities, digest("replacement"))
    with pytest.raises(RelayStateConflict):
        accept_response(
            store,
            claim,
            control_credential_digest=digest("replacement"),
            consumer_identity="worker-a",
        )
    assert store.terminal_records()[0].generation == claim.generation


@pytest.mark.parametrize(
    "limited_bound,same_client",
    [
        ("max_responses", False),
        ("max_responses_per_client", True),
        ("max_terminal_records", False),
        ("max_terminal_records_per_client", True),
    ],
)
def test_response_and_terminal_bounds_are_independent(
    store_factory, capabilities, limited_bound, same_client
):
    bounds = {
        "max_responses": 4,
        "max_responses_per_client": 4,
        "max_terminal_records": 4,
        "max_terminal_records_per_client": 4,
        limited_bound: 1,
    }
    store, _ = registered_store(store_factory, capabilities, **bounds)
    first = claimed_work(store)
    second_client = "client-key" if same_client else "client-key-b"
    selection = reserve(store, "request-b", client_public_key=second_client)
    enqueue(
        store,
        selection,
        "request-b",
        client_public_key=second_client,
    )
    second = store.claim_queued_request("node-a", digest("owner"), "worker-a")
    accept_response(store, first)
    with pytest.raises(RelayStateCapacityExceeded):
        accept_response(store, second, client_public_key=second_client)
    assert len(store.response_records()) == len(store.terminal_records()) == 1
    assert len(store.active_claims("node-a")) == 1


def test_response_and_terminal_expiry_are_inclusive_and_deterministic(
    store_factory, capabilities
):
    clock = EpochClock()
    store, _ = registered_store(
        store_factory,
        capabilities,
        clock=clock,
        response_replay_ttl_seconds=2,
        terminal_retention_seconds=3,
    )
    claim = claimed_work(store)
    accept_response(store, claim)
    clock.value += 2
    assert store.response_records() == ()
    assert len(store.terminal_records()) == 1
    clock.value += 1
    assert store.terminal_records() == ()
    assert store.queued_requests("node-a") == store.active_claims("node-a") == ()


def test_terminal_is_authoritative_at_inclusive_response_expiry(
    store_factory, capabilities
):
    clock = EpochClock()
    store, _ = registered_store(
        store_factory,
        capabilities,
        clock=clock,
        response_replay_ttl_seconds=2,
        terminal_retention_seconds=3,
    )
    claim = claimed_work(store)
    original = accept_response(store, claim)
    terminal_before = store.terminal_records()[0]
    clock.value = original.replay_expires_at_epoch

    retry = accept_response(store, claim)

    assert store.response_records() == ()
    expired_terminal = replace(terminal_before, retrieval_state="retrieval_expired")
    assert store.terminal_records() == (expired_terminal,)
    assert retry == replace(original, new_outcome=False)
    with pytest.raises(RelayStateConflict):
        accept_response(store, claim, envelope=response_envelope("conflict"))
    assert store.response_records() == ()
    assert store.terminal_records() == (expired_terminal,)


def test_terminal_retry_releases_capacity_exactly_once(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    completed = claimed_work(store)
    first = accept_response(store, completed)
    later = claimed_work(store, "request-b")

    assert accept_response(store, completed) == replace(first, new_outcome=False)
    active = store.active_claims("node-a")
    assert len(active) == 1
    assert (active[0].request_identity_digest, active[0].generation) == (
        digest_with_domain("request-b", b"request\0"),
        later.generation,
    )


def test_completed_identity_fencing_cleanup_and_node_id_reuse(
    store_factory, capabilities
):
    clock = EpochClock()
    store, _ = registered_store(
        store_factory,
        capabilities,
        clock=clock,
        response_replay_ttl_seconds=2,
        terminal_retention_seconds=3,
    )
    completed = claimed_work(store)
    accept_response(store, completed)
    with pytest.raises(RelayStateConflict, match="terminal"):
        reserve(store)

    assert store.unregister("node-a", digest("owner"))
    store.register("node-a", capabilities, digest("replacement"))
    clock.value += 3
    assert store.terminal_records() == ()
    selection = reserve(store)
    enqueue(store, selection)
    replacement = store.claim_queued_request(
        "node-a", digest("replacement"), "worker-a"
    )

    assert replacement.generation > completed.generation
    with pytest.raises(RelayStateConflict, match="generation"):
        accept_response(
            store,
            completed,
            control_credential_digest=digest("replacement"),
        )


def test_response_records_are_immutable_defensive_and_repr_redacted(
    store_factory, capabilities, caplog
):
    store, _ = registered_store(store_factory, capabilities)
    claim = claimed_work(store)
    secret_values = {
        "client-key",
        "request-a",
        "node-a",
        "worker-a",
        digest("owner"),
        "sealed-response",
        "response-cipherkey",
        "response-iv",
    }
    result = accept_response(store, claim)
    response = store.response_records()[0]
    terminal = store.terminal_records()[0]
    assert terminal.replay_expires_at_epoch == result.replay_expires_at_epoch
    assert store.terminal_records()[0] is not terminal
    with pytest.raises(FrozenInstanceError):
        response.envelope.ciphertext = "changed"
    rendered = repr(response) + repr(terminal) + repr(result) + repr(response.envelope)
    assert all(value not in rendered for value in secret_values)
    assert all(value not in caplog.text for value in secret_values)
    assert "status='future_state'" in repr(replace(response, status="future_state"))
    assert "outcome='future_outcome'" in repr(
        replace(terminal, outcome="future_outcome")
    )


def test_response_envelope_allowlist_type_and_utf8_bytes(store_factory, capabilities):
    with pytest.raises(TypeError):
        EncryptedResponseEnvelope(
            protocol="tokenplace_api_v1_relay_e2ee",
            version=1,
            ciphertext="ciphertext",
            cipherkey="key",
            iv="iv",
            plaintext="forbidden",
        )
    store, _ = registered_store(
        store_factory, capabilities, max_response_envelope_bytes=120
    )
    claim = claimed_work(store)
    with pytest.raises(RelayStateStoreError, match="byte bound"):
        accept_response(store, claim, envelope=response_envelope("é" * 100))
    assert store.response_records() == ()


def completed_response(store_factory, capabilities, **config_overrides):
    store, clock = registered_store(store_factory, capabilities, **config_overrides)
    _, selection = queued_work(store)
    claim = store.claim_queued_request("node-a", digest("owner"), "worker-a")
    acceptance = accept_response(store, claim)
    return store, clock, claim, acceptance, selection.reservation_token


def retrieve_response(store, credential, acknowledgement_token=None, **identity):
    return store.retrieve_encrypted_response(
        identity.get("client_public_key", "client-key"),
        identity.get("request_id", "request-a"),
        credential,
        acknowledgement_token,
    )


def test_response_retrieval_is_replayable_until_valid_acknowledgement(
    store_factory, capabilities
):
    store, _, claim, acceptance, credential = completed_response(
        store_factory, capabilities
    )

    first = retrieve_response(store, credential)
    second = retrieve_response(store, credential)

    assert (
        first
        == second
        == ResponseRetrievalResult(
            "response_ready",
            response_envelope(),
            first.acknowledgement_token,
            acceptance.replay_expires_at_epoch,
        )
    )
    assert first.acknowledgement_token is not None
    assert len(store.response_records()) == 1
    assert store.terminal_records()[0].outcome == "completed"

    acknowledged = retrieve_response(store, credential, first.acknowledgement_token)
    duplicate = retrieve_response(store, credential, first.acknowledgement_token)
    assert acknowledged == duplicate == ResponseRetrievalResult("acknowledged")
    assert retrieve_response(store, credential, "0" * 64) == ResponseRetrievalResult(
        "invalid_acknowledgement"
    )
    assert store.response_records() == ()
    assert store.terminal_records()[0].outcome == "completed"
    assert accept_response(store, claim) == replace(acceptance, new_outcome=False)


@pytest.mark.parametrize("credential", ["0" * 64, "wrong", None, b"not-text"])
def test_response_retrieval_requires_the_original_client_credential(
    store_factory, capabilities, credential
):
    store, _, _, _, valid_credential = completed_response(
        store_factory, capabilities
    )

    unauthenticated = retrieve_response(store, credential)
    assert unauthenticated == ResponseRetrievalResult(
        "invalid_retrieval_credential"
    )
    assert unauthenticated.envelope is None
    assert unauthenticated.acknowledgement_token is None
    assert len(store.response_records()) == 1

    token = retrieve_response(store, valid_credential).acknowledgement_token
    assert token is not None
    assert retrieve_response(store, credential, token) == ResponseRetrievalResult(
        "invalid_retrieval_credential"
    )
    assert len(store.response_records()) == 1


@pytest.mark.parametrize("token", ["wrong", "0" * 64, b"not-text", "f" * 63])
def test_wrong_or_malformed_acknowledgement_does_not_consume(
    store_factory, capabilities, token
):
    store, _, _, _, credential = completed_response(store_factory, capabilities)
    result = retrieve_response(store, credential, token)
    assert result == ResponseRetrievalResult("invalid_acknowledgement")
    assert len(store.response_records()) == 1
    assert store.terminal_records()[0].retrieval_state == "response_ready"


def test_acknowledgements_are_identity_and_key_bound(store_factory, capabilities):
    store, _, _, _, credential = completed_response(store_factory, capabilities)
    token = retrieve_response(store, credential).acknowledgement_token
    assert (
        retrieve_response(
            store, credential, token, client_public_key="another-client"
        ).state
        == "unknown"
    )
    assert (
        retrieve_response(store, credential, token, request_id="another-request").state
        == "unknown"
    )

    other = InMemoryRelayStateStore(
        store.config,
        acknowledgement_key=acknowledgement_key("other"),
        epoch_time=EpochClock(),
    )
    other.register("node-a", capabilities, digest("owner"))
    _, other_selection = queued_work(other)
    other_claim = other.claim_queued_request("node-a", digest("owner"), "worker-a")
    accept_response(other, other_claim)
    assert (
        retrieve_response(
            other, other_selection.reservation_token, token
        ).state
        == "invalid_acknowledgement"
    )
    assert len(store.response_records()) == len(other.response_records()) == 1


def test_equivalent_store_clients_derive_the_same_token(store_factory, capabilities):
    first, _, _, _, first_credential = completed_response(store_factory, capabilities)
    second, _, _, _, second_credential = completed_response(store_factory, capabilities)
    first_result = retrieve_response(first, first_credential)
    second_result = retrieve_response(second, second_credential)
    assert first_result.acknowledgement_token == second_result.acknowledgement_token


def test_concurrent_retrieval_and_acknowledgement_are_atomic(
    store_factory, capabilities
):
    store, _, _, _, credential = completed_response(store_factory, capabilities)
    token = retrieve_response(store, credential).acknowledgement_token
    with ThreadPoolExecutor(max_workers=8) as pool:
        reads = list(
            pool.map(
                lambda _: retrieve_response(store, credential),
                range(8),
            )
        )
    assert len(set(reads)) == 1
    assert reads[0].state == "response_ready"

    with ThreadPoolExecutor(max_workers=8) as pool:
        acknowledgements = list(
            pool.map(
                lambda _: retrieve_response(store, credential, token),
                range(8),
            )
        )
    assert {result.state for result in acknowledgements} == {"acknowledged"}
    assert store.response_records() == ()


def test_retrieval_racing_acknowledgement_leaves_a_coherent_state(
    store_factory, capabilities
):
    store, _, _, _, credential = completed_response(store_factory, capabilities)
    token = retrieve_response(store, credential).acknowledgement_token
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(retrieve_response, store, credential)
        ack = pool.submit(retrieve_response, store, credential, token)
    assert read.result().state in {"response_ready", "acknowledged"}
    assert ack.result().state == "acknowledged"
    assert (
        retrieve_response(store, credential).state == "acknowledged"
    )


def test_retrieval_expiry_is_inclusive_fixed_and_preserves_completion(
    store_factory, capabilities
):
    store, clock, claim, acceptance, credential = completed_response(
        store_factory,
        capabilities,
        response_replay_ttl_seconds=2,
        terminal_retention_seconds=3,
    )
    ready = retrieve_response(store, credential)
    clock.value = acceptance.replay_expires_at_epoch
    expired = retrieve_response(store, credential, ready.acknowledgement_token)
    assert expired == ResponseRetrievalResult("retrieval_expired")
    assert store.response_records() == ()
    assert store.terminal_records()[0].outcome == "completed"
    assert accept_response(store, claim) == replace(acceptance, new_outcome=False)
    assert retrieve_response(store, credential) == expired
    clock.value += 1
    assert (
        retrieve_response(store, credential).state == "unknown"
    )


def test_acknowledgement_racing_expiry_has_one_coherent_terminal_state(
    store_factory, capabilities
):
    store, clock, _, acceptance, credential = completed_response(
        store_factory,
        capabilities,
        response_replay_ttl_seconds=2,
        terminal_retention_seconds=3,
    )
    token = retrieve_response(store, credential).acknowledgement_token
    clock.value = acceptance.replay_expires_at_epoch
    with ThreadPoolExecutor(max_workers=2) as pool:
        acknowledgement = pool.submit(
            retrieve_response,
            store,
            credential,
            token,
        )
        cleanup = pool.submit(store.response_records)
    assert acknowledgement.result().state == "retrieval_expired"
    assert cleanup.result() == ()
    assert store.terminal_records()[0].retrieval_state == "retrieval_expired"


def test_acknowledgement_key_validation_and_sensitive_representations(
    store_factory, capabilities
):
    config = RelayStateStoreConfig(namespace="testing.keys")
    for invalid in (None, "x" * 32, b"x" * 31, bytearray(32)):
        with pytest.raises(RelayStateStoreError, match="at least 256 bits") as error:
            InMemoryRelayStateStore(config, acknowledgement_key=invalid)
        assert repr(invalid) not in str(error.value)

    store, _, _, _, credential = completed_response(store_factory, capabilities)
    result = retrieve_response(store, credential)
    terminal = store.terminal_records()[0]
    rendered = repr(store) + repr(result) + repr(terminal)
    assert result.acknowledgement_token not in rendered
    assert terminal.acknowledgement_digest not in rendered
    assert "sealed-response" not in rendered
    assert "client-key" not in rendered


def test_claim_fifo_empty_poll_and_capacity_is_unchanged(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    queued_work(store, "request-a")
    queued_work(store, "request-b")

    first = store.claim_queued_request("node-a", digest("owner"), "worker-a")
    second = store.claim_queued_request("node-a", digest("owner"), "worker-a")

    assert first.state == second.state == "claimed"
    assert (first.request_id, second.request_id) == ("request-a", "request-b")
    assert store.queued_requests("node-a") == ()
    assert len(store.active_claims("node-a")) == 2
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-c")
    assert store.claim_queued_request(
        "node-a", digest("owner"), "worker-a"
    ) == ClaimResult("empty")


def test_claims_are_node_local_and_owner_authenticated(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    store.register("node-b", capabilities, digest("owner-b"))
    queued_work(store)
    with pytest.raises(RelayStateCredentialMismatch):
        store.claim_queued_request("node-a", digest("wrong"), "worker")
    assert (
        store.claim_queued_request("node-b", digest("owner-b"), "worker").state
        == "empty"
    )
    assert (
        store.claim_queued_request("node-a", digest("owner"), "worker").request_id
        == "request-a"
    )


def test_concurrent_claim_has_one_winner_per_item(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    queued_work(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.claim_queued_request(
                    "node-a", digest("owner"), "worker"
                ),
                range(8),
            )
        )
    assert sum(result.state == "claimed" for result in results) == 1
    assert sum(result.state == "empty" for result in results) == 7


def test_claim_reclaim_is_inclusive_and_fences_old_generation(
    store_factory, capabilities
):
    store, clock = registered_store(store_factory, capabilities)
    queued_work(store)
    first = store.claim_queued_request("node-a", digest("owner"), "worker-a")
    clock.value = first.lease_expires_at_epoch - 0.001
    assert (
        store.claim_queued_request("node-a", digest("owner"), "worker-b").state
        == "empty"
    )
    clock.value = first.lease_expires_at_epoch
    second = store.claim_queued_request("node-a", digest("owner"), "worker-b")
    assert second.state == "reclaimed" and second.generation > first.generation
    stale = store.renew_claim(
        "node-a",
        digest("owner"),
        "worker-a",
        "client-key",
        "request-a",
        first.generation,
    )
    assert stale.state == "stale_generation"
    assert store.active_claims("node-a")[0].generation == second.generation


def test_expired_claim_is_reclaimable_not_active_or_capacity_consuming(
    store_factory, capabilities
):
    store, clock = registered_store(
        store_factory, capabilities, max_claims=1, max_claims_per_node=1
    )
    store.register("node-b", capabilities, digest("owner-b"))
    queued_work(store, "request-a")
    first = store.claim_queued_request("node-a", digest("owner"), "worker-a")

    clock.value = first.lease_expires_at_epoch
    assert store.active_claims("node-a") == ()
    assert [item.request_id for item in store.queued_requests("node-a")] == [
        "request-a"
    ]

    selection = reserve(store, "request-b")
    assert selection.selected_node_id == "node-b"
    enqueue(store, selection, "request-b")
    second = store.claim_queued_request("node-b", digest("owner-b"), "worker-b")
    assert second.state == "claimed"

    with pytest.raises(RelayStateCapacityExceeded):
        store.claim_queued_request("node-a", digest("owner"), "worker-new")


def test_expired_claim_releases_per_node_capacity_for_reclaim(
    store_factory, capabilities
):
    store, clock = registered_store(
        store_factory, capabilities, max_claims=2, max_claims_per_node=1
    )
    queued_work(store, "request-a")
    first = store.claim_queued_request("node-a", digest("owner"), "worker-a")

    clock.value = first.lease_expires_at_epoch
    reclaimed = store.claim_queued_request("node-a", digest("owner"), "worker-new")

    assert reclaimed.state == "reclaimed"
    assert reclaimed.generation > first.generation


def test_multiple_reclaims_strictly_increase_generation(store_factory, capabilities):
    store, clock = registered_store(store_factory, capabilities, lease_ttl_seconds=60)
    queued_work(store)
    claims = [store.claim_queued_request("node-a", digest("owner"), "worker-0")]

    for attempt in range(1, 4):
        clock.value = claims[-1].lease_expires_at_epoch
        claims.append(
            store.claim_queued_request("node-a", digest("owner"), f"worker-{attempt}")
        )

    assert [claim.state for claim in claims] == ["claimed"] + ["reclaimed"] * 3
    assert all(
        newer.generation > older.generation for older, newer in zip(claims, claims[1:])
    )


def test_claimed_idempotent_results_have_claimed_state_and_no_new_token(
    store_factory, capabilities
):
    store, _ = registered_store(store_factory, capabilities)
    queued, selection = queued_work(store)
    store.claim_queued_request("node-a", digest("owner"), "worker")

    selected_retry = reserve(store)
    assert selected_retry.state == "claimed"
    assert selected_retry.reservation_token is None
    assert not selected_retry.created

    enqueue_retry = enqueue(store, selection)
    assert enqueue_retry.state == "claimed"
    assert not enqueue_retry.created
    assert enqueue_retry.sequence == queued.sequence


def test_claim_renewal_auth_identity_and_deadline_bounds(store_factory, capabilities):
    store, clock = registered_store(store_factory, capabilities)
    queued_work(store, request_deadline_epoch=clock.value + 12)
    claim = store.claim_queued_request("node-a", digest("owner"), "worker")
    assert (
        store.renew_claim(
            "node-a",
            digest("wrong"),
            "worker",
            "client-key",
            "request-a",
            claim.generation,
        ).state
        == "owner_mismatch"
    )
    assert (
        store.renew_claim(
            "node-a",
            digest("owner"),
            "other",
            "client-key",
            "request-a",
            claim.generation,
        ).state
        == "owner_mismatch"
    )
    assert (
        store.renew_claim(
            "node-a", digest("owner"), "worker", "client-key", "wrong", claim.generation
        ).state
        == "missing_or_expired"
    )
    clock.value += 5
    renewed = store.renew_claim(
        "node-a", digest("owner"), "worker", "client-key", "request-a", claim.generation
    )
    assert renewed.state == "continued"
    assert renewed.lease_expires_at_epoch == clock.value + 7
    clock.value = renewed.lease_expires_at_epoch
    assert (
        store.renew_claim(
            "node-a",
            digest("owner"),
            "worker",
            "client-key",
            "request-a",
            claim.generation,
        ).state
        == "missing_or_expired"
    )
    assert store.active_claims("node-a") == ()


def test_exact_request_deadline_prevents_renewal_and_reclaim(
    store_factory, capabilities
):
    store, clock = registered_store(store_factory, capabilities)
    deadline = clock.value + 5
    queued_work(store, request_deadline_epoch=deadline)
    claim = store.claim_queued_request("node-a", digest("owner"), "worker")

    clock.value = deadline

    assert (
        store.renew_claim(
            "node-a",
            digest("owner"),
            "worker",
            "client-key",
            "request-a",
            claim.generation,
        ).state
        == "missing_or_expired"
    )
    assert (
        store.claim_queued_request("node-a", digest("owner"), "worker-new").state
        == "empty"
    )


def test_wrong_node_renewal_fails_closed_without_mutation(store_factory, capabilities):
    store, _ = registered_store(store_factory, capabilities)
    store.register("node-b", capabilities, digest("owner-b"))
    queued_work(store)
    claim = store.claim_queued_request("node-a", digest("owner"), "worker")
    before = store.active_claims("node-a")

    result = store.renew_claim(
        "node-b",
        digest("owner-b"),
        "worker",
        "client-key",
        "request-a",
        claim.generation,
    )

    assert result.state == "owner_mismatch"
    assert store.active_claims("node-a") == before


def test_claim_records_are_defensive_ciphertext_only_and_redacted(
    store_factory, capabilities
):
    store, _ = registered_store(store_factory, capabilities)
    queued_work(store)
    result = store.claim_queued_request("node-a", digest("owner"), "unsafe-worker")
    record = store.active_claims("node-a")[0]
    assert isinstance(record, ClaimRecord)
    assert record.envelope == envelope()
    assert "unsafe-worker" not in repr(record)
    assert "ciphertext" not in repr(record)
    assert "client-key" not in repr(result) and "request-a" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        record.generation = 99


def test_unregister_and_node_id_reuse_cannot_restore_old_generation(
    store_factory, capabilities
):
    store, _ = registered_store(store_factory, capabilities)
    queued_work(store)
    old = store.claim_queued_request("node-a", digest("owner"), "worker")
    assert store.unregister("node-a", digest("owner"))
    store.register("node-a", capabilities, digest("new-owner"))
    queued_work(store)
    new = store.claim_queued_request("node-a", digest("new-owner"), "new-worker")
    assert new.generation > old.generation
    assert (
        store.renew_claim(
            "node-a",
            digest("new-owner"),
            "worker",
            "client-key",
            "request-a",
            old.generation,
        ).state
        == "stale_generation"
    )


def test_registration_expiry_and_node_id_reuse_cannot_restore_old_generation(
    store_factory, capabilities
):
    clock = EpochClock()
    store, _ = registered_store(
        store_factory, capabilities, clock=clock, lease_ttl_seconds=5
    )
    queued_work(store)
    old = store.claim_queued_request("node-a", digest("owner"), "worker")

    clock.value += 5
    store.register("node-a", capabilities, digest("new-owner"))
    queued_work(store)
    new = store.claim_queued_request("node-a", digest("new-owner"), "new-worker")

    assert new.generation > old.generation
    assert (
        store.renew_claim(
            "node-a",
            digest("new-owner"),
            "worker",
            "client-key",
            "request-a",
            old.generation,
        ).state
        == "stale_generation"
    )


def test_concurrent_renewal_or_reclaim_has_one_serialized_result(
    store_factory, capabilities
):
    store, clock = registered_store(store_factory, capabilities)
    queued_work(store)
    old = store.claim_queued_request("node-a", digest("owner"), "worker")
    clock.value = old.lease_expires_at_epoch
    with ThreadPoolExecutor(max_workers=2) as pool:
        renewal = pool.submit(
            store.renew_claim,
            "node-a",
            digest("owner"),
            "worker",
            "client-key",
            "request-a",
            old.generation,
        )
        reclaim = pool.submit(
            store.claim_queued_request, "node-a", digest("owner"), "worker-new"
        )
    assert renewal.result().state in {"missing_or_expired", "stale_generation"}
    assert reclaim.result().state == "reclaimed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim_ttl_seconds", 0),
        ("max_claims", 0),
        ("max_claims_per_node", 0),
        ("max_consumer_identity_bytes", 0),
    ],
)
def test_claim_configuration_bounds(field, value):
    with pytest.raises(RelayStateStoreError):
        RelayStateStoreConfig(namespace="test", **{field: value})


def test_register_lookup_and_duplicate_require_owner_digest(
    store_factory, capabilities
):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=30)
    assert isinstance(store, RelayStateStore)

    first = store.register("node-a", capabilities, digest("owner"))
    clock.value += 5
    with pytest.raises(RelayStateCredentialMismatch):
        store.register("node-a", capabilities, digest("attacker"))

    assert store.get("node-a") == first
    duplicate = store.register("node-a", capabilities, digest("owner"))

    assert duplicate.registered_at_epoch == first.registered_at_epoch
    assert duplicate.lease_expires_at_epoch == clock.value + 30
    assert duplicate.control_credential_digest == digest("owner")
    assert store.list() == (duplicate,)


def test_renew_is_idempotent_and_can_atomically_change_capabilities(
    store_factory, capabilities
):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=10)
    store.register("node-a", capabilities, digest("owner"))
    changed = ComputeNodeCapabilities(
        supported_model_ids=("qwen3-8b-instruct", "other-model"),
        active_context_tier="64k-full",
        maximum_total_context_tokens=65536,
        default_output_token_reservation=2048,
        maximum_output_tokens=4096,
        max_concurrency=4,
        backend_class="metal",
    )

    renewed = store.renew("node-a", digest("owner"), capabilities=changed)
    repeated = store.renew("node-a", digest("owner"), capabilities=changed)

    assert renewed == repeated
    assert repeated is not None and repeated.capabilities == changed
    assert len(store.list()) == 1


def test_expiry_uses_injected_epoch_clock_and_boundary_is_inclusive(
    store_factory, capabilities
):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=10)
    record = store.register("node-a", capabilities, digest("owner"))
    assert (
        record.lease_expires_at_epoch > 1_000_000_000
    )  # epoch, not monotonic process time

    clock.value = record.lease_expires_at_epoch
    assert store.get("node-a") is None
    assert store.expire() == ()


def test_expire_returns_each_removed_record_once(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=1)
    registered = store.register("node-a", capabilities, digest("owner"))
    clock.value += 1
    assert store.expire() == (registered,)
    assert store.expire() == ()


def test_list_and_expire_are_sorted_by_node_id(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=1)
    registered = {
        node_id: store.register(node_id, capabilities, digest(node_id))
        for node_id in ("node-c", "node-a", "node-b")
    }

    assert store.list() == tuple(registered[node_id] for node_id in sorted(registered))
    clock.value += 1
    assert store.expire() == tuple(
        registered[node_id] for node_id in sorted(registered)
    )


def test_unregister_and_unknown_node_behavior(store_factory, capabilities):
    store = store_factory()
    store.register("node-a", capabilities, digest("owner"))
    with pytest.raises(RelayStateCredentialMismatch):
        store.unregister("node-a", digest("wrong"))
    assert store.unregister("node-a", digest("owner")) is True
    assert store.unregister("node-a", digest("owner")) is False
    assert store.renew("missing", digest("owner")) is None


@pytest.mark.parametrize("namespace", ["", "UPPER", "contains space", "x" * 129])
def test_namespace_is_bounded_and_validated(namespace):
    with pytest.raises(RelayStateStoreError):
        RelayStateStoreConfig(namespace=namespace)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", 2])
def test_schema_version_requires_the_supported_non_boolean_integer(schema_version):
    with pytest.raises(RelayStateStoreError, match="schema"):
        RelayStateStoreConfig(namespace="test", schema_version=schema_version)

    assert RelayStateStoreConfig(namespace="test", schema_version=1).schema_version == 1


def test_ttl_and_record_bounds_are_validated():
    with pytest.raises(RelayStateStoreError, match="TTL"):
        RelayStateStoreConfig(namespace="test", lease_ttl_seconds=0)
    with pytest.raises(RelayStateStoreError, match="bound"):
        RelayStateStoreConfig(namespace="test", max_compute_nodes=0)


def test_registration_rejects_an_infinite_computed_deadline(
    store_factory, capabilities
):
    store = store_factory(clock=EpochClock(1e308), lease_ttl_seconds=1e308)

    with pytest.raises(RelayStateStoreError, match="deadline"):
        store.register("node-a", capabilities, digest("owner"))

    assert store.list() == ()


def test_renewal_rejects_an_infinite_deadline_without_mutating_live_record(
    store_factory, capabilities
):
    clock = EpochClock(0.0)
    store = store_factory(clock=clock, lease_ttl_seconds=1e308)
    original = store.register("node-a", capabilities, digest("owner"))
    clock.value = 9e307

    with pytest.raises(RelayStateStoreError, match="deadline"):
        store.renew("node-a", digest("owner"))

    clock.value = 0.0
    assert store.get("node-a") == original


def test_unknown_renewal_ignores_an_infinite_computed_deadline(
    store_factory, capabilities
):
    store = store_factory(clock=EpochClock(1e308), lease_ttl_seconds=1e308)

    assert store.renew("missing", digest("owner"), capabilities=capabilities) is None


def test_boundary_expired_renewal_ignores_an_infinite_computed_deadline(
    store_factory, capabilities
):
    clock = EpochClock(0.0)
    store = store_factory(clock=clock, lease_ttl_seconds=1e308)
    store.register("node-a", capabilities, digest("owner"))
    clock.value = 1e308

    assert store.renew("node-a", digest("owner")) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_compute_nodes", 1.5),
        ("max_compute_nodes", True),
        ("max_compute_nodes", "10"),
        ("max_node_id_bytes", 1.5),
        ("max_node_id_bytes", True),
        ("max_node_id_bytes", "10"),
    ],
)
def test_integer_record_bounds_reject_invalid_types(field, value):
    with pytest.raises(RelayStateStoreError, match="bound"):
        RelayStateStoreConfig(namespace="test", **{field: value})


def test_capacity_and_node_id_bounds(store_factory, capabilities):
    store = store_factory(max_compute_nodes=1, max_node_id_bytes=6)
    store.register("node-a", capabilities, digest("owner"))
    with pytest.raises(RelayStateCapacityExceeded):
        store.register("node-b", capabilities, digest("owner-b"))
    with pytest.raises(RelayStateStoreError, match="node ID"):
        store.get("too-long")


def test_capability_bounds_reuse_current_scheduler_limits(capabilities):
    with pytest.raises(RelayStateStoreError):
        replace(capabilities, supported_model_ids=tuple(f"m{i}" for i in range(65)))
    with pytest.raises(RelayStateStoreError):
        replace(capabilities, max_concurrency=129)
    with pytest.raises(RelayStateStoreError):
        replace(capabilities, maximum_total_context_tokens=1)


def test_capabilities_match_api_v1_canonical_normalisation():
    normalized = ComputeNodeCapabilities(
        supported_model_ids=(" QWEN3-8B ", "qwen3-8b", " Llama-3 ", "QWEN3-8B"),
        active_context_tier=" 8K-FAST ",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=1,
        maximum_output_tokens=1,
        max_concurrency=1,
        backend_class=" CUDA ",
    )

    assert normalized.supported_model_ids == ("qwen3-8b", "llama-3")
    assert normalized.active_context_tier == "8k-fast"
    assert normalized.backend_class == "cuda"
    assert replace(normalized, backend_class=" Unsupported ").backend_class == "unknown"


def test_records_are_immutable_and_reads_do_not_expose_mutable_state(
    store_factory, capabilities
):
    store = store_factory()
    returned = store.register("node-a", capabilities, digest("owner"))
    with pytest.raises(FrozenInstanceError):
        returned.node_id = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        returned.capabilities.supported_model_ids[0] = "mutated"  # type: ignore[index]
    assert store.get("node-a") == returned


def test_raw_credentials_and_application_payloads_are_outside_the_typed_contract(
    store_factory, capabilities
):
    store = store_factory()
    with pytest.raises(RelayStateStoreError, match="digest"):
        store.register("node-a", capabilities, "raw-control-credential")
    with pytest.raises(TypeError):
        ComputeNodeCapabilities(
            supported_model_ids=("model",),
            active_context_tier="8k-fast",
            maximum_total_context_tokens=8192,
            default_output_token_reservation=1,
            maximum_output_tokens=1,
            max_concurrency=1,
            messages=[{"content": "plaintext"}],  # type: ignore[call-arg]
        )
    with pytest.raises(RelayStateStoreError, match="ComputeNodeCapabilities"):
        payload = {"prompt": "plaintext", "arbitrary": "x" * 1_000_000}
        store.register("node-a", payload, digest("owner"))  # type: ignore[arg-type]


def test_plaintext_and_arbitrary_payload_fields_are_absent_from_typed_api():
    forbidden = {
        "control_credential",
        "prompt",
        "messages",
        "tools",
        "tool_data",
        "model_output",
        "relay_private_key",
        "payload",
    }
    typed_fields = {
        field.name
        for record_type in (
            RelayStateStoreConfig,
            ComputeNodeCapabilities,
            ComputeNodeRegistration,
        )
        for field in fields(record_type)
    }
    public_parameters = {
        parameter
        for method in (RelayStateStore.register, RelayStateStore.renew)
        for parameter in inspect.signature(method).parameters
    }

    assert forbidden.isdisjoint(typed_fields)
    assert forbidden.isdisjoint(public_parameters)


def test_concurrent_registration_and_renewal_never_duplicate_or_tear_state(
    store_factory, capabilities
):
    store = store_factory(lease_ttl_seconds=30)
    owner_digest = digest("owner")

    snapshots = tuple(
        replace(
            capabilities,
            supported_model_ids=(f"model-{index}", f"shared-{index}"),
            max_concurrency=index + 1,
            backend_class=("cuda" if index % 2 else "metal"),
        )
        for index in range(8)
    )

    def transition(index: int):
        snapshot = snapshots[index % len(snapshots)]
        if index % 2:
            return store.register("node-a", snapshot, owner_digest)
        return store.renew("node-a", owner_digest, capabilities=snapshot)

    store.register("node-a", capabilities, owner_digest)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(transition, range(200)))

    assert all(
        result is not None
        and result.capabilities in snapshots
        and result.control_credential_digest == owner_digest
        for result in results
    )
    records = store.list()
    assert len(records) == 1
    assert records[0].node_id == "node-a"
    assert records[0].capabilities in snapshots
    assert records[0].control_credential_digest == owner_digest


def test_scheduler_eligibility_smallest_tier_and_least_load(
    store_factory, capabilities
):
    store = store_factory()
    fast = replace(capabilities, max_concurrency=4)
    full = replace(
        fast, active_context_tier="64k-full", maximum_total_context_tokens=65536
    )
    store.register("full", full, digest("full"))
    store.register("fast-a", fast, digest("fast-a"))
    store.register("fast-b", fast, digest("fast-b"))
    assert reserve(store).selected_node_id == "fast-a"
    assert reserve(store, "request-b").selected_node_id == "fast-b"


def test_scheduler_round_robin_registration_order(store_factory, capabilities):
    store = store_factory()
    for node in ("node-b", "node-a", "node-c"):
        store.register(node, replace(capabilities, max_concurrency=4), digest(node))
    assert [reserve(store, f"request-{i}").selected_node_id for i in range(3)] == [
        "node-b",
        "node-a",
        "node-c",
    ]


def test_ineligible_and_full_nodes_are_skipped(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock, reservation_ttl_seconds=2)
    store.register("unhealthy", capabilities, digest("unhealthy"))
    store.set_scheduler_state(
        "unhealthy", digest("unhealthy"), SchedulerNodeState(healthy=False)
    )
    store.register("draining", capabilities, digest("draining"))
    store.set_scheduler_state(
        "draining", digest("draining"), SchedulerNodeState(draining=True)
    )
    store.register(
        "incompatible",
        replace(capabilities, supported_model_ids=("other",)),
        digest("incompatible"),
    )
    store.register("full", replace(capabilities, max_concurrency=1), digest("full"))
    assert reserve(store).selected_node_id == "full"
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")
    clock.value += 2
    assert reserve(store, "request-b").selected_node_id == "full"
    clock.value += 30
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-c", request_deadline_epoch=clock.value + 100)


def test_selection_idempotency_conflict_and_digest_only_token(
    store_factory, capabilities
):
    store = store_factory()
    store.register("node-a", capabilities, digest("owner"))
    first = reserve(store)
    retried = reserve(store)
    assert first.created and first.reservation_token is not None
    assert not retried.created and retried.reservation_token is None
    records = store.list_reservations()
    assert len(records) == 1
    assert first.reservation_token not in repr(first)
    assert first.reservation_token not in repr(records)
    assert (
        records[0].token_digest
        == hashlib.sha256(first.reservation_token.encode("ascii")).hexdigest()
    )
    with pytest.raises(
        RelayStateConflict, match="^request identity conflict$"
    ) as error:
        reserve(store, requested_context_tier="64k-full")
    assert first.reservation_token not in str(error.value)
    assert len(store.list_reservations()) == 1


def test_enqueue_consumes_once_and_identical_retry_is_safe(store_factory, capabilities):
    store = store_factory()
    store.register("node-a", capabilities, digest("owner"))
    selection = reserve(store)
    result = enqueue(store, selection)
    retry = enqueue(store, selection)
    assert result.created and not retry.created and result.sequence == retry.sequence
    assert store.list_reservations() == ()
    assert len(store.queued_requests("node-a")) == 1
    with pytest.raises(RelayStateInvalidReservation):
        enqueue(store, selection, reservation_token="0" * 64)
    assert len(store.queued_requests("node-a")) == 1
    with pytest.raises(RelayStateConflict):
        enqueue(store, selection, envelope=envelope("different"))
    assert len(store.queued_requests("node-a")) == 1


def test_selection_retry_after_enqueue_reports_explicit_queued_state(
    store_factory, capabilities
):
    clock = EpochClock()
    store = store_factory(clock=clock)
    store.register("node-a", capabilities, digest("owner"))
    selection = reserve(store)
    clock.value += 1
    enqueue(store, selection)

    retry = reserve(store)

    assert retry.state == "queued"
    assert retry.reservation_expires_at_epoch is None
    assert retry.reservation_token is None
    assert not retry.created


@pytest.mark.parametrize("teardown", ["unregister", "expire"])
def test_node_teardown_removes_queued_work(store_factory, capabilities, teardown):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=1)
    store.register("node-a", capabilities, digest("owner"))
    enqueue(store, reserve(store))

    if teardown == "unregister":
        assert store.unregister("node-a", digest("owner"))
    else:
        clock.value += 1
        assert store.expire()

    assert store.queued_requests("node-a") == ()
    store.register("node-a", capabilities, digest("replacement"))
    assert reserve(store).selected_node_id == "node-a"


def test_wrong_cross_identity_cross_node_expired_and_reused_tokens_fail(
    store_factory, capabilities
):
    clock = EpochClock()
    store = store_factory(clock=clock, reservation_ttl_seconds=1)
    for node in ("node-a", "node-b"):
        store.register(node, capabilities, digest(node))
    selection = reserve(store)
    for overrides in (
        {"reservation_token": "0" * 64},
        {"request_id": "other"},
        {"selected_node_id": "node-b"},
    ):
        with pytest.raises(RelayStateInvalidReservation):
            enqueue(store, selection, **overrides)
    enqueue(store, selection)
    other = reserve(store, "request-b")
    with pytest.raises(RelayStateInvalidReservation):
        enqueue(
            store,
            other,
            request_id="request-b",
            reservation_token=selection.reservation_token,
        )
    clock.value += 1
    with pytest.raises(RelayStateInvalidReservation):
        enqueue(store, other, request_id="request-b")


def test_inclusive_deadline_expiry_releases_capacity_once(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock)
    store.register("node-a", replace(capabilities, max_concurrency=1), digest("owner"))
    selection = reserve(store, request_deadline_epoch=clock.value + 1)
    clock.value = selection.request_deadline_epoch
    assert store.list_reservations() == ()
    assert (
        reserve(
            store, "replacement", request_deadline_epoch=clock.value + 10
        ).selected_node_id
        == "node-a"
    )


def test_queue_and_reservations_count_toward_all_bounds(store_factory, capabilities):
    store = store_factory(max_queue_depth_per_node=1, max_reservations_per_node=1)
    store.register("node-a", replace(capabilities, max_concurrency=3), digest("owner"))
    first = reserve(store)
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")
    enqueue(store, first)
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")


def test_strict_envelope_allowlist_and_utf8_byte_bound(store_factory, capabilities):
    with pytest.raises(TypeError):
        EncryptedRequestEnvelope(
            protocol="tokenplace_api_v1_relay_e2ee",
            version=1,
            ciphertext="c",
            cipherkey="k",
            iv="i",
            messages=[],
        )
    store = store_factory(max_envelope_bytes=110)
    store.register("node-a", capabilities, digest("owner"))
    selection = reserve(store)
    with pytest.raises(RelayStateStoreError, match="byte bound"):
        enqueue(store, selection, envelope=envelope("é" * 100))
    assert store.list_reservations() and store.queued_requests("node-a") == ()


def test_concurrent_identity_operations_are_atomic(store_factory, capabilities):
    store = store_factory(max_queue_depth_per_node=4)
    store.register("node-a", replace(capabilities, max_concurrency=4), digest("owner"))
    with ThreadPoolExecutor(max_workers=8) as executor:
        selections = list(executor.map(lambda _: reserve(store), range(8)))
    assert sum(item.created for item in selections) == 1
    token_result = next(item for item in selections if item.created)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: enqueue(store, token_result), range(8)))
    assert sum(item.created for item in results) == 1
    assert len(store.queued_requests("node-a")) == 1


def test_concurrent_different_identities_never_overreserve(store_factory, capabilities):
    store = store_factory(max_queue_depth_per_node=2)
    store.register("node-a", replace(capabilities, max_concurrency=2), digest("owner"))

    def attempt(index):
        try:
            return reserve(store, f"request-{index}")
        except RelayStateNoCapacity:
            return None

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(attempt, range(16)))
    assert sum(item is not None for item in results) == 2
    assert len(store.list_reservations()) == 2


def test_scheduler_reads_are_immutable_defensive_copies(store_factory, capabilities):
    store = store_factory()
    store.register("node-a", capabilities, digest("owner"))
    selection = reserve(store)
    record = store.list_reservations()[0]
    with pytest.raises(FrozenInstanceError):
        record.selected_node_id = "changed"
    enqueue(store, selection)
    queued = store.queued_requests("node-a")
    with pytest.raises(FrozenInstanceError):
        queued[0].envelope.ciphertext = "changed"
    assert store.queued_requests("node-a")[0].envelope.ciphertext == "ciphertext"


def test_queue_preserves_only_exact_safe_routing_identity(store_factory, capabilities):
    store = store_factory()
    store.register("node-a", capabilities, digest("owner"))
    selection = reserve(
        store, request_id="Request-Exact", client_public_key="Key-Exact"
    )
    enqueue(
        store,
        selection,
        request_id="Request-Exact",
        client_public_key="Key-Exact",
    )

    queued = store.queued_requests("node-a")[0]
    assert queued.client_public_key == "Key-Exact"
    assert queued.request_id == "Request-Exact"
    assert queued.client_identity_digest == digest_with_domain("Key-Exact", b"client\0")
    assert queued.request_identity_digest == digest_with_domain(
        "Request-Exact", b"request\0"
    )
    assert {field.name for field in fields(type(queued))}.isdisjoint(
        {"prompt", "messages", "credentials", "url", "headers", "payload"}
    )
    assert selection.reservation_token not in repr(queued)


def test_reservation_expiry_preserves_fairness_cursor(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock, reservation_ttl_seconds=1)
    for node_id in ("node-a", "node-b"):
        store.register(node_id, capabilities, digest(node_id))
    assert reserve(store).selected_node_id == "node-a"
    clock.value += 1
    assert reserve(store, "request-b").selected_node_id == "node-b"


def test_fingerprint_bound_evicts_inactive_lru_but_not_active(
    store_factory, capabilities
):
    clock = EpochClock()
    store = store_factory(
        clock=clock, max_scheduler_fingerprints=2, reservation_ttl_seconds=1
    )
    store.register(
        "node-a",
        replace(
            capabilities,
            supported_model_ids=("model-a", "model-b", "model-c"),
            max_concurrency=3,
        ),
        digest("owner"),
    )
    reserve(store, "a", requested_model_id="model-a")
    reserve(store, "b", requested_model_id="model-b")
    before = store.list_reservations()
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "c", requested_model_id="model-c")
    assert store.list_reservations() == before
    clock.value += 1
    assert reserve(store, "c", requested_model_id="model-c").created


def test_global_lifecycle_and_queue_bounds_reject_without_mutation(
    store_factory, capabilities
):
    store = store_factory(max_request_lifecycles=1, max_queued_requests=1)
    store.register("node-a", replace(capabilities, max_concurrency=3), digest("owner"))
    first = reserve(store)
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")
    assert store.list_reservations()[0].request_identity_digest == digest_with_domain(
        "request-a", b"request\0"
    )
    enqueue(store, first)
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")
    assert len(store.queued_requests("node-a")) == 1


def test_concurrent_per_client_queued_boundary(store_factory, capabilities):
    store = store_factory(
        max_reservations_per_client=8, max_queued_requests_per_client=2
    )
    store.register("node-a", replace(capabilities, max_concurrency=8), digest("owner"))
    selections = [reserve(store, f"request-{index}") for index in range(8)]

    def attempt(selection):
        try:
            return enqueue(
                store, selection, request_id=f"request-{selections.index(selection)}"
            )
        except RelayStateNoCapacity:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, selections))
    assert sum(result is not None for result in results) == 2
    assert len(store.queued_requests("node-a")) == 2
    assert len(store.list_reservations()) == 6


def test_context_requirement_and_claimed_work_count_toward_capacity(
    store_factory, capabilities
):
    store = store_factory()
    store.register("fast", capabilities, digest("fast"))
    full = replace(
        capabilities, active_context_tier="64k-full", maximum_total_context_tokens=65536
    )
    store.register("full", full, digest("full"))
    assert reserve(store, requested_context_tier="64k-full").selected_node_id == "full"
    store.set_scheduler_state(
        "fast", digest("fast"), SchedulerNodeState(claimed_work=2)
    )
    store.set_scheduler_state(
        "full", digest("full"), SchedulerNodeState(claimed_work=1)
    )
    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")


def test_active_context_tier_must_satisfy_request(store_factory, capabilities):
    store = store_factory()
    oversized_fast = replace(capabilities, maximum_total_context_tokens=65536)
    full = replace(
        capabilities,
        active_context_tier="64k-full",
        maximum_total_context_tokens=65536,
    )
    store.register("fast", oversized_fast, digest("fast"))
    store.register("full", full, digest("full"))

    assert reserve(store, requested_context_tier="64k-full").selected_node_id == "full"


def test_queued_work_counts_toward_per_client_bound(store_factory, capabilities):
    store = store_factory(max_reservations_per_client=1)
    store.register("node-a", replace(capabilities, max_concurrency=2), digest("owner"))
    enqueue(store, reserve(store))

    with pytest.raises(RelayStateNoCapacity):
        reserve(store, "request-b")
    assert (
        reserve(store, "request-c", client_public_key="other-client").selected_node_id
        == "node-a"
    )


def test_inactive_fairness_fingerprints_are_reclaimed(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock, max_scheduler_fingerprints=1)
    store.register(
        "node-a",
        replace(capabilities, supported_model_ids=("model-a", "model-b")),
        digest("owner"),
    )
    first = reserve(
        store,
        requested_model_id="model-a",
        request_deadline_epoch=clock.value + 1,
    )
    enqueue(store, first)
    clock.value += 1

    assert (
        reserve(
            store,
            "request-b",
            requested_model_id="model-b",
            request_deadline_epoch=clock.value + 10,
        ).selected_node_id
        == "node-a"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reservation_ttl_seconds", 301),
        ("max_request_ttl_seconds", True),
        ("max_reservations", 0),
        ("max_reservations_per_client", 0),
        ("max_reservations_per_node", 0),
        ("max_queue_depth_per_node", 0),
        ("max_request_lifecycles", 0),
        ("max_queued_requests", 0),
        ("max_queued_requests_per_client", 0),
        ("max_identity_bytes", 0),
        ("max_model_id_bytes", 0),
        ("max_scheduler_fingerprints", 0),
        ("max_envelope_bytes", 0),
        ("max_response_envelope_bytes", 0),
        ("max_responses", 0),
        ("max_responses_per_client", 0),
        ("response_replay_ttl_seconds", 0),
        ("max_terminal_records", 0),
        ("max_terminal_records_per_client", 0),
        ("terminal_retention_seconds", 0),
    ],
)
def test_scheduler_configuration_bounds_are_explicit(field, value):
    with pytest.raises(RelayStateStoreError):
        RelayStateStoreConfig(namespace="test", **{field: value})


def test_terminal_retention_must_cover_response_replay_retention():
    with pytest.raises(RelayStateStoreError, match="must cover"):
        RelayStateStoreConfig(
            namespace="test",
            response_replay_ttl_seconds=10,
            terminal_retention_seconds=9,
        )
