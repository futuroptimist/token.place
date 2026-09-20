"""Focused contract tests for shared Valkey rate-limit storage."""

from unittest.mock import Mock

import pytest

from api.shared_rate_limit import (
    RateLimitBackendUnavailable,
    SharedValkeyRateLimitStorage,
)
from relay_state_store import RelayStateStoreError
from valkey_relay_state import ValkeyFoundationError, ValkeyRegistrationStore


def _storage(results):
    foundation = Mock()
    foundation.config = Mock()
    foundation.config.key.side_effect = (
        lambda family, route, digest, window:
        f"tokenplace:{{test:cluster}}:relay:v2:{family}:{route}:{digest}:{window}"
    )
    foundation.execute.side_effect = results
    store = object.__new__(ValkeyRegistrationStore)
    store._foundation = foundation
    return SharedValkeyRateLimitStorage(
        "tokenplace-valkey://shared", store_factory=lambda: store
    ), foundation


def test_fixed_window_operations_validate_and_parse_results():
    storage, foundation = _storage(
        ([b"ok", b"2", b"120"], [b"ok", b"2", b"120"], [b"ok", b"1", b"120"])
    )
    key = "LIMITER/user/2/1/minute"

    assert storage.incr(key, 60, 2) == 2
    assert storage.get(key) == 2
    assert storage.decr(key) == 1
    assert all("{test:cluster}" in call.args[1][0] for call in foundation.execute.call_args_list)


def test_atomic_multi_bucket_rejection_is_reported_without_adapter_rollback():
    storage, foundation = _storage(([b"limited", b"2", b"3600"],))

    assert storage.hit_many([
        ("LIMITER/ip/10/1/hour", 1, 10),
        ("LIMITER/owner/2/1/hour", 1, 2),
    ]) == (False, 1, 3600.0)
    foundation.execute.assert_called_once()


@pytest.mark.parametrize("result", ([b"bad"], [b"ok", b"not-an-int", b"3"]))
def test_malformed_backend_results_fail_closed(result):
    storage, _ = _storage((result,))
    with pytest.raises(ValkeyFoundationError, match="invalid result"):
        storage.get("LIMITER/user/2/1/minute")


def test_configuration_and_backend_failures_are_bounded():
    with pytest.raises(ValueError, match="not configured"):
        SharedValkeyRateLimitStorage("tokenplace-valkey://shared")

    for error in (ValkeyFoundationError("down"), RelayStateStoreError("down")):
        storage = SharedValkeyRateLimitStorage(
            "tokenplace-valkey://shared",
            store_factory=Mock(side_effect=error),
        )
        with pytest.raises(RateLimitBackendUnavailable, match="backend unavailable"):
            storage.get("LIMITER/user/2/1/minute")

    storage = SharedValkeyRateLimitStorage(
        "tokenplace-valkey://shared", store_factory=lambda: object()
    )
    with pytest.raises(ValueError, match="not Valkey-backed"):
        storage.check()


@pytest.mark.parametrize(
    ("key", "expiry"),
    (
        ("invalid", 60),
        ("LIMITER/user/2/367/day", 367 * 86400),
        ("LIMITER/user/2/1/minute", 30),
    ),
)
def test_mutations_reject_unbounded_keys_and_windows(key, expiry):
    storage, _ = _storage(())
    with pytest.raises(ValueError, match="invalid bounded rate-limit"):
        storage.incr(key, expiry)


def test_mutations_and_multi_bucket_inputs_are_bounded():
    storage, _ = _storage(())
    key = "LIMITER/user/2/1/minute"

    with pytest.raises(ValueError, match="mutation"):
        storage.incr(key, 60, 0)
    with pytest.raises(ValueError, match="rollback"):
        storage.decr(key, 2)
    with pytest.raises(ValueError, match="buckets"):
        storage.hit_many([])
    with pytest.raises(ValueError, match="bucket"):
        storage.hit_many([(key, 3, 2)])


def test_multi_bucket_success_and_backend_failures():
    storage, foundation = _storage(([b"ok"], ValkeyFoundationError("down")))
    bucket = [("LIMITER/user/2/1/minute", 1, 2)]

    assert storage.hit_many(bucket) == (True, -1, 0)
    with pytest.raises(RateLimitBackendUnavailable, match="backend unavailable"):
        storage.hit_many(bucket)

    foundation.readiness.return_value = None
    assert storage.check() is True
    foundation.readiness.assert_called_once_with()
    assert storage.reset() is None
    with pytest.raises(ValkeyFoundationError, match="clear is unavailable"):
        storage.clear(bucket[0][0])
