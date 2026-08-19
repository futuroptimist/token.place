import dataclasses
from concurrent.futures import ThreadPoolExecutor

import pytest

from relay_state_store import (
    ComputeCapabilities,
    InMemoryRelayStateStore,
    RelayStateCapacityError,
    RelayStateStore,
    RelayStateValidationError,
)


class EpochClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(params=[InMemoryRelayStateStore], ids=["memory"])
def store_factory(request):
    def create(*, clock=None, max_nodes=4, namespace="test.cluster"):
        return request.param(
            namespace=namespace,
            lease_ttl_seconds=30,
            max_nodes=max_nodes,
            epoch_clock=clock or EpochClock(),
        )

    return create


@pytest.fixture
def capabilities():
    return ComputeCapabilities(
        supported_model_ids=("qwen3-8b-instruct",),
        active_context_tier="8k-fast",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=1024,
        maximum_output_tokens=2048,
        max_concurrency=2,
        backend_class="cpu",
    )


def test_store_implements_public_contract(store_factory):
    assert isinstance(store_factory(), RelayStateStore)


def test_registration_lookup_listing_and_duplicate_semantics(
    store_factory, capabilities
):
    store = store_factory()
    first = store.register("node-b", "a" * 64, capabilities)
    duplicate = store.register(
        "node-b",
        "b" * 64,
        dataclasses.replace(capabilities, backend_class="cuda"),
    )
    store.register("node-a", "c" * 64, capabilities)

    assert first.lease_expires_at_epoch == 1_700_000_030.0
    assert duplicate.control_credential_digest == "a" * 64
    assert duplicate.capabilities.backend_class == "cuda"
    assert [record.node_id for record in store.list_nodes()] == ["node-a", "node-b"]
    assert store.get("node-b") == duplicate


def test_renewal_is_idempotent_and_may_change_capabilities(store_factory, capabilities):
    store = store_factory()
    store.register("node", "a" * 64, capabilities)
    unchanged = store.renew("node")
    changed = store.renew("node", dataclasses.replace(capabilities, max_concurrency=3))

    assert unchanged.lease_expires_at_epoch == changed.lease_expires_at_epoch
    assert changed.capabilities.max_concurrency == 3
    assert changed.control_credential_digest == "a" * 64
    assert store.renew("unknown") is None


def test_expiration_boundary_uses_injected_epoch_clock(store_factory, capabilities):
    clock = EpochClock()
    store = store_factory(clock=clock)
    record = store.register("node", "a" * 64, capabilities)
    assert (
        record.lease_expires_at_epoch > 1_000_000_000
    )  # epoch, never monotonic process time

    clock.now = record.lease_expires_at_epoch - 0.001
    assert store.get("node") is not None
    clock.now = record.lease_expires_at_epoch
    assert store.expire() == ("node",)
    assert store.get("node") is None


def test_explicit_unregistration_and_unknown_node_are_deterministic(
    store_factory, capabilities
):
    store = store_factory()
    store.register("node", "a" * 64, capabilities)
    assert store.unregister("node") is True
    assert store.unregister("node") is False
    assert store.unregister("unknown") is False


@pytest.mark.parametrize("namespace", ["", "has spaces", "x" * 129])
def test_namespace_validation(namespace):
    with pytest.raises(RelayStateValidationError):
        InMemoryRelayStateStore(namespace=namespace, lease_ttl_seconds=30, max_nodes=1)


def test_schema_ttl_and_capacity_bounds(store_factory, capabilities):
    with pytest.raises(RelayStateValidationError):
        InMemoryRelayStateStore(
            namespace="test", schema_version=2, lease_ttl_seconds=30, max_nodes=1
        )
    with pytest.raises(RelayStateValidationError):
        InMemoryRelayStateStore(namespace="test", lease_ttl_seconds=0, max_nodes=1)

    store = store_factory(max_nodes=1)
    store.register("one", "a" * 64, capabilities)
    with pytest.raises(RelayStateCapacityError):
        store.register("two", "b" * 64, capabilities)
    assert store.register("one", "b" * 64, capabilities).node_id == "one"


def test_capability_bounds_reuse_api_v1_scheduler_limits(capabilities):
    with pytest.raises(RelayStateValidationError):
        dataclasses.replace(
            capabilities, supported_model_ids=tuple(f"model-{i}" for i in range(65))
        )
    with pytest.raises(RelayStateValidationError):
        dataclasses.replace(capabilities, max_concurrency=129)
    with pytest.raises(RelayStateValidationError):
        dataclasses.replace(capabilities, active_context_tier="unbounded")


def test_records_are_immutable_and_reads_cannot_mutate_store(
    store_factory, capabilities
):
    store = store_factory()
    returned = store.register("node", "a" * 64, capabilities)
    with pytest.raises(dataclasses.FrozenInstanceError):
        returned.control_credential_digest = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        returned.capabilities.max_concurrency = 99
    assert store.get("node").control_credential_digest == "a" * 64


def test_raw_credentials_and_application_payload_fields_are_outside_contract(
    store_factory, capabilities
):
    fields = {field.name for field in dataclasses.fields(capabilities)}
    assert fields.isdisjoint(
        {"control_credential", "prompt", "messages", "tools", "model_output", "payload"}
    )
    with pytest.raises(TypeError):
        ComputeCapabilities(
            supported_model_ids=("model",),
            active_context_tier="8k-fast",
            maximum_total_context_tokens=8192,
            default_output_token_reservation=1,
            maximum_output_tokens=1,
            prompt="plaintext is not accepted",
        )
    with pytest.raises(RelayStateValidationError):
        store_factory().register("node", "raw-control-credential", capabilities)


def test_concurrent_registration_and_renewal_have_one_complete_record(
    store_factory, capabilities
):
    store = store_factory(max_nodes=2)

    def transition(index):
        updated = dataclasses.replace(capabilities, max_concurrency=1 + index % 8)
        store.register("node", f"{index:064x}", updated)
        return store.renew("node", updated)

    with ThreadPoolExecutor(max_workers=16) as executor:
        records = list(executor.map(transition, range(200)))

    assert len(store.list_nodes()) == 1
    assert all(record is not None and record.node_id == "node" for record in records)
    final = store.get("node")
    assert final.control_credential_digest in {f"{index:064x}" for index in range(200)}
    assert final.capabilities.max_concurrency in range(1, 9)
    assert final.lease_expires_at_epoch == 1_700_000_030.0
