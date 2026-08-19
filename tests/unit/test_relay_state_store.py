"""Backend contract for registration/lease RelayStateStore implementations."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from relay_state_store import (
    CredentialMismatchError,
    ComputeNodeCapabilities,
    InMemoryRelayStateStore,
    MAX_MODEL_IDS_PER_NODE,
    RELAY_STATE_SCHEMA_VERSION,
    RelayStateStore,
    StoreCapacityError,
    UnknownNodeError,
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
    """Factory fixture future backends can replace to reuse this contract."""

    def make(**overrides):
        options = {
            "namespace": "testing.cluster-a",
            "schema_version": RELAY_STATE_SCHEMA_VERSION,
            "lease_ttl_seconds": 30,
            "max_registrations": 8,
            "clock": clock,
        }
        options.update(overrides)
        return InMemoryRelayStateStore(**options)

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
        backend_class="cpu",
    )


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def test_factory_produces_public_contract(store_factory):
    assert isinstance(store_factory(), RelayStateStore)


def test_registration_lookup_listing_and_duplicate_semantics(
    store_factory, clock, capabilities
):
    store = store_factory()
    registered = store.register("node-b", digest("owner"), capabilities)
    assert registered.registered_at_epoch == clock.value
    assert registered.lease_expires_at_epoch == clock.value + 30
    assert registered.namespace == "testing.cluster-a"
    assert registered.schema_version == RELAY_STATE_SCHEMA_VERSION

    clock.value += 5
    changed = replace(capabilities, backend_class="cuda")
    duplicate = store.register("node-b", digest("owner"), changed)
    assert duplicate.registered_at_epoch == registered.registered_at_epoch
    assert duplicate.lease_expires_at_epoch == clock.value + 30
    assert duplicate.capabilities == changed
    store.register("node-a", digest("a"), capabilities)
    assert tuple(record.node_id for record in store.list()) == ("node-a", "node-b")
    with pytest.raises(CredentialMismatchError):
        store.register("node-b", digest("intruder"), capabilities)


def test_renew_is_idempotent_and_may_change_valid_capabilities(
    store_factory, clock, capabilities
):
    store = store_factory()
    initial = store.register("node", digest("owner"), capabilities)
    renewed = store.renew("node", digest("owner"))
    assert renewed == initial
    clock.value += 1
    changed = replace(capabilities, max_concurrency=3)
    renewed = store.renew("node", digest("owner"), capabilities=changed)
    assert renewed.capabilities == changed
    assert renewed.lease_expires_at_epoch == clock.value + 30
    with pytest.raises(CredentialMismatchError):
        store.renew("node", digest("wrong"))
    with pytest.raises(UnknownNodeError):
        store.renew("missing", digest("owner"))


def test_expiration_uses_epoch_clock_and_boundary_is_expired(
    store_factory, clock, capabilities
):
    store = store_factory()
    store.register("node", digest("owner"), capabilities)
    clock.value += 29.999
    assert store.expire() == ()
    clock.value += 0.001
    assert store.expire() == ("node",)
    assert store.get("node") is None
    with pytest.raises(UnknownNodeError):
        store.renew("node", digest("owner"))


def test_unregister_is_atomic_and_unknown_removal_is_idempotent(
    store_factory, capabilities
):
    store = store_factory()
    assert store.unregister("unknown", digest("owner")) is False
    store.register("node", digest("owner"), capabilities)
    with pytest.raises(CredentialMismatchError):
        store.unregister("node", digest("wrong"))
    assert store.get("node") is not None
    assert store.unregister("node", digest("owner")) is True
    assert store.unregister("node", digest("owner")) is False


@pytest.mark.parametrize("namespace", ["", " space", "a/b", "x" * 129])
def test_namespace_is_bounded_and_validated(namespace, clock):
    with pytest.raises(ValueError):
        InMemoryRelayStateStore(namespace=namespace, clock=clock)


def test_schema_ttl_and_capacity_configuration_are_validated(
    store_factory, clock, capabilities
):
    with pytest.raises(ValueError, match="schema"):
        InMemoryRelayStateStore(namespace="test", schema_version=2, clock=clock)
    with pytest.raises(ValueError, match="TTL"):
        InMemoryRelayStateStore(namespace="test", lease_ttl_seconds=0, clock=clock)
    store = store_factory(max_registrations=1)
    store.register("one", digest("one"), capabilities)
    with pytest.raises(StoreCapacityError):
        store.register("two", digest("two"), capabilities)


def test_capability_bounds_match_current_api_v1_scheduler_shape(capabilities):
    with pytest.raises(ValueError):
        replace(
            capabilities,
            supported_model_ids=tuple(
                str(i) for i in range(MAX_MODEL_IDS_PER_NODE + 1)
            ),
        )
    with pytest.raises(ValueError):
        replace(capabilities, active_context_tier="invented")
    with pytest.raises(ValueError):
        replace(capabilities, max_concurrency=129)
    with pytest.raises(ValueError):
        replace(capabilities, maximum_total_context_tokens=8191)
    with pytest.raises(ValueError):
        replace(capabilities, default_output_token_reservation=2049)


def test_reads_are_immutable_and_do_not_expose_store_mutation(
    store_factory, capabilities
):
    store = store_factory()
    snapshot = store.register("node", digest("owner"), capabilities)
    with pytest.raises(FrozenInstanceError):
        snapshot.node_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.capabilities.supported_model_ids[0] = "changed"  # type: ignore[index]
    assert store.get("node") == snapshot


def test_raw_credentials_and_application_payloads_are_outside_typed_contract(
    store_factory, capabilities
):
    store = store_factory()
    with pytest.raises(ValueError, match="digest"):
        store.register("node", "raw-control-credential", capabilities)
    with pytest.raises(TypeError):
        ComputeNodeCapabilities(  # type: ignore[call-arg]
            supported_model_ids=("model",),
            active_context_tier="8k-fast",
            maximum_total_context_tokens=8192,
            default_output_token_reservation=1,
            maximum_output_tokens=1,
            max_concurrency=1,
            prompt="plaintext",
        )
    with pytest.raises(TypeError):
        store.register("node", digest("owner"), {"messages": ["plaintext"]})  # type: ignore[arg-type]


def test_concurrent_registration_and_renewal_has_one_complete_record(
    store_factory, capabilities
):
    store = store_factory()
    owner_digest = digest("owner")

    def transition(index: int):
        changed = replace(capabilities, max_concurrency=(index % 8) + 1)
        try:
            return store.register("node", owner_digest, changed)
        except StoreCapacityError:  # pragma: no cover - contract forbids this outcome
            raise AssertionError("duplicate registration consumed capacity")

    with ThreadPoolExecutor(max_workers=16) as pool:
        records = list(pool.map(transition, range(128)))
        renewed = list(
            pool.map(lambda _: store.renew("node", owner_digest), range(128))
        )

    assert len(store.list()) == 1
    final = store.get("node")
    assert final is not None
    assert final in records or final in renewed
    assert final.control_credential_digest == owner_digest
    assert final.capabilities.maximum_output_tokens == 2048
