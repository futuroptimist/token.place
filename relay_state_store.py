"""Typed state-store boundary for compute-node registrations and leases.

This module is deliberately not wired into the relay runtime yet.  It defines the
small registration/lease contract that future shared backends must implement.
"""

from __future__ import annotations

import hmac
import hashlib
import math
import re
import threading
import time
import secrets
from dataclasses import dataclass, replace
from typing import Callable, Protocol, runtime_checkable

RELAY_STATE_SCHEMA_VERSION = 1
CONTEXT_TIER_TOKEN_BOUNDS = {"8k-fast": 8192, "64k-full": 65536}
ALLOWED_BACKEND_CLASSES = frozenset(
    {"cpu", "cuda", "metal", "vulkan", "gpu", "unknown"}
)
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RelayStateStoreError(ValueError):
    """Base error for invalid state transitions or store configuration."""


class RelayStateCapacityExceeded(RelayStateStoreError):
    """Raised when a new registration would exceed the configured record bound."""


class RelayStateCredentialMismatch(RelayStateStoreError):
    """Raised when a live registration is addressed with the wrong digest."""


class RelayStateConflict(RelayStateStoreError):
    """An identity already exists with different immutable parameters."""


class RelayStateReservationRejected(RelayStateStoreError):
    """A reservation is missing, expired, consumed, or does not match."""


class RelayStateNoEligibleNode(RelayStateStoreError):
    """No registration can safely accept the requested work."""


