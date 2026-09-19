"""Focused contract tests for shared Valkey rate-limit storage."""

from unittest.mock import Mock

import pytest

from api.shared_rate_limit import SharedValkeyRateLimitStorage
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
