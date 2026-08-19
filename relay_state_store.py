"""Typed state-store boundary for compute-node registration leases.

This module is deliberately independent from the current relay runtime.  It is the
small first contract that future shared-state backends must implement.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol, runtime_checkable

SCHEMA_VERSION = 1
MAX_NAMESPACE_LENGTH = 128
MAX_NODE_ID_LENGTH = 16_384
CONTROL_CREDENTIAL_DIGEST_LENGTH = 64
MAX_MODEL_IDS_PER_NODE = 64
MAX_MODEL_ID_LENGTH = 128
MAX_CONCURRENCY = 128
MAX_TOKEN_COUNT = 1_000_000
CONTEXT_TIER_TOKENS = {"8k-fast": 8192, "64k-full": 65536}
ALLOWED_BACKEND_CLASSES = frozenset(
    {"cpu", "cuda", "metal", "vulkan", "gpu", "unknown"}
)


class RelayStateValidationError(ValueError):
    """Raised when store configuration or a bounded record is invalid."""


class RelayStateCapacityError(RuntimeError):
    """Raised when registering a new node would exceed the configured bound."""


@dataclass(frozen=True, slots=True)
class ComputeCapabilities:
    """Bounded API-v1 scheduler metadata for one compute node."""

    supported_model_ids: tuple[str, ...]
    active_context_tier: str
    maximum_total_context_tokens: int
    default_output_token_reservation: int
    maximum_output_tokens: int
    max_concurrency: int = 1
    backend_class: str = "unknown"
    api_version: str = "v1"

    def __post_init__(self) -> None:
        models = self.supported_model_ids
        if (
            not isinstance(models, tuple)
            or not 1 <= len(models) <= MAX_MODEL_IDS_PER_NODE
        ):
            raise RelayStateValidationError(
                "supported_model_ids must be a bounded non-empty tuple"
            )
        if len(set(models)) != len(models) or any(
            not isinstance(model, str) or not model or len(model) > MAX_MODEL_ID_LENGTH
            for model in models
        ):
            raise RelayStateValidationError(
                "supported_model_ids contains an invalid or duplicate model"
            )
        if self.api_version != "v1":
            raise RelayStateValidationError("api_version must be v1")
        tier_minimum = CONTEXT_TIER_TOKENS.get(self.active_context_tier)
        if tier_minimum is None:
            raise RelayStateValidationError("active_context_tier is unsupported")
        integer_fields = (
            self.maximum_total_context_tokens,
            self.default_output_token_reservation,
            self.maximum_output_tokens,
            self.max_concurrency,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in integer_fields
        ):
            raise RelayStateValidationError(
                "token and concurrency bounds must be positive integers"
            )
        if (
            self.maximum_total_context_tokens > MAX_TOKEN_COUNT
            or self.maximum_output_tokens > MAX_TOKEN_COUNT
        ):
            raise RelayStateValidationError("token metadata exceeds the record bound")
        if self.default_output_token_reservation > self.maximum_output_tokens:
            raise RelayStateValidationError(
                "default output reservation exceeds maximum output tokens"
            )
        if (
            self.maximum_total_context_tokens < tier_minimum
            or self.max_concurrency > MAX_CONCURRENCY
        ):
            raise RelayStateValidationError(
                "capability metadata is outside scheduler bounds"
            )
        if self.backend_class not in ALLOWED_BACKEND_CLASSES:
            raise RelayStateValidationError("backend_class is unsupported")


@dataclass(frozen=True, slots=True)
class ComputeNodeRecord:
    """Immutable registration record; credentials are represented only by a digest."""

    node_id: str
    control_credential_digest: str
    capabilities: ComputeCapabilities
    lease_expires_at_epoch: float
    schema_version: int


@runtime_checkable
class RelayStateStore(Protocol):
    """Registration/lease state transitions implemented by every backend."""

    namespace: str
    schema_version: int
    lease_ttl_seconds: float
    max_nodes: int

    def register(
        self,
        node_id: str,
        control_credential_digest: str,
        capabilities: ComputeCapabilities,
    ) -> ComputeNodeRecord: ...

    def renew(
        self, node_id: str, capabilities: ComputeCapabilities | None = None
    ) -> ComputeNodeRecord | None: ...

    def get(self, node_id: str) -> ComputeNodeRecord | None: ...

    def list_nodes(self) -> tuple[ComputeNodeRecord, ...]: ...

    def expire(self) -> tuple[str, ...]: ...

    def unregister(self, node_id: str) -> bool: ...


class InMemoryRelayStateStore:
    """Thread-safe, process-local implementation of :class:`RelayStateStore`."""

    def __init__(
        self,
        *,
        namespace: str,
        schema_version: int = SCHEMA_VERSION,
        lease_ttl_seconds: float,
        max_nodes: int,
        epoch_clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(namespace, str)
            or not namespace
            or len(namespace) > MAX_NAMESPACE_LENGTH
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in namespace
            )
        ):
            raise RelayStateValidationError(
                "namespace must be a bounded cluster/environment identifier"
            )
        if schema_version != SCHEMA_VERSION:
            raise RelayStateValidationError(
                f"unsupported schema version: {schema_version}"
            )
        if (
            not isinstance(lease_ttl_seconds, (int, float))
            or not math.isfinite(lease_ttl_seconds)
            or lease_ttl_seconds <= 0
        ):
            raise RelayStateValidationError(
                "lease_ttl_seconds must be finite and positive"
            )
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or max_nodes < 1
        ):
            raise RelayStateValidationError("max_nodes must be a positive integer")
        self.namespace = namespace
        self.schema_version = schema_version
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.max_nodes = max_nodes
        self._clock = epoch_clock
        self._records: dict[str, ComputeNodeRecord] = {}
        self._lock = threading.RLock()

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RelayStateValidationError("epoch clock returned a non-finite value")
        return now

    @staticmethod
    def _validate_identity(node_id: str, digest: str | None = None) -> None:
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id) > MAX_NODE_ID_LENGTH
        ):
            raise RelayStateValidationError(
                "node_id must be a bounded non-empty string"
            )
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != CONTROL_CREDENTIAL_DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RelayStateValidationError(
                "control credential digest must be a lowercase SHA-256 hex digest"
            )

    def _expire_locked(self, now: float) -> list[str]:
        expired = sorted(
            node_id
            for node_id, record in self._records.items()
            if record.lease_expires_at_epoch <= now
        )
        for node_id in expired:
            del self._records[node_id]
        return expired

    def register(
        self,
        node_id: str,
        control_credential_digest: str,
        capabilities: ComputeCapabilities,
    ) -> ComputeNodeRecord:
        self._validate_identity(node_id, control_credential_digest)
        if not isinstance(capabilities, ComputeCapabilities):
            raise RelayStateValidationError(
                "capabilities must be a ComputeCapabilities record"
            )
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            existing = self._records.get(node_id)
            if existing is None and len(self._records) >= self.max_nodes:
                raise RelayStateCapacityError(
                    "compute-node registration capacity reached"
                )
            # Duplicate registration renews and updates scheduler metadata, but cannot
            # rotate the owner credential digest through an unauthenticated duplicate.
            digest = (
                existing.control_credential_digest
                if existing
                else control_credential_digest
            )
            record = ComputeNodeRecord(
                node_id=node_id,
                control_credential_digest=digest,
                capabilities=capabilities,
                lease_expires_at_epoch=now + self.lease_ttl_seconds,
                schema_version=self.schema_version,
            )
            self._records[node_id] = record
            return replace(record)

    def renew(
        self, node_id: str, capabilities: ComputeCapabilities | None = None
    ) -> ComputeNodeRecord | None:
        self._validate_identity(node_id)
        if capabilities is not None and not isinstance(
            capabilities, ComputeCapabilities
        ):
            raise RelayStateValidationError(
                "capabilities must be a ComputeCapabilities record"
            )
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            current = self._records.get(node_id)
            if current is None:
                return None
            record = replace(
                current,
                capabilities=(
                    current.capabilities if capabilities is None else capabilities
                ),
                lease_expires_at_epoch=now + self.lease_ttl_seconds,
            )
            self._records[node_id] = record
            return replace(record)

    def get(self, node_id: str) -> ComputeNodeRecord | None:
        self._validate_identity(node_id)
        with self._lock:
            self._expire_locked(self._now())
            record = self._records.get(node_id)
            return replace(record) if record else None

    def list_nodes(self) -> tuple[ComputeNodeRecord, ...]:
        with self._lock:
            self._expire_locked(self._now())
            return tuple(
                replace(self._records[node_id]) for node_id in sorted(self._records)
            )

    def expire(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._expire_locked(self._now()))

    def unregister(self, node_id: str) -> bool:
        self._validate_identity(node_id)
        with self._lock:
            self._expire_locked(self._now())
            return self._records.pop(node_id, None) is not None
