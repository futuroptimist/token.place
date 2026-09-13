"""Shared assertions for the registration-only backend component contract."""

from dataclasses import FrozenInstanceError, replace

import pytest

from relay_state_store import (
    RelayStateCapacityExceeded,
    RelayStateCredentialMismatch,
    RelayStateStoreError,
)


def assert_registration_contract(store, capabilities, digest) -> None:
    """Exercise only methods implemented by both registration backends."""

    owner = digest("owner")
    first = store.register("node-a", capabilities, owner)
    assert store.get("node-a") == first
    with pytest.raises(RelayStateCredentialMismatch):
        store.register("node-a", capabilities, digest("wrong"))
    changed = replace(capabilities, max_concurrency=3)
    duplicate = store.register("node-a", changed, owner)
    assert duplicate.registered_at_epoch == first.registered_at_epoch
    assert duplicate.capabilities == changed
    assert store.renew("node-a", owner) is not None
    with pytest.raises(RelayStateCredentialMismatch):
        store.renew("node-a", digest("wrong"))
    assert store.renew("missing", owner) is None
    with pytest.raises(RelayStateCapacityExceeded):
        store.register("node-b", capabilities, digest("other"))
    with pytest.raises(FrozenInstanceError):
        duplicate.node_id = "changed"
    with pytest.raises(RelayStateStoreError):
        store.get("")
    with pytest.raises(RelayStateStoreError):
        store.unregister("node-a", "raw-credential")
    assert store.unregister("node-a", owner) is True
    assert store.unregister("node-a", owner) is False
    assert store.list() == ()