@dataclass(frozen=True, slots=True)
class RelayStateStoreConfig:
    """Explicit key-space, expiry, and size policy shared by all backends."""

    namespace: str
    schema_version: int = RELAY_STATE_SCHEMA_VERSION
    lease_ttl_seconds: float = 30.0
    max_compute_nodes: int = 1024
    max_node_id_bytes: int = 8192
    reservation_ttl_seconds: float = 10.0
    max_reservations: int = 4096
    max_reservations_per_client: int = 32
    max_reservations_per_node: int = 128
    max_queue_depth_per_node: int = 128
    max_identity_bytes: int = 8192
    max_model_bytes: int = 128
    max_envelope_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not _NAMESPACE_RE.fullmatch(
            self.namespace
        ):
            raise RelayStateStoreError(
                "namespace must be 1-128 lowercase routing-safe characters"
            )
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != RELAY_STATE_SCHEMA_VERSION
        ):
            raise RelayStateStoreError("unsupported relay state schema version")
        if (
            isinstance(self.lease_ttl_seconds, bool)
            or not isinstance(self.lease_ttl_seconds, (int, float))
            or not math.isfinite(float(self.lease_ttl_seconds))
            or self.lease_ttl_seconds <= 0
        ):
            raise RelayStateStoreError("lease TTL must be a finite positive number")
        if (
            isinstance(self.max_compute_nodes, bool)
            or not isinstance(self.max_compute_nodes, int)
            or not 1 <= self.max_compute_nodes <= 1_000_000
        ):
            raise RelayStateStoreError(
                "compute-node bound must be between 1 and 1,000,000"
            )
        if (
            isinstance(self.max_node_id_bytes, bool)
            or not isinstance(self.max_node_id_bytes, int)
            or not 1 <= self.max_node_id_bytes <= 65_536
        ):
            raise RelayStateStoreError(
                "node-id bound must be between 1 and 65,536 bytes"
            )
        for name, value, maximum in (
            ("reservation bound", self.max_reservations, 1_000_000),
            ("per-client reservation bound", self.max_reservations_per_client, 100_000),
            ("per-node reservation bound", self.max_reservations_per_node, 100_000),
            ("queue bound", self.max_queue_depth_per_node, 100_000),
            ("identity bound", self.max_identity_bytes, 65_536),
            ("model bound", self.max_model_bytes, 4096),
            ("envelope bound", self.max_envelope_bytes, 16_777_216),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise RelayStateStoreError(
                    f"{name} must be an integer between 1 and {maximum}"
                )
        if (
            isinstance(self.reservation_ttl_seconds, bool)
            or not isinstance(self.reservation_ttl_seconds, (int, float))
            or not math.isfinite(float(self.reservation_ttl_seconds))
            or self.reservation_ttl_seconds <= 0
        ):
            raise RelayStateStoreError(
                "reservation TTL must be a finite positive number"
            )


@dataclass(frozen=True, slots=True)
class ComputeNodeCapabilities:
    """Bounded API-v1 scheduler metadata; no application payload fields exist."""

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
            raise RelayStateStoreError("capability API version must be v1")
        if (
            not isinstance(self.supported_model_ids, tuple)
            or not 1 <= len(self.supported_model_ids) <= 64
        ):
            raise RelayStateStoreError(
                "supported model IDs must be a tuple containing 1-64 values"
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for model_id in self.supported_model_ids:
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or len(model_id.strip()) > 128
            ):
                raise RelayStateStoreError(
                    "model IDs must be non-empty strings of at most 128 characters"
                )
            value = model_id.strip().lower()
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        object.__setattr__(self, "supported_model_ids", tuple(normalized))
        normalized_tier = (
            self.active_context_tier.strip().lower()
            if isinstance(self.active_context_tier, str)
            else ""
        )
        if normalized_tier not in CONTEXT_TIER_TOKEN_BOUNDS:
            raise RelayStateStoreError("unsupported active context tier")
        object.__setattr__(self, "active_context_tier", normalized_tier)
        self._validate_positive_int(
            self.maximum_total_context_tokens, "maximum context tokens"
        )
        if (
            self.maximum_total_context_tokens
            < CONTEXT_TIER_TOKEN_BOUNDS[self.active_context_tier]
        ):
            raise RelayStateStoreError(
                "maximum context tokens are below the active tier"
            )
        self._validate_positive_int(
            self.default_output_token_reservation, "default output reservation"
        )
        self._validate_positive_int(self.maximum_output_tokens, "maximum output tokens")
        if self.default_output_token_reservation > self.maximum_output_tokens:
            raise RelayStateStoreError(
                "default output reservation exceeds maximum output tokens"
            )
        self._validate_positive_int(
            self.max_concurrency, "maximum concurrency", maximum=128
        )
        normalized_backend = (
            self.backend_class.strip().lower()
            if isinstance(self.backend_class, str)
            else ""
        )
        object.__setattr__(
            self,
            "backend_class",
            (
                normalized_backend
                if normalized_backend in ALLOWED_BACKEND_CLASSES
                else "unknown"
            ),
        )

    @staticmethod
    def _validate_positive_int(
        value: int, name: str, *, maximum: int = 1_000_000
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise RelayStateStoreError(
                f"{name} must be an integer between 1 and {maximum}"
            )


@dataclass(frozen=True, slots=True)
class ComputeNodeRegistration:
    """Immutable stored registration containing a digest, never a raw credential."""

    node_id: str
    capabilities: ComputeNodeCapabilities
    control_credential_digest: str
    registered_at_epoch: float
    lease_expires_at_epoch: float
    healthy: bool = True
    draining: bool = False


@dataclass(frozen=True, slots=True)
class EncryptedRequestEnvelope:
    """Exact relay-blind API-v1 ciphertext envelope."""

    protocol: str
    version: str
    ciphertext: str
    cipherkey: str
    iv: str

    def __post_init__(self) -> None:
        if self.protocol != "e2ee" or self.version != "v1":
            raise RelayStateStoreError("encrypted envelope must use e2ee API v1")
        if not all(
            isinstance(v, str) and v for v in (self.ciphertext, self.cipherkey, self.iv)
        ):
            raise RelayStateStoreError(
                "encrypted envelope fields must be non-empty strings"
            )


@dataclass(frozen=True, slots=True)
class SchedulerReservation:
    """Safe selection result. Raw token exists only in the first return value."""

    node_id: str
    model_id: str
    context_tier: str
    deadline_epoch: float
    reservation_expires_at_epoch: float
    reservation_token: str | None = None
    retry: bool = False


@dataclass(frozen=True, slots=True)
class QueuedEncryptedRequest:
    client_identity_digest: str
    request_identity_digest: str
    node_id: str
    model_id: str
    context_tier: str
    deadline_epoch: float
    envelope: EncryptedRequestEnvelope
    queued_at_epoch: float


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    node_id: str
    client_identity_digest: str
    request_identity_digest: str
    queued_at_epoch: float
    retry: bool = False


@dataclass(frozen=True, slots=True)
class _StoredReservation:
    token_digest: str
    client_digest: str
    request_digest: str
    node_id: str
    model_id: str
    context_tier: str
    deadline_epoch: float
    expires_at_epoch: float


@runtime_checkable
class RelayStateStore(Protocol):
    """Transition-oriented compute registration and lease state contract."""

    @property
    def config(self) -> RelayStateStoreConfig: ...

    def register(
        self,
        node_id: str,
        capabilities: ComputeNodeCapabilities,
        control_credential_digest: str,
    ) -> ComputeNodeRegistration: ...

    def renew(
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        capabilities: ComputeNodeCapabilities | None = None,
    ) -> ComputeNodeRegistration | None: ...
    def get(self, node_id: str) -> ComputeNodeRegistration | None: ...
    def list(self) -> tuple[ComputeNodeRegistration, ...]: ...
    def expire(self) -> tuple[ComputeNodeRegistration, ...]: ...
    def unregister(self, node_id: str, control_credential_digest: str) -> bool: ...
    def select_and_reserve(
        self,
        client_identity: str,
        request_identity: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
    ) -> SchedulerReservation: ...
    def consume_reservation_and_enqueue(
        self,
        client_identity: str,
        request_identity: str,
        reservation_token: str,
        node_id: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult: ...
    def queued(self, node_id: str) -> tuple[QueuedEncryptedRequest, ...]: ...


class InMemoryRelayStateStore:
    """Lock-protected, process-local implementation of :class:`RelayStateStore`."""

    def __init__(
        self,
        config: RelayStateStoreConfig,
        *,
        epoch_time: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._epoch_time = epoch_time
        self._records: dict[str, ComputeNodeRegistration] = {}
        self._reservations: dict[tuple[str, str], _StoredReservation] = {}
        self._queues: dict[str, list[QueuedEncryptedRequest]] = {}
        self._enqueued: dict[tuple[str, str], tuple[str, EnqueueResult]] = {}
        self._cursors: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    @property
    def config(self) -> RelayStateStoreConfig:
        return self._config

    def register(
        self,
        node_id: str,
        capabilities: ComputeNodeCapabilities,
        control_credential_digest: str,
    ) -> ComputeNodeRegistration:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        if not isinstance(capabilities, ComputeNodeCapabilities):
            raise RelayStateStoreError("capabilities must be ComputeNodeCapabilities")
        with self._lock:
            now = self._now()
            lease_deadline = self._lease_deadline(now)
            self._expire_locked(now)
            existing = self._records.get(node_id)
            if existing is None and len(self._records) >= self.config.max_compute_nodes:
                raise RelayStateCapacityExceeded(
                    "compute-node registration capacity reached"
                )
            if existing is not None:
                self._require_digest(existing, control_credential_digest)
            record = ComputeNodeRegistration(
                node_id=node_id,
                capabilities=capabilities,
                control_credential_digest=(
                    existing.control_credential_digest
                    if existing
                    else control_credential_digest
                ),
                registered_at_epoch=(existing.registered_at_epoch if existing else now),
                lease_expires_at_epoch=lease_deadline,
            )
            self._records[node_id] = record
            return replace(record)

    def renew(
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        capabilities: ComputeNodeCapabilities | None = None,
    ) -> ComputeNodeRegistration | None:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
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
            self._require_digest(existing, control_credential_digest)
            lease_deadline = self._lease_deadline(now)
            renewed = replace(
                existing,
                capabilities=capabilities or existing.capabilities,
                lease_expires_at_epoch=lease_deadline,
            )
            self._records[node_id] = renewed
            return replace(renewed)

    def get(self, node_id: str) -> ComputeNodeRegistration | None:
        self._validate_node_id(node_id)
        with self._lock:
            self._expire_locked(self._now())
            record = self._records.get(node_id)
            return replace(record) if record else None

    def list(self) -> tuple[ComputeNodeRegistration, ...]:
        with self._lock:
            self._expire_locked(self._now())
            return tuple(
                replace(self._records[node_id]) for node_id in sorted(self._records)
            )

    def expire(self) -> tuple[ComputeNodeRegistration, ...]:
        with self._lock:
            return tuple(replace(record) for record in self._expire_locked(self._now()))

    def unregister(self, node_id: str, control_credential_digest: str) -> bool:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        with self._lock:
            self._expire_locked(self._now())
            existing = self._records.get(node_id)
            if existing is None:
                return False
            self._require_digest(existing, control_credential_digest)
            del self._records[node_id]
            return True

    def set_node_status(
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        healthy: bool,
        draining: bool,
    ) -> ComputeNodeRegistration | None:
        """Atomically update scheduler-only health flags."""
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        if not isinstance(healthy, bool) or not isinstance(draining, bool):
            raise RelayStateStoreError("health and draining flags must be booleans")
        with self._lock:
            self._expire_locked(self._now())
            record = self._records.get(node_id)
            if record is None:
                return None
            self._require_digest(record, control_credential_digest)
            updated = replace(record, healthy=healthy, draining=draining)
            self._records[node_id] = updated
            return replace(updated)

    def select_and_reserve(
        self,
        client_identity: str,
        request_identity: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
    ) -> SchedulerReservation:
        client, request = self._identity(client_identity, request_identity)
        model, tier = self._parameters(model_id, context_tier)
        with self._lock:
            now = self._now()
            deadline = self._deadline(deadline_epoch, now)
            self._expire_locked(now)
            self._expire_reservations_locked(now)
            key = (client, request)
            existing = self._reservations.get(key)
            if existing:
                if (
                    existing.model_id,
                    existing.context_tier,
                    existing.deadline_epoch,
                ) != (model, tier, deadline):
                    raise RelayStateConflict(
                        "request identity has conflicting selection parameters"
                    )
                # Digest-only storage deliberately cannot reconstruct the secret. A retry
                # returns safe metadata and requires the caller to retain its original token.
                return SchedulerReservation(
                    existing.node_id,
                    model,
                    tier,
                    deadline,
                    existing.expires_at_epoch,
                    None,
                    True,
                )
            if key in self._enqueued:
                raise RelayStateConflict("request identity is already queued")
            if len(self._reservations) >= self.config.max_reservations:
                raise RelayStateCapacityExceeded("reservation capacity reached")
            if (
                sum(r.client_digest == client for r in self._reservations.values())
                >= self.config.max_reservations_per_client
            ):
                raise RelayStateCapacityExceeded("client reservation capacity reached")
            requested_tokens = CONTEXT_TIER_TOKEN_BOUNDS[tier]
            candidates = []
            for position, record in enumerate(self._records.values()):
                caps = record.capabilities
                load = len(self._queues.get(record.node_id, ())) + sum(
                    r.node_id == record.node_id for r in self._reservations.values()
                )
                if (
                    record.healthy
                    and not record.draining
                    and model in caps.supported_model_ids
                    and caps.maximum_total_context_tokens >= requested_tokens
                    and load < caps.max_concurrency
                    and load < self.config.max_queue_depth_per_node
                    and sum(
                        r.node_id == record.node_id for r in self._reservations.values()
                    )
                    < self.config.max_reservations_per_node
                ):
                    candidates.append(
                        (
                            CONTEXT_TIER_TOKEN_BOUNDS[caps.active_context_tier],
                            load,
                            position,
                            record,
                        )
                    )
            if not candidates:
                raise RelayStateNoEligibleNode("no eligible compute node")
            minimum = min((x[0], x[1]) for x in candidates)
            tied = [x for x in candidates if x[:2] == minimum]
            fingerprint = (model, tier)
            anchor = self._cursors.get(fingerprint)
            if anchor and any(x[3].node_id == anchor for x in tied):
                index = next(i for i, x in enumerate(tied) if x[3].node_id == anchor)
                chosen = tied[(index + 1) % len(tied)][3]
            else:
                chosen = tied[0][3]
            token = secrets.token_urlsafe(32)
            expires = min(deadline, now + self.config.reservation_ttl_seconds)
            self._reservations[key] = _StoredReservation(
                hashlib.sha256(token.encode()).hexdigest(),
                client,
                request,
                chosen.node_id,
                model,
                tier,
                deadline,
                expires,
            )
            self._cursors[fingerprint] = chosen.node_id
            return SchedulerReservation(
                chosen.node_id, model, tier, deadline, expires, token
            )

    def consume_reservation_and_enqueue(
        self,
        client_identity: str,
        request_identity: str,
        reservation_token: str,
        node_id: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult:
        client, request = self._identity(client_identity, request_identity)
        model, tier = self._parameters(model_id, context_tier)
        if not isinstance(envelope, EncryptedRequestEnvelope):
            raise RelayStateStoreError("envelope must be EncryptedRequestEnvelope")
        envelope_size = sum(
            len(v.encode())
            for v in (
                envelope.protocol,
                envelope.version,
                envelope.ciphertext,
                envelope.cipherkey,
                envelope.iv,
            )
        )
        if envelope_size > self.config.max_envelope_bytes:
            raise RelayStateCapacityExceeded("encrypted envelope byte bound exceeded")
        if not isinstance(reservation_token, str) or not reservation_token:
            raise RelayStateReservationRejected("reservation rejected")
        fingerprint = hashlib.sha256(
            repr((node_id, model, tier, deadline_epoch, envelope)).encode()
        ).hexdigest()
        with self._lock:
            now = self._now()
            deadline = self._deadline(deadline_epoch, now)
            self._expire_locked(now)
            self._expire_reservations_locked(now)
            key = (client, request)
            prior = self._enqueued.get(key)
            if prior:
                if hmac.compare_digest(prior[0], fingerprint):
                    return replace(prior[1], retry=True)
                raise RelayStateConflict(
                    "request identity has conflicting enqueue parameters"
                )
            reservation = self._reservations.get(key)
            supplied = hashlib.sha256(reservation_token.encode()).hexdigest()
            if (
                reservation is None
                or not hmac.compare_digest(reservation.token_digest, supplied)
                or (
                    reservation.node_id,
                    reservation.model_id,
                    reservation.context_tier,
                    reservation.deadline_epoch,
                )
                != (node_id, model, tier, deadline)
            ):
                raise RelayStateReservationRejected("reservation rejected")
            queue = self._queues.setdefault(node_id, [])
            if len(queue) >= self.config.max_queue_depth_per_node:
                raise RelayStateCapacityExceeded("node queue capacity reached")
            item = QueuedEncryptedRequest(
                client, request, node_id, model, tier, deadline, envelope, now
            )
            result = EnqueueResult(node_id, client, request, now)
            queue.append(item)
            del self._reservations[key]
            self._enqueued[key] = (fingerprint, result)
            return result

    def queued(self, node_id: str) -> tuple[QueuedEncryptedRequest, ...]:
        self._validate_node_id(node_id)
        with self._lock:
            return tuple(replace(item) for item in self._queues.get(node_id, ()))

    def _expire_locked(self, now: float) -> list[ComputeNodeRegistration]:
        expired = sorted(
            (
                record
                for record in self._records.values()
                if record.lease_expires_at_epoch <= now
            ),
            key=lambda record: record.node_id,
        )
        for record in expired:
            del self._records[record.node_id]
        return expired

    def _expire_reservations_locked(self, now: float) -> None:
        for key, reservation in tuple(self._reservations.items()):
            if reservation.expires_at_epoch <= now or reservation.deadline_epoch <= now:
                del self._reservations[key]

    def _identity(self, client_identity: str, request_identity: str) -> tuple[str, str]:
        values = []
        for value in (client_identity, request_identity):
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > self.config.max_identity_bytes
            ):
                raise RelayStateStoreError(
                    "identity is empty or exceeds its configured byte bound"
                )
            values.append(hashlib.sha256(value.encode()).hexdigest())
        return values[0], values[1]

    def _parameters(self, model_id: str, context_tier: str) -> tuple[str, str]:
        if (
            not isinstance(model_id, str)
            or not model_id.strip()
            or len(model_id.strip().encode()) > self.config.max_model_bytes
        ):
            raise RelayStateStoreError(
                "model is empty or exceeds its configured byte bound"
            )
        model = model_id.strip().lower()
        tier = context_tier.strip().lower() if isinstance(context_tier, str) else ""
        if tier not in CONTEXT_TIER_TOKEN_BOUNDS:
            raise RelayStateStoreError("unsupported requested context tier")
        return model, tier

    @staticmethod
    def _deadline(value: float, now: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= now
        ):
            raise RelayStateReservationRejected(
                "request deadline is expired or invalid"
            )
        return float(value)

    def _now(self) -> float:
        value = self._epoch_time()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RelayStateStoreError("epoch clock must return a finite number")
        return float(value)

    def _lease_deadline(self, now: float) -> float:
        deadline = now + self.config.lease_ttl_seconds
        if not math.isfinite(deadline):
            raise RelayStateStoreError("lease deadline must be finite")
        return deadline

    def _validate_node_id(self, node_id: str) -> None:
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id.encode("utf-8")) > self.config.max_node_id_bytes
        ):
            raise RelayStateStoreError(
                "node ID is empty or exceeds its configured byte bound"
            )

    @staticmethod
    def _validate_digest(digest: str) -> None:
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise RelayStateStoreError(
                "control credential digest must be lowercase SHA-256 hex"
            )

    @staticmethod
    def _require_digest(record: ComputeNodeRegistration, digest: str) -> None:
        if not hmac.compare_digest(record.control_credential_digest, digest):
            raise RelayStateCredentialMismatch(
                "control credential digest does not own this registration"
            )
