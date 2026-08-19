"""Backend contract for registration/lease RelayStateStore implementations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from relay_state_store import (
    MAX_MODEL_IDS_PER_NODE,
    ComputeNodeCapabilities,
    InMemoryRelayStateStore,
    RegistrationCapacityError,
    RegistrationConflictError,
    RelayStateStore,
    RelayStateStoreError,
)


class EpochClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def clock() -> EpochClock:
    return EpochClock()


@pytest.fixture
def store_factory(clock):
    """Factory boundary future Valkey contract tests will implement."""

    def factory(**overrides) -> RelayStateStore:
        options = {
            "namespace": "test/cluster-a",
            "lease_ttl_seconds": 30,
            "epoch_time": clock,
        }
        options.update(overrides)
        return InMemoryRelayStateStore(**options)

    return factory


@pytest.fixture
def capabilities() -> ComputeNodeCapabilities:
    return ComputeNodeCapabilities(
        supported_model_ids=("model-a",),
        active_context_tier="8k-fast",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=1024,
        maximum_output_tokens=2048,
        max_concurrency=2,
        backend_class="cpu",
    )


def digest(seed: int) -> str:
    return f"{seed:064x}"


def test_register_lookup_and_deterministic_listing(store_factory, capabilities, clock):
    store = store_factory()
    second = store.register("node-b", digest(2), capabilities)
    first = store.register("node-a", digest(1), capabilities)

    assert first.registered_at_epoch == clock.value
    assert first.lease_expires_at_epoch == clock.value + store.lease_ttl_seconds
    assert first.schema_version == store.schema_version == 1
    assert store.namespace == "test/cluster-a"
    assert store.get("node-b") == second
    assert tuple(record.node_id for record in store.list()) == ("node-a", "node-b")


def test_duplicate_registration_is_renewal_but_cannot_change_owner(
    store_factory, capabilities, clock
):
    store = store_factory()
    original = store.register("node-a", digest(1), capabilities)
    clock.value += 5
    updated_capabilities = replace(capabilities, max_concurrency=4)
    duplicate = store.register("node-a", digest(1), updated_capabilities)

    assert duplicate.registered_at_epoch == original.registered_at_epoch
    assert duplicate.lease_expires_at_epoch == clock.value + 30
    assert duplicate.capabilities.max_concurrency == 4
    with pytest.raises(RegistrationConflictError):
        store.register("node-a", digest(2), capabilities)


def test_renew_is_idempotent_and_may_change_valid_capabilities(
    store_factory, capabilities, clock
):
    store = store_factory()
    original = store.register("node-a", digest(1), capabilities)
    renewed = store.renew("node-a", digest(1))
    assert renewed == original

    clock.value += 10
    changed = store.renew(
        "node-a", digest(1), replace(capabilities, backend_class="cuda")
    )
    assert changed is not None
    assert changed.capabilities.backend_class == "cuda"
    assert changed.lease_expires_at_epoch == clock.value + 30
    assert store.renew("unknown", digest(9)) is None
    with pytest.raises(RegistrationConflictError):
        store.renew("node-a", digest(2))


def test_expiry_boundary_uses_injected_epoch_clock(store_factory, capabilities, clock):
    store = store_factory(lease_ttl_seconds=5)
    store.register("node-a", digest(1), capabilities)
    clock.value += 4.999
    assert store.expire() == ()
    clock.value += 0.001
    assert store.get("node-a") is None
    assert store.expire() == ()


def test_expire_returns_stable_node_ids(store_factory, capabilities, clock):
    store = store_factory(lease_ttl_seconds=5)
    store.register("node-b", digest(2), capabilities)
    store.register("node-a", digest(1), capabilities)
    clock.value += 5
    assert store.expire() == ("node-a", "node-b")


def test_unregister_is_idempotent_and_optionally_owner_checked(
    store_factory, capabilities
):
    store = store_factory()
    store.register("node-a", digest(1), capabilities)
    with pytest.raises(RegistrationConflictError):
        store.unregister("node-a", digest(2))
    assert store.unregister("node-a", digest(1)) is True
    assert store.unregister("node-a", digest(1)) is False
    assert store.unregister("unknown") is False


@pytest.mark.parametrize("namespace", ["", " space", "x" * 129, "unsafe?cluster"])
def test_namespace_is_bounded_and_validated(namespace, clock):
    with pytest.raises(RelayStateStoreError):
        InMemoryRelayStateStore(namespace=namespace, epoch_time=clock)


def test_schema_ttl_and_capacity_configuration_are_explicit(clock):
    with pytest.raises(RelayStateStoreError, match="schema"):
        InMemoryRelayStateStore(namespace="test", schema_version=2, epoch_time=clock)
    for ttl in (0, 3601, float("inf")):
        with pytest.raises(RelayStateStoreError, match="lease_ttl"):
            InMemoryRelayStateStore(
                namespace="test", lease_ttl_seconds=ttl, epoch_time=clock
            )
    with pytest.raises(RelayStateStoreError, match="max_registrations"):
        InMemoryRelayStateStore(namespace="test", max_registrations=0, epoch_time=clock)


def test_registration_capacity_is_atomic_and_expired_slots_are_reused(
    store_factory, capabilities, clock
):
    store = store_factory(max_registrations=1, lease_ttl_seconds=1)
    store.register("node-a", digest(1), capabilities)
    with pytest.raises(RegistrationCapacityError):
        store.register("node-b", digest(2), capabilities)
    clock.value += 1
    store.register("node-b", digest(2), capabilities)
    assert tuple(record.node_id for record in store.list()) == ("node-b",)


def test_records_are_immutable_and_reads_cannot_mutate_store(
    store_factory, capabilities
):
    store = store_factory()
    record = store.register("node-a", digest(1), capabilities)
    with pytest.raises(FrozenInstanceError):
        record.node_id = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.capabilities.max_concurrency = 9  # type: ignore[misc]
    listed = store.list()
    assert isinstance(listed, tuple)
    assert store.get("node-a").node_id == "node-a"  # type: ignore[union-attr]


def test_contract_rejects_raw_credentials_and_arbitrary_application_payloads(
    store_factory, capabilities
):
    store = store_factory()
    with pytest.raises(RelayStateStoreError, match="digest"):
        store.register("node-a", "raw-control-credential", capabilities)
    with pytest.raises(TypeError):
        ComputeNodeCapabilities(
            supported_model_ids=("model-a",),
            active_context_tier="8k-fast",
            maximum_total_context_tokens=8192,
            default_output_token_reservation=1024,
            maximum_output_tokens=2048,
            max_concurrency=2,
            messages=({"role": "user", "content": "secret"},),  # type: ignore[call-arg]
        )
    with pytest.raises(RelayStateStoreError, match="ComputeNodeCapabilities"):
        store.register("node-a", digest(1), {"prompt": "secret"})  # type: ignore[arg-type]


def test_capability_bounds_match_api_v1_scheduler_surface(capabilities):
    with pytest.raises(RelayStateStoreError):
        replace(
            capabilities,
            supported_model_ids=tuple(
                f"model-{i}" for i in range(MAX_MODEL_IDS_PER_NODE + 1)
            ),
        )
    with pytest.raises(RelayStateStoreError):
        replace(capabilities, max_concurrency=129)
    with pytest.raises(RelayStateStoreError):
        replace(capabilities, maximum_total_context_tokens=8191)
    assert (
        replace(capabilities, backend_class="private-host-data").backend_class
        == "unknown"
    )


def test_concurrent_registration_has_no_duplicates_or_torn_records(
    store_factory, capabilities
):
    store = store_factory(max_registrations=100)

    def register(index: int):
        node_index = index % 20
        return store.register(
            f"node-{node_index:02}",
            digest(node_index + 1),
            replace(capabilities, max_concurrency=(index % 8) + 1),
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(register, range(500)))
    records = store.list()
    assert len(records) == 20
    assert len({record.node_id for record in records}) == 20
    assert all(
        record.lease_expires_at_epoch - record.registered_at_epoch == 30
        for record in records
    )


def test_concurrent_renewal_preserves_complete_capability_snapshots(
    store_factory, capabilities
):
    store = store_factory()
    store.register("node-a", digest(1), capabilities)

    def renew(max_concurrency: int):
        return store.renew(
            "node-a", digest(1), replace(capabilities, max_concurrency=max_concurrency)
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(renew, ((index % 8) + 1 for index in range(500))))
    assert all(
        result is not None and 1 <= result.capabilities.max_concurrency <= 8
        for result in results
    )
    final = store.get("node-a")
    assert final is not None
    assert final.capabilities.supported_model_ids == ("model-a",)
    assert 1 <= final.capabilities.max_concurrency <= 8
