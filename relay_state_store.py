"""Typed state-store boundary for API v1 compute-node registrations and leases.

This module is intentionally not wired into the relay runtime yet.  It contains
only bounded control-plane metadata; inference payloads do not belong here.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

SCHEMA_VERSION = 1
MAX_NAMESPACE_LENGTH = 128
MAX_NODE_ID_LENGTH = 16_384
MAX_CREDENTIAL_DIGEST_LENGTH = 128
MAX_MODEL_IDS_PER_NODE = 64
MAX_MODEL_ID_LENGTH = 128
MAX_REGISTRATIONS = 10_000
MIN_LEASE_TTL_SECONDS = 1.0
MAX_LEASE_TTL_SECONDS = 3_600.0
CONTEXT_TIER_TOKENS = {"8k-fast": 8192, "64k-full": 65536}
ALLOWED_BACKEND_CLASSES = frozenset(
    {"cpu", "cuda", "metal", "vulkan", "gpu", "unknown"}
)
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class RelayStateStoreError(ValueError):
    """Base class for deterministic store contract errors."""


class RegistrationCapacityError(RelayStateStoreError):
    """Raised when a new registration would exceed the configured bound."""


class RegistrationConflictError(RelayStateStoreError):
    """Raised when a duplicate node presents a different credential digest."""


@dataclass(frozen=True, slots=True)
class ComputeNodeCapabilities:
    """Bounded scheduler metadata accepted by the current API v1 relay."""

    supported_model_ids: tuple[str, ...]
    active_context_tier: str
    maximum_total_context_tokens: int
    default_output_token_reservation: int
    maximum_output_tokens: int
    max_concurrency: int
    backend_class: str = "unknown"
    api_version: str = "v1"

    def __post_init__(self) -> None:
        models = self.supported_model_ids
        if (
            not isinstance(models, tuple)
            or not 1 <= len(models) <= MAX_MODEL_IDS_PER_NODE
        ):
            raise RelayStateStoreError(
                "supported_model_ids must be a bounded, non-empty tuple"
            )
        normalized = tuple(
            model.strip().lower() for model in models if isinstance(model, str)
        )
        if len(normalized) != len(models) or any(
            not model or len(model) > MAX_MODEL_ID_LENGTH for model in normalized
        ):
            raise RelayStateStoreError(
                "supported_model_ids contains an invalid model id"
            )
        if len(set(normalized)) != len(normalized):
            raise RelayStateStoreError(
                "supported_model_ids must not contain duplicates"
            )
        object.__setattr__(self, "supported_model_ids", normalized)
        if self.api_version != "v1":
            raise RelayStateStoreError("api_version must be v1")
        tier_floor = CONTEXT_TIER_TOKENS.get(self.active_context_tier)
        if tier_floor is None:
            raise RelayStateStoreError("active_context_tier is unsupported")
        _bounded_positive_int(
            self.maximum_total_context_tokens, "maximum_total_context_tokens"
        )
        _bounded_positive_int(
            self.default_output_token_reservation, "default_output_token_reservation"
        )
        _bounded_positive_int(self.maximum_output_tokens, "maximum_output_tokens")
        _bounded_positive_int(self.max_concurrency, "max_concurrency", maximum=128)
        if self.maximum_total_context_tokens < tier_floor:
            raise RelayStateStoreError(
                "maximum_total_context_tokens is below the active tier"
            )
        if self.default_output_token_reservation > self.maximum_output_tokens:
            raise RelayStateStoreError(
                "default output reservation cannot exceed maximum output tokens"
            )
        backend = (
            self.backend_class.strip().lower()
            if isinstance(self.backend_class, str)
            else ""
        )
        object.__setattr__(
            self,
            "backend_class",
            backend if backend in ALLOWED_BACKEND_CLASSES else "unknown",
        )


@dataclass(frozen=True, slots=True)
class ComputeNodeRegistration:
    """Immutable registration record containing a digest, never a raw credential."""

    node_id: str
    control_credential_digest: str
    capabilities: ComputeNodeCapabilities
    registered_at_epoch: float
    lease_expires_at_epoch: float
    schema_version: int = SCHEMA_VERSION


class RelayStateStore(Protocol):
    """Transition-oriented compute registration and lease store contract."""

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
        capabilities: ComputeNodeCapabilities | None = None,
    ) -> ComputeNodeRegistration | None: ...
    def get(self, node_id: str) -> ComputeNodeRegistration | None: ...
    def list(self) -> tuple[ComputeNodeRegistration, ...]: ...
    def expire(self) -> tuple[str, ...]: ...
    def unregister(
        self, node_id: str, control_credential_digest: str | None = None
    ) -> bool: ...


class InMemoryRelayStateStore:
    """Lock-protected, process-local implementation of :class:`RelayStateStore`."""

    def __init__(
        self,
        *,
        namespace: str,
        schema_version: int = SCHEMA_VERSION,
        lease_ttl_seconds: float = 30.0,
        max_registrations: int = MAX_REGISTRATIONS,
        epoch_time: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(namespace, str)
            or not 1 <= len(namespace) <= MAX_NAMESPACE_LENGTH
            or not _NAMESPACE_RE.fullmatch(namespace)
        ):
            raise RelayStateStoreError("namespace is invalid")
        if schema_version != SCHEMA_VERSION:
            raise RelayStateStoreError(f"unsupported schema version: {schema_version}")
        if (
            not isinstance(lease_ttl_seconds, (int, float))
            or isinstance(lease_ttl_seconds, bool)
            or not math.isfinite(lease_ttl_seconds)
            or not MIN_LEASE_TTL_SECONDS <= lease_ttl_seconds <= MAX_LEASE_TTL_SECONDS
        ):
            raise RelayStateStoreError(
                "lease_ttl_seconds is outside the authoritative bounds"
            )
        _bounded_positive_int(
            max_registrations, "max_registrations", maximum=MAX_REGISTRATIONS
        )
        if not callable(epoch_time):
            raise RelayStateStoreError("epoch_time must be callable")
        self.namespace = namespace
        self.schema_version = schema_version
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.max_registrations = max_registrations
        self._epoch_time = epoch_time
        self._records: dict[str, ComputeNodeRegistration] = {}
        self._lock = threading.RLock()

    def register(
        self,
        node_id: str,
        control_credential_digest: str,
        capabilities: ComputeNodeCapabilities,
    ) -> ComputeNodeRegistration:
        node_id, digest = _validate_identity(node_id, control_credential_digest)
        if not isinstance(capabilities, ComputeNodeCapabilities):
            raise RelayStateStoreError("capabilities must be ComputeNodeCapabilities")
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            existing = self._records.get(node_id)
            if existing is not None and existing.control_credential_digest != digest:
                raise RegistrationConflictError(
                    "node is already owned by another credential digest"
                )
            if existing is None and len(self._records) >= self.max_registrations:
                raise RegistrationCapacityError("registration capacity reached")
            record = ComputeNodeRegistration(
                node_id=node_id,
                control_credential_digest=digest,
                capabilities=capabilities,
                registered_at_epoch=existing.registered_at_epoch if existing else now,
                lease_expires_at_epoch=now + self.lease_ttl_seconds,
            )
            self._records[node_id] = record
            return record

    def renew(
        self,
        node_id: str,
        control_credential_digest: str,
        capabilities: ComputeNodeCapabilities | None = None,
    ) -> ComputeNodeRegistration | None:
        node_id, digest = _validate_identity(node_id, control_credential_digest)
        if capabilities is not None and not isinstance(
            capabilities, ComputeNodeCapabilities
        ):
            raise RelayStateStoreError("capabilities must be ComputeNodeCapabilities")
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            existing = self._records.get(node_id)
            if existing is None:
                return None
            if existing.control_credential_digest != digest:
                raise RegistrationConflictError("credential digest does not own node")
            renewed = ComputeNodeRegistration(
                node_id=node_id,
                control_credential_digest=digest,
                capabilities=capabilities or existing.capabilities,
                registered_at_epoch=existing.registered_at_epoch,
                lease_expires_at_epoch=now + self.lease_ttl_seconds,
            )
            self._records[node_id] = renewed
            return renewed

    def get(self, node_id: str) -> ComputeNodeRegistration | None:
        node_id = _validate_node_id(node_id)
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

    def unregister(
        self, node_id: str, control_credential_digest: str | None = None
    ) -> bool:
        node_id = _validate_node_id(node_id)
        if control_credential_digest is not None:
            _validate_digest(control_credential_digest)
        with self._lock:
            self._expire_locked(self._now())
            existing = self._records.get(node_id)
            if existing is None:
                return False
            if (
                control_credential_digest is not None
                and existing.control_credential_digest != control_credential_digest
            ):
                raise RegistrationConflictError("credential digest does not own node")
            del self._records[node_id]
            return True

    def _now(self) -> float:
        now = self._epoch_time()
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(now)
        ):
            raise RelayStateStoreError("epoch clock returned an invalid value")
        return float(now)

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


def _bounded_positive_int(value: int, field: str, *, maximum: int = 1_000_000) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise RelayStateStoreError(
            f"{field} must be an integer between 1 and {maximum}"
        )


def _validate_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id or len(node_id) > MAX_NODE_ID_LENGTH:
        raise RelayStateStoreError("node_id is invalid")
    return node_id


def _validate_digest(digest: str) -> str:
    if (
        not isinstance(digest, str)
        or len(digest) > MAX_CREDENTIAL_DIGEST_LENGTH
        or not _DIGEST_RE.fullmatch(digest)
    ):
        raise RelayStateStoreError(
            "control_credential_digest must be a lowercase SHA-256 hex digest"
        )
    return digest


def _validate_identity(node_id: str, digest: str) -> tuple[str, str]:
    return _validate_node_id(node_id), _validate_digest(digest)
