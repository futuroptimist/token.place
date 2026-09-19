"""Bounded rate-limit storage backed by the authoritative relay store."""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from limits.storage import Storage


class SharedRateLimitStorage(Storage):
    """``limits`` adapter that never contains connection details or raw identities."""

    STORAGE_SCHEME = ["tokenplace-valkey"]

    def __init__(self, uri: str | None = None, **options: Any) -> None:
        super().__init__(uri, wrap_exceptions=True)
        factory = options.get("coordinator_factory")
        route_class = options.get("route_class")
        if not callable(factory) or not isinstance(route_class, str):
            raise ValueError("invalid shared rate-limit configuration")
        self._factory: Callable[[], Any] = factory
        self._route_class = route_class
        self._expiries: dict[str, int] = {}

    @property
    def base_exceptions(self):
        return Exception

    @staticmethod
    def _digest(key: str) -> str:
        if not isinstance(key, str) or len(key.encode("utf-8")) > 4096:
            raise ValueError("invalid rate-limit identity")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        digest = self._digest(key)
        self._expiries[digest] = expiry
        return self._factory().rate_limit_increment(
            self._route_class, digest, expiry, amount
        )

    def get(self, key: str) -> int:
        digest = self._digest(key)
        return self._factory().rate_limit_read(
            self._route_class, digest, self._expiries.get(digest, 60)
        )[0]

    def get_expiry(self, key: str) -> float:
        digest = self._digest(key)
        return self._factory().rate_limit_read(
            self._route_class, digest, self._expiries.get(digest, 60)
        )[1]

    def decr(self, key: str, amount: int = 1) -> int:
        digest = self._digest(key)
        return self._factory().rate_limit_increment(
            self._route_class, digest, self._expiries.get(digest, 60), -amount
        )

    def check(self) -> bool:
        try:
            self._factory().readiness()
            return True
        except Exception:
            return False

    def reset(self) -> int:
        raise NotImplementedError("shared rate-limit reset is forbidden")

    def clear(self, key: str) -> None:
        raise NotImplementedError("shared rate-limit clear is forbidden")
