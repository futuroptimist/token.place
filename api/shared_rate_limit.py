"""limits storage adapter for the relay's reviewed Valkey key space."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Callable

from limits.storage import Storage

from relay_state_store import RelayStateStoreError
from valkey_relay_state import (
    RATE_LIMIT_MULTI_SCRIPT, RATE_LIMIT_SCRIPT, ValkeyFoundationError,
    ValkeyRegistrationStore,
)


class RateLimitBackendUnavailable(ValkeyFoundationError):
    """Bounded exception used only for rate-limit coordination failures."""

_LIMIT_KEY_RE = re.compile(r"^LIMITER/.+/(?P<amount>[1-9][0-9]*)/(?P<multiples>[1-9][0-9]*)/(?P<unit>second|minute|hour|day|month|year)s?$")
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "month": 30 * 86400, "year": 365 * 86400}


class SharedValkeyRateLimitStorage(Storage):
    """Fail-closed fixed-window storage using Valkey TIME and reviewed Lua."""

    STORAGE_SCHEME = ["tokenplace-valkey"]

    def __init__(self, uri: str | None = None, **options: Any) -> None:
        super().__init__(uri, **options)
        factory = options.get("store_factory")
        if not callable(factory):
            raise ValueError("shared rate-limit store is not configured")
        self._store_factory: Callable[[], Any] = factory

    @property
    def _foundation(self):
        try:
            store = self._store_factory()
        except (ValkeyFoundationError, RelayStateStoreError):
            raise RateLimitBackendUnavailable(
                "rate-limit backend unavailable"
            ) from None
        if not isinstance(store, ValkeyRegistrationStore):
            raise ValueError("shared rate-limit store is not Valkey-backed")
        return store._foundation

    @property
    def base_exceptions(self):
        return (ValkeyFoundationError, RelayStateStoreError, RateLimitBackendUnavailable)

    @staticmethod
    def _expiry(key: str) -> int:
        match = _LIMIT_KEY_RE.fullmatch(key)
        if match is None:
            raise ValueError("invalid bounded rate-limit key")
        expiry = int(match.group("multiples")) * _UNIT_SECONDS[match.group("unit")]
        if not 1 <= expiry <= 366 * 86400:
            raise ValueError("invalid bounded rate-limit window")
        return expiry

    def _execute(self, operation: str, key: str, amount: int = 0) -> tuple[int, float]:
        expiry = self._expiry(key)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        # Valkey chooses the authoritative window from TIME inside the script.
        # Remove only the validated placeholder, leaving the final separator.
        prefix = self._foundation.config.key("ratelimit", "application", digest, 0)[:-1]
        try:
            result = self._foundation.execute(
                RATE_LIMIT_SCRIPT.name,
                (prefix,),
                (operation.encode(), str(expiry).encode(), str(amount).encode()),
                max_result_bytes=256,
            )
        except (ValkeyFoundationError, RelayStateStoreError):
            raise RateLimitBackendUnavailable(
                "rate-limit backend unavailable"
            ) from None
        try:
            if not isinstance(result, list) or len(result) != 3:
                raise ValueError
            status, value_raw, reset_raw = result
            if status != b"ok" or not isinstance(value_raw, bytes) or not isinstance(reset_raw, bytes):
                raise ValueError
            value = int(value_raw)
            reset = float(reset_raw)
            if value < 0 or not math.isfinite(reset) or reset <= 0:
                raise ValueError
            return value, reset
        except (TypeError, ValueError, OverflowError):
            raise RateLimitBackendUnavailable(
                "rate-limit backend returned invalid result"
            ) from None

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        if expiry != self._expiry(key) or not 1 <= amount <= 1_000_000:
            raise ValueError("invalid bounded rate-limit mutation")
        return self._execute("hit", key, amount)[0]

    def decr(self, key: str, amount: int = 1) -> int:
        if amount != 1:
            raise ValueError("invalid bounded rate-limit rollback")
        return self._execute("decr", key, amount)[0]

    def hit_many(self, buckets: list[tuple[str, int, int]]) -> tuple[bool, int, float]:
        if not buckets or len(buckets) > 16:
            raise ValueError("invalid bounded rate-limit buckets")
        keys, args = [], []
        for key, amount, maximum in buckets:
            expiry = self._expiry(key)
            if not 1 <= amount <= maximum <= 1_000_000:
                raise ValueError("invalid bounded rate-limit bucket")
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            keys.append(self._foundation.config.key("ratelimit", "application", digest, 0)[:-1])
            args.extend((str(expiry).encode(), str(amount).encode(), str(maximum).encode()))
        try:
            result = self._foundation.execute(
                RATE_LIMIT_MULTI_SCRIPT.name, tuple(keys), tuple(args), max_result_bytes=256
            )
        except (ValkeyFoundationError, RelayStateStoreError):
            raise RateLimitBackendUnavailable(
                "rate-limit backend unavailable"
            ) from None
        if result == [b"ok"]:
            return True, -1, 0
        try:
            if not isinstance(result, list) or len(result) != 3:
                raise ValueError
            status, index_raw, retry_raw = result
            if status != b"limited" or not isinstance(index_raw, bytes) or not isinstance(retry_raw, bytes):
                raise ValueError
            index = int(index_raw) - 1
            retry_after = int(retry_raw)
            if not 0 <= index < len(buckets) or not 1 <= retry_after <= self._expiry(buckets[index][0]):
                raise ValueError
            return False, index, retry_after
        except (TypeError, ValueError, OverflowError):
            raise RateLimitBackendUnavailable(
                "rate-limit backend returned invalid result"
            ) from None

    def get(self, key: str) -> int:
        return self._execute("get", key)[0]

    def get_expiry(self, key: str) -> float:
        return self._execute("get", key)[1]

    def check(self) -> bool:
        self._foundation.readiness()
        return True

    def reset(self) -> None:
        return None

    def clear(self, key: str) -> None:
        raise ValkeyFoundationError("shared rate-limit clear is unavailable")
