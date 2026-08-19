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
from relay_state_store import RelayStateNoEligibleNode
from relay_state_store import RelayStateReservationInvalid
from relay_state_store import RelayStateStore
from relay_state_store import RelayStateStoreConfig
from relay_state_store import RelayStateStoreError

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


def scheduler_capabilities(*, models=("model-a",), tier="8k-fast", concurrency=2):
    return ComputeNodeCapabilities(
        supported_model_ids=models,
        active_context_tier=tier,
        maximum_total_context_tokens=65536 if tier == "64k-full" else 8192,
        default_output_token_reservation=1,
        maximum_output_tokens=1,
        max_concurrency=concurrency,
    )


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
        "model_id": "model-a",
        "context_tier": "8k-fast",
        "deadline_epoch": 1_700_000_100.0,
    }
    values.update(overrides)
    return store.select_and_reserve(**values)


def test_selection_uses_compatibility_smallest_tier_least_load_and_fairness(
    store_factory,
):
    store = store_factory()
    for node, caps in (
        (
            "full",
            scheduler_capabilities(models=("model-a", "model-b"), tier="64k-full"),
        ),
        ("fast-a", scheduler_capabilities()),
        ("fast-b", scheduler_capabilities()),
    ):
        store.register(node, caps, digest(node))

    first = reserve(store, "one")
    second = reserve(store, "two")
    third = reserve(store, "three")
    full = reserve(store, "full", context_tier="64k-full")

    assert [
        first.reservation.node_id,
        second.reservation.node_id,
        third.reservation.node_id,
    ] == ["fast-a", "fast-b", "fast-a"]
    assert full.reservation.node_id == "full"
    with pytest.raises(RelayStateNoEligibleNode):
        reserve(store, "missing", model_id="missing")


def test_unhealthy_draining_expired_incompatible_and_full_nodes_are_ineligible(
    store_factory,
):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=5, max_queue_depth_per_node=1)
    store.register("expired", scheduler_capabilities(concurrency=1), digest("expired"))
    clock.value += 5
    for node in ("unhealthy", "draining", "full"):
        store.register(node, scheduler_capabilities(concurrency=1), digest(node))
    store.set_scheduler_state(
        "unhealthy", digest("unhealthy"), healthy=False, draining=False
    )
    store.set_scheduler_state(
        "draining", digest("draining"), healthy=True, draining=True
    )
    reserve(
        store, "occupy", client_public_key="other", deadline_epoch=clock.value + 100
    )
    with pytest.raises(RelayStateNoEligibleNode):
        reserve(store, deadline_epoch=clock.value + 100)
    assert store.get("expired") is None


def test_selection_retry_is_idempotent_conflicts_and_does_not_disclose_token(
    store_factory,
):
    store = store_factory()
    store.register("node-a", scheduler_capabilities(), digest("node-a"))
    first = reserve(store)
    repeated = reserve(store)
    assert repeated.created is False and repeated.reservation_token is None
    assert repeated.reservation == first.reservation
    assert len(store.reservations()) == 1
    with pytest.raises(RelayStateConflict, match="different parameters"):
        reserve(store, model_id="model-b")
    raw_token = first.reservation_token
    assert raw_token and len(raw_token) == 64
    assert raw_token not in repr(first)
    assert raw_token not in repr(store.reservations())
    assert raw_token not in repr(vars(store))
    assert (
        first.reservation.token_digest == hashlib.sha256(raw_token.encode()).hexdigest()
    )


def test_reservation_expiry_is_inclusive_releases_once_without_rewinding_cursor(
    store_factory,
):
    clock = EpochClock()
    store = store_factory(clock=clock, reservation_ttl_seconds=5)
    store.register("node-a", scheduler_capabilities(concurrency=1), digest("a"))
    store.register("node-b", scheduler_capabilities(concurrency=1), digest("b"))
    first = reserve(store, deadline_epoch=clock.value + 100)
    clock.value = first.reservation.expires_at_epoch
    assert store.reservations() == ()
    assert (
        reserve(store, "next", deadline_epoch=clock.value + 100).reservation.node_id
        == "node-b"
    )


def test_enqueue_consumes_once_and_identical_retry_is_safe(store_factory):
    store = store_factory()
    store.register("node-a", scheduler_capabilities(), digest("node-a"))
    selected = reserve(store)
    args = dict(
        client_public_key="client-key",
        request_id="request-a",
        reservation_token=selected.reservation_token,
        node_id="node-a",
        model_id="model-a",
        context_tier="8k-fast",
        deadline_epoch=1_700_000_100.0,
        envelope=envelope(),
    )
    first = store.enqueue_encrypted_request(**args)
    repeated = store.enqueue_encrypted_request(**args)
    assert first.created is True and repeated.created is False
    assert first.request == repeated.request and len(store.queued()) == 1
    assert store.reservations() == ()


