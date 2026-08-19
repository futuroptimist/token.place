"""Typed storage boundary for compute-node registrations and leases.

This module is intentionally not wired into the relay runtime yet.  It defines
the first, backend-neutral state-machine slice needed by a future shared store.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

RELAY_STATE_SCHEMA_VERSION = 1
DEFAULT_REGISTRATION_LEASE_SECONDS = 30.0
MAX_REGISTRATIONS = 10_000
MAX_NODE_ID_LENGTH = 16_384
MAX_MODEL_IDS_PER_NODE = 64
MAX_MODEL_ID_LENGTH = 128
MAX_TOKEN_COUNT = 1_000_000
MAX_CONCURRENCY = 128
CONTEXT_TIER_TOKEN_LIMITS = {"8k-fast": 8192, "64k-full": 65536}
ALLOWED_BACKEND_CLASSES = frozenset(
    {"cpu", "cuda", "metal", "vulkan", "gpu", "unknown"}
)

_NAMESPACE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RelayStateStoreError(Exception):
    """Base error for deterministic store contract failures."""


class StoreCapacityError(RelayStateStoreError):
    """The configured registration bound has been reached."""


class CredentialMismatchError(RelayStateStoreError):
    """The supplied digest does not own the existing registration."""


class UnknownNodeError(RelayStateStoreError):
    """A lease renewal targeted an unknown or expired node."""


@dataclass(frozen=True, slots=True)
class ComputeNodeCapabilities:
    """Bounded API-v1 scheduler metadata; no application payload is accepted."""

    supported_model_ids: tuple[str, ...]
    active_context_tier: str
    maximum_total_context_tokens: int
    default_output_token_reservation: int
    maximum_output_tokens: int
    max_concurrency: int
    backend_class: str = "unknown"
    api_version: str = "v1"

    def __post_init__(self) -> None:
        if self.api_version != "v1":
            raise ValueError("capability api_version must be v1")
        if self.active_context_tier not in CONTEXT_TIER_TOKEN_LIMITS:
            raise ValueError("unsupported active context tier")
        models = self.supported_model_ids
        if (
            not isinstance(models, tuple)
            or not 1 <= len(models) <= MAX_MODEL_IDS_PER_NODE
        ):
            raise ValueError("supported_model_ids must be a bounded, non-empty tuple")
        if len(set(models)) != len(models):
            raise ValueError("supported_model_ids must not contain duplicates")
        if any(
            not isinstance(model, str) or not model or len(model) > MAX_MODEL_ID_LENGTH
            for model in models
        ):
            raise ValueError("model ids must be bounded, non-empty strings")
        self._validate_positive_int(
            "maximum_total_context_tokens", self.maximum_total_context_tokens
        )
        self._validate_positive_int(
            "default_output_token_reservation", self.default_output_token_reservation
        )
        self._validate_positive_int("maximum_output_tokens", self.maximum_output_tokens)
        self._validate_positive_int(
            "max_concurrency", self.max_concurrency, maximum=MAX_CONCURRENCY
        )
        if (
            self.maximum_total_context_tokens
            < CONTEXT_TIER_TOKEN_LIMITS[self.active_context_tier]
        ):
            raise ValueError("maximum context is below the active tier")
        if self.default_output_token_reservation > self.maximum_output_tokens:
            raise ValueError("default output reservation exceeds maximum output tokens")
        if self.backend_class not in ALLOWED_BACKEND_CLASSES:
            raise ValueError("unsupported backend class")

    @staticmethod
    def _validate_positive_int(
        name: str, value: int, *, maximum: int = MAX_TOKEN_COUNT
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class ComputeNodeRegistration:
    """Immutable registration snapshot with an authoritative UTC epoch lease."""

    namespace: str
    schema_version: int
    node_id: str
    control_credential_digest: str
    capabilities: ComputeNodeCapabilities
    registered_at_epoch: float
    lease_expires_at_epoch: float


@runtime_checkable
class RelayStateStore(Protocol):
    """Transition-oriented contract for registration and lease state only."""

    namespace: str
    schema_version: int
    lease_ttl_seconds: float
    max_registrations: int

    def register(
        self,
        node_id: str,
        control_credential_digest: str,
        capabilities: ComputeNodeCapabilities,
    ) -> ComputeNodeRegistration: ...

    def renew(
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        capabilities: ComputeNodeCapabilities | None = None,
    ) -> ComputeNodeRegistration: ...

    def get(self, node_id: str) -> ComputeNodeRegistration | None: ...

    def list(self) -> tuple[ComputeNodeRegistration, ...]: ...

    def expire(self) -> tuple[str, ...]: ...

    def unregister(self, node_id: str, control_credential_digest: str) -> bool: ...


class InMemoryRelayStateStore:
    """Thread-safe, process-local implementation of :class:`RelayStateStore`."""

    def __init__(
        self,
        *,
        namespace: str,
        schema_version: int = RELAY_STATE_SCHEMA_VERSION,
        lease_ttl_seconds: float = DEFAULT_REGISTRATION_LEASE_SECONDS,
        max_registrations: int = MAX_REGISTRATIONS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(namespace, str) or not _NAMESPACE_PATTERN.fullmatch(
            namespace
        ):
            raise ValueError(
                "namespace must be a bounded environment/cluster identifier"
            )
        if schema_version != RELAY_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported relay state schema version: {schema_version}"
            )
        if not isinstance(lease_ttl_seconds, (int, float)) or not math.isfinite(
            lease_ttl_seconds
        ):
            raise ValueError("lease TTL must be finite")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")
        if (
            isinstance(max_registrations, bool)
            or not isinstance(max_registrations, int)
            or max_registrations <= 0
        ):
            raise ValueError("max_registrations must be a positive integer")
        self.namespace = namespace
        self.schema_version = schema_version
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.max_registrations = max_registrations
        self._clock = clock
        self._records: dict[str, ComputeNodeRegistration] = {}
        self._lock = threading.RLock()

    def register(
        self,
        node_id: str,
        control_credential_digest: str,
        capabilities: ComputeNodeCapabilities,
    ) -> ComputeNodeRegistration:
        self._validate_input(node_id, control_credential_digest, capabilities)
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            existing = self._records.get(node_id)
            if (
                existing is not None
                and existing.control_credential_digest != control_credential_digest
            ):
                raise CredentialMismatchError(
                    "node is already owned by another credential digest"
                )
            if existing is None and len(self._records) >= self.max_registrations:
                raise StoreCapacityError("registration capacity reached")
            registered_at = existing.registered_at_epoch if existing else now
            record = self._new_record(
                node_id, control_credential_digest, capabilities, registered_at, now
            )
            self._records[node_id] = record
            return record

    def renew(
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        capabilities: ComputeNodeCapabilities | None = None,
    ) -> ComputeNodeRegistration:
        self._validate_identity(node_id, control_credential_digest)
        if capabilities is not None and not isinstance(
            capabilities, ComputeNodeCapabilities
        ):
            raise TypeError("capabilities must be ComputeNodeCapabilities")
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            existing = self._records.get(node_id)
            if existing is None:
                raise UnknownNodeError("cannot renew an unknown or expired node")
            if existing.control_credential_digest != control_credential_digest:
                raise CredentialMismatchError("credential digest does not own node")
            record = self._new_record(
                node_id,
                control_credential_digest,
                capabilities or existing.capabilities,
                existing.registered_at_epoch,
                now,
            )
            self._records[node_id] = record
            return record

    def get(self, node_id: str) -> ComputeNodeRegistration | None:
        self._validate_node_id(node_id)
        with self._lock:
            self._expire_locked(self._now())
            return self._records.get(node_id)

    def list(self) -> tuple[ComputeNodeRegistration, ...]:
        with self._lock:
            self._expire_locked(self._now())
            return tuple(self._records[node_id] for node_id in sorted(self._records))

    def expire(self) -> tuple[str, ...]:
        with self._lock:
            return self._expire_locked(self._now())

    def unregister(self, node_id: str, control_credential_digest: str) -> bool:
        self._validate_identity(node_id, control_credential_digest)
        with self._lock:
            self._expire_locked(self._now())
            existing = self._records.get(node_id)
            if existing is None:
                return False
            if existing.control_credential_digest != control_credential_digest:
                raise CredentialMismatchError("credential digest does not own node")
            del self._records[node_id]
            return True

    def _new_record(
        self,
        node_id: str,
        digest: str,
        capabilities: ComputeNodeCapabilities,
        registered_at: float,
        now: float,
    ) -> ComputeNodeRegistration:
        return ComputeNodeRegistration(
            namespace=self.namespace,
            schema_version=self.schema_version,
            node_id=node_id,
            control_credential_digest=digest,
            capabilities=capabilities,
            registered_at_epoch=registered_at,
            lease_expires_at_epoch=now + self.lease_ttl_seconds,
        )

    def _expire_locked(self, now: float) -> tuple[str, ...]:
        expired = tuple(
            sorted(
                node_id
                for node_id, record in self._records.items()
                if record.lease_expires_at_epoch <= now
            )
        )
        for node_id in expired:
            del self._records[node_id]
        return expired

    def _now(self) -> float:
        now = self._clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("clock must return a finite UTC epoch value")
        return float(now)

    @classmethod
    def _validate_input(
        cls, node_id: str, digest: str, capabilities: ComputeNodeCapabilities
    ) -> None:
        cls._validate_identity(node_id, digest)
        if not isinstance(capabilities, ComputeNodeCapabilities):
            raise TypeError("capabilities must be ComputeNodeCapabilities")

    @staticmethod
    def _validate_node_id(node_id: str) -> None:
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id) > MAX_NODE_ID_LENGTH
        ):
            raise ValueError("node_id must be a bounded, non-empty string")

    @classmethod
    def _validate_identity(cls, node_id: str, digest: str) -> None:
        cls._validate_node_id(node_id)
        if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
            raise ValueError(
                "control credential digest must be a lowercase SHA-256 hex digest"
            )
