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
from relay_state_store import RelayStateConflict
from relay_state_store import RelayStateCapacityExceeded
from relay_state_store import RelayStateCredentialMismatch
from relay_state_store import RelayStateNoEligibleNode
from relay_state_store import RelayStateReservationRejected
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


def scheduler_capabilities(*, tier="8k-fast", concurrency=2, models=("model",)):
    return ComputeNodeCapabilities(
        supported_model_ids=models,
        active_context_tier=tier,
        maximum_total_context_tokens=(8192 if tier == "8k-fast" else 65536),
        default_output_token_reservation=1,
        maximum_output_tokens=1,
        max_concurrency=concurrency,
    )


def reserve(store, client="client", request="request", **overrides):
    values = dict(
        requested_model="model",
        context_tier="8k-fast",
        request_deadline_epoch=1_700_000_060.0,
    )
    values.update(overrides)
    return store.select_and_reserve(client, request, **values)


def envelope(ciphertext="ciphertext"):
    return EncryptedRequestEnvelope(
        protocol="tokenplace_api_v1_relay_e2ee",
        version=1,
        ciphertext=ciphertext,
        cipherkey="cipherkey",
        iv="iv",
    )


def enqueue(store, reservation, client="client", request="request", **overrides):
    values = dict(
        reservation_token=reservation.reservation_token,
        selected_node_id=reservation.selected_node_id,
        requested_model="model",
        context_tier="8k-fast",
        request_deadline_epoch=1_700_000_060.0,
        envelope=envelope(),
    )
    values.update(overrides)
    return store.consume_reservation_and_enqueue(client, request, **values)


def test_scheduler_eligibility_smallest_tier_health_and_capacity(store_factory):
    store = store_factory(max_queue_depth_per_node=2)
    for node, caps in (
        ("small", scheduler_capabilities()),
        ("large", scheduler_capabilities(tier="64k-full")),
        ("wrong", scheduler_capabilities(models=("other",))),
        ("full", scheduler_capabilities(concurrency=1)),
        ("draining", scheduler_capabilities()),
        ("unhealthy", scheduler_capabilities()),
    ):
        store.register(node, caps, digest(node))
    store.set_scheduler_status("full", healthy=True, draining=False, claimed_work=1)
    store.set_scheduler_status("draining", healthy=True, draining=True)
    store.set_scheduler_status("unhealthy", healthy=False, draining=False)

    assert reserve(store).selected_node_id == "small"
    assert (
        reserve(
            store, request="large-request", context_tier="64k-full"
        ).selected_node_id
        == "large"
    )


def test_expired_and_incompatible_nodes_are_ineligible(store_factory):
    clock = EpochClock()
    store = store_factory(clock=clock, lease_ttl_seconds=1)
    store.register("node", scheduler_capabilities(models=("other",)), digest("node"))
    with pytest.raises(RelayStateNoEligibleNode):
        reserve(store)
    clock.value += 1
    with pytest.raises(RelayStateNoEligibleNode):
        reserve(store, request="second")


def test_least_load_then_registration_order_round_robin(store_factory):
    store = store_factory()
    for node in ("a", "b", "c"):
        store.register(node, scheduler_capabilities(concurrency=4), digest(node))
    store.set_scheduler_status("a", healthy=True, draining=False, claimed_work=1)
    assert reserve(store, request="one").selected_node_id == "b"
    assert reserve(store, request="two").selected_node_id == "c"
    # All three now have load one, and the cursor continues after c.
    assert reserve(store, request="three").selected_node_id == "a"


def test_selection_idempotency_conflict_and_digest_only_token(store_factory):
    store = store_factory()
    store.register("node", scheduler_capabilities(), digest("node"))
    first = reserve(store)
    raw = first.reservation_token
    assert raw and len(raw) >= 43
    repeated = reserve(store)
    assert repeated.created is False and repeated.reservation_token is None
    assert repeated.selected_node_id == first.selected_node_id
    assert raw not in repr(first)
    assert raw not in repr(vars(store))
    with pytest.raises(RelayStateConflict) as caught:
        reserve(store, requested_model="different")
    assert raw not in str(caught.value)


def test_reservations_count_toward_concurrency_and_bounds(store_factory):
    store = store_factory(max_reservations_per_node=1, max_queue_depth_per_node=1)
    store.register("node", scheduler_capabilities(concurrency=2), digest("node"))
    reserve(store)
    with pytest.raises(RelayStateNoEligibleNode):
        reserve(store, client="other", request="other")