def test_enqueue_conflict_wrong_cross_identity_cross_node_and_reused_tokens_fail_closed(
    store_factory,
):
    store = store_factory()
    store.register("node-a", scheduler_capabilities(), digest("node-a"))
    store.register("node-b", scheduler_capabilities(), digest("node-b"))
    selected = reserve(store)
    base = dict(
        client_public_key="client-key",
        request_id="request-a",
        reservation_token=selected.reservation_token,
        node_id=selected.reservation.node_id,
        model_id="model-a",
        context_tier="8k-fast",
        deadline_epoch=1_700_000_100.0,
        envelope=envelope(),
    )
    for change in (
        {"reservation_token": "0" * 64},
        {"client_public_key": "other-client"},
        {"request_id": "other-request"},
        {"node_id": "node-b"},
    ):
        with pytest.raises(RelayStateReservationInvalid):
            store.enqueue_encrypted_request(**{**base, **change})
    assert store.queued() == () and len(store.reservations()) == 1
    store.enqueue_encrypted_request(**base)
    with pytest.raises(RelayStateConflict):
        store.enqueue_encrypted_request(**{**base, "envelope": envelope("different")})
    with pytest.raises(RelayStateReservationInvalid):
        store.enqueue_encrypted_request(**{**base, "request_id": "new-request"})
    assert len(store.queued()) == 1


def test_expired_token_and_deadline_fail_at_inclusive_epoch_boundary(store_factory):
    clock = EpochClock()
    store = store_factory(clock=clock, reservation_ttl_seconds=5)
    store.register("node-a", scheduler_capabilities(), digest("node-a"))
    selected = reserve(store, deadline_epoch=clock.value + 5)
    clock.value += 5
    with pytest.raises((RelayStateStoreError, RelayStateReservationInvalid)):
        store.enqueue_encrypted_request(
            "client-key",
            "request-a",
            selected.reservation_token,
            "node-a",
            "model-a",
            "8k-fast",
            clock.value,
            envelope(),
        )
    assert store.reservations() == () and store.queued() == ()


def test_strict_envelope_allowlist_immutability_and_byte_bound(store_factory):
    store = store_factory(max_envelope_bytes=64)
    store.register("node-a", scheduler_capabilities(), digest("node-a"))
    with pytest.raises(TypeError):
        EncryptedRequestEnvelope(
            protocol="tokenplace_api_v1_relay_e2ee",
            version=1,
            ciphertext="c",
            cipherkey="k",
            iv="i",
            messages=[{"content": "plaintext"}],  # type: ignore[call-arg]
        )
    selected = reserve(store)
    with pytest.raises(RelayStateCapacityExceeded):
        store.enqueue_encrypted_request(
            "client-key",
            "request-a",
            selected.reservation_token,
            "node-a",
            "model-a",
            "8k-fast",
            1_700_000_100.0,
            envelope("x" * 100),
        )
    assert store.queued() == ()


def test_concurrent_idempotency_and_admission_never_overreserve(store_factory):
    store = store_factory(max_queue_depth_per_node=2, max_reservations_per_node=2)
    store.register("node-a", scheduler_capabilities(concurrency=2), digest("node-a"))
    with ThreadPoolExecutor(max_workers=16) as pool:
        same = list(pool.map(lambda _index: reserve(store), range(20)))
    assert sum(item.created for item in same) == 1 and len(store.reservations()) == 1
    token = next(item.reservation_token for item in same if item.reservation_token)
    enqueue_args = (
        "client-key",
        "request-a",
        token,
        "node-a",
        "model-a",
        "8k-fast",
        1_700_000_100.0,
        envelope(),
    )
    with ThreadPoolExecutor(max_workers=16) as pool:
        enqueued = list(
            pool.map(
                lambda _index: store.enqueue_encrypted_request(*enqueue_args), range(20)
            )
        )
    assert sum(item.created for item in enqueued) == 1 and len(store.queued()) == 1

    def attempt(index):
        try:
            return reserve(store, f"other-{index}", client_public_key=f"client-{index}")
        except RelayStateNoEligibleNode:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(attempt, range(50)))
    assert len(store.queued()) + len(store.reservations()) <= 2
