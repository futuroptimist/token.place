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
from relay_state_store import EncryptedRequestEnvelope
from relay_state_store import InMemoryRelayStateStore
from relay_state_store import RelayStateCapacityExceeded
from relay_state_store import RelayStateCredentialMismatch
from relay_state_store import RelayStateConflict
from relay_state_store import RelayStateInvalidReservation
from relay_state_store import RelayStateNoCapacity
from relay_state_store import RelayStateStore
from relay_state_store import RelayStateStoreConfig
from relay_state_store import RelayStateStoreError
from relay_state_store import SchedulerNodeState

# isort: on


class EpochClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture(params=["memory"])
def store_factory(request):
    """Factory seam to parameterize over real Valkey in a future slice."""

    def make(*, clock=None, **config_overrides):
        assert request.param == "memory"
        config = RelayStateStoreConfig(
            namespace="testing.cluster-a", **config_overrides
        )
        return InMemoryRelayStateStore(config, epoch_time=clock or EpochClock())

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
    ],
)
def test_scheduler_configuration_bounds_are_explicit(field, value):
    with pytest.raises(RelayStateStoreError):
        RelayStateStoreConfig(namespace="test", **{field: value})