def test_inclusive_reservation_expiry_releases_capacity_once_without_cursor_rewind(
    store_factory,
):
    clock = EpochClock()
    store = store_factory(
        clock=clock, reservation_ttl_seconds=5, max_reservations_per_node=1
    )
    for node in ("a", "b"):
        store.register(node, scheduler_capabilities(), digest(node))
    first = reserve(store)
    clock.value = first.reservation_expires_at_epoch
    second = reserve(store, request="second")
    assert second.selected_node_id != first.selected_node_id
    with pytest.raises(RelayStateReservationRejected):
        enqueue(store, first)


def test_enqueue_consumes_once_and_identical_retry_is_safe(store_factory):
    store = store_factory()
    store.register("node", scheduler_capabilities(), digest("node"))
    reservation = reserve(store)
    first = enqueue(store, reservation)
    repeated = enqueue(store, reservation)
    assert first.created is True and repeated.created is False
    assert repeated.request == first.request
    assert store.list_queued("node") == (first.request,)
    with pytest.raises(RelayStateConflict):
        enqueue(store, reservation, envelope=envelope("different"))
    assert store.list_queued("node") == (first.request,)


@pytest.mark.parametrize("mutation", ["wrong-token", "identity", "node"])
def test_invalid_cross_identity_and_cross_node_tokens_fail_closed(
    store_factory, mutation
):
    store = store_factory()
    for node in ("node", "other"):
        store.register(node, scheduler_capabilities(), digest(node))
    reservation = reserve(store)
    kwargs = {}
    client = "client"
    if mutation == "wrong-token":
        kwargs["reservation_token"] = "wrong"
    elif mutation == "identity":
        client = "other-client"
    else:
        kwargs["selected_node_id"] = "other"
    with pytest.raises(RelayStateReservationRejected):
        enqueue(store, reservation, client=client, **kwargs)
    assert store.list_queued("node") == ()


def test_deadline_expiry_is_inclusive(store_factory):
    clock = EpochClock()
    store = store_factory(clock=clock)
    store.register("node", scheduler_capabilities(), digest("node"))
    reservation = reserve(store, request_deadline_epoch=clock.value + 5)
    clock.value += 5
    with pytest.raises(RelayStateReservationRejected):
        enqueue(store, reservation, request_deadline_epoch=clock.value)
    assert store.list_queued("node") == ()


def test_strict_envelope_allowlist_and_byte_bound(store_factory):
    with pytest.raises(TypeError):
        EncryptedRequestEnvelope(
            protocol="tokenplace_api_v1_relay_e2ee",
            version=1,
            ciphertext="c",
            cipherkey="k",
            iv="i",
            messages=[],  # type: ignore[call-arg]
        )
    store = store_factory(max_envelope_bytes=150)
    store.register("node", scheduler_capabilities(), digest("node"))
    reservation = reserve(store)
    with pytest.raises(RelayStateCapacityExceeded):
        enqueue(store, reservation, envelope=envelope("x" * 200))
    assert store.list_queued("node") == ()


def test_concurrent_selection_and_enqueue_respect_identity_and_capacity(store_factory):
    store = store_factory(max_queue_depth_per_node=8)
    store.register("node", scheduler_capabilities(concurrency=8), digest("node"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        same = list(pool.map(lambda _: reserve(store), range(32)))
    assert sum(item.created for item in same) == 1
    token = next(item.reservation_token for item in same if item.created)
    initial = replace(same[0], reservation_token=token)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: enqueue(store, initial), range(32)))
    assert sum(item.created for item in results) == 1
    assert len(store.list_queued("node")) == 1

    def admit(index):
        try:
            return reserve(store, client=f"c{index}", request=f"r{index}")
        except RelayStateNoEligibleNode:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        admitted = list(pool.map(admit, range(32)))
    assert sum(item is not None for item in admitted) == 7


def test_scheduler_records_and_reads_are_immutable(store_factory):
    store = store_factory()
    store.register("node", scheduler_capabilities(), digest("node"))
    result = enqueue(store, reserve(store))
    with pytest.raises(FrozenInstanceError):
        result.request.selected_node_id = "changed"  # type: ignore[misc]
    assert store.list_queued("node")[0].selected_node_id == "node"
