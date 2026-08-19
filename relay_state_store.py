"""Typed state-store boundary for compute-node registrations and leases.

This module is deliberately not wired into the relay runtime yet.  It defines the
small registration/lease contract that future shared backends must implement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
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
    """Raised when an identity is retried with different immutable parameters."""


class RelayStateReservationRejected(RelayStateStoreError):
    """Raised when a reservation proof cannot authorize an enqueue."""


class RelayStateNoEligibleNode(RelayStateStoreError):
    """Raised when no compatible node has admission capacity."""


@dataclass(frozen=True, slots=True)
class RelayStateStoreConfig:
    """Explicit key-space, expiry, and size policy shared by all backends."""

    namespace: str
    schema_version: int = RELAY_STATE_SCHEMA_VERSION
    lease_ttl_seconds: float = 30.0
    max_compute_nodes: int = 1024
    max_node_id_bytes: int = 8192
    reservation_ttl_seconds: float = 15.0
    max_reservations: int = 4096
    max_reservations_per_client: int = 16
    max_reservations_per_node: int = 128
    max_queue_depth_per_node: int = 128
    max_scheduler_fingerprints: int = 1024
    max_request_identities: int = 100_000
    max_identity_bytes: int = 8192
    max_model_id_bytes: int = 128
    max_context_tier_bytes: int = 32
    max_request_deadline_seconds: float = 3600.0
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
            ("per-client reservation bound", self.max_reservations_per_client, 10_000),
            ("per-node reservation bound", self.max_reservations_per_node, 10_000),
            ("queue-depth bound", self.max_queue_depth_per_node, 1_000_000),
            ("scheduler-fingerprint bound", self.max_scheduler_fingerprints, 1_000_000),
            ("request-identity bound", self.max_request_identities, 1_000_000),
            ("identity bound", self.max_identity_bytes, 65_536),
            ("model-id bound", self.max_model_id_bytes, 4096),
            ("context-tier bound", self.max_context_tier_bytes, 256),
            ("envelope bound", self.max_envelope_bytes, 64 * 1024 * 1024),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise RelayStateStoreError(
                    f"{name} must be an integer between 1 and {maximum}"
                )
        for name, value, maximum in (
            ("reservation TTL", self.reservation_ttl_seconds, 300.0),
            ("request deadline", self.max_request_deadline_seconds, 86_400.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 < value <= maximum
            ):
                raise RelayStateStoreError(
                    f"{name} must be a finite positive bounded number"
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


@dataclass(frozen=True, slots=True)
class EncryptedRequestEnvelope:
    """The complete API-v1 ciphertext allowlist stored by the relay."""

    protocol: str
    version: int
    ciphertext: str
    cipherkey: str
    iv: str

    def __post_init__(self) -> None:
        if self.protocol != "tokenplace_api_v1_relay_e2ee" or self.version != 1:
            raise RelayStateStoreError("encrypted envelope must use relay E2EE API v1")
        for value in (self.ciphertext, self.cipherkey, self.iv):
            if not isinstance(value, str) or not value:
                raise RelayStateStoreError(
                    "encrypted envelope fields must be non-empty strings"
                )


@dataclass(frozen=True, slots=True)
class SchedulerReservation:
    """Safe reservation view; the raw token is returned only on first creation.

    An idempotent selection retry returns ``reservation_token=None``.  The caller
    must retain the first response; a digest-only backend cannot reconstruct a
    lost bearer token without violating the token-storage invariant.
    """

    client_identity_digest: str
    request_identity_digest: str
    selected_node_id: str
    requested_model: str
    context_tier: str
    request_deadline_epoch: float
    reservation_expires_at_epoch: float
    reservation_token: str | None = None
    created: bool = True

    def __repr__(self) -> str:
        return (
            "SchedulerReservation(client_identity_digest="
            f"{self.client_identity_digest!r}, request_identity_digest="
            f"{self.request_identity_digest!r}, selected_node_id={self.selected_node_id!r}, "
            f"requested_model={self.requested_model!r}, context_tier={self.context_tier!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, "
            f"reservation_expires_at_epoch={self.reservation_expires_at_epoch!r}, "
            f"created={self.created!r})"
        )


@dataclass(frozen=True, slots=True)
class QueuedEncryptedRequest:
    client_identity_digest: str
    request_identity_digest: str
    selected_node_id: str
    requested_model: str
    context_tier: str
    request_deadline_epoch: float
    envelope: EncryptedRequestEnvelope
    queued_at_epoch: float


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    request: QueuedEncryptedRequest
    created: bool


@dataclass(frozen=True, slots=True)
class _StoredReservation:
    identity: tuple[str, str]
    node_id: str
    model: str
    tier: str
    request_deadline: float
    expires_at: float
    token_digest: str


@dataclass(frozen=True, slots=True)
class _Lifecycle:
    selection_parameters: tuple[str, str, float]
    reservation_digest: str | None = None
    queued: QueuedEncryptedRequest | None = None
    envelope_digest: str | None = None


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
    def set_scheduler_status(
        self, node_id: str, *, healthy: bool, draining: bool, claimed_work: int = 0
    ) -> None: ...
    def select_and_reserve(
        self,
        client_public_key: str,
        request_identity: str,
        *,
        requested_model: str,
        context_tier: str,
        request_deadline_epoch: float,
    ) -> SchedulerReservation: ...
    def consume_reservation_and_enqueue(
        self,
        client_public_key: str,
        request_identity: str,
        *,
        reservation_token: str,
        selected_node_id: str,
        requested_model: str,
        context_tier: str,
        request_deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult: ...
    def list_queued(self, node_id: str) -> tuple[QueuedEncryptedRequest, ...]: ...


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
        self._scheduler_status: dict[str, tuple[bool, bool, int]] = {}
        self._reservations: dict[str, _StoredReservation] = {}
        self._lifecycles: dict[tuple[str, str], _Lifecycle] = {}
        self._queues: dict[str, list[QueuedEncryptedRequest]] = {}
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
            self._scheduler_status.pop(node_id, None)
            return True

    def set_scheduler_status(
        self, node_id: str, *, healthy: bool, draining: bool, claimed_work: int = 0
    ) -> None:
        self._validate_node_id(node_id)
        if not isinstance(healthy, bool) or not isinstance(draining, bool):
            raise RelayStateStoreError(
                "scheduler health and draining values must be boolean"
            )
        if (
            isinstance(claimed_work, bool)
            or not isinstance(claimed_work, int)
            or claimed_work < 0
        ):
            raise RelayStateStoreError("claimed work must be a non-negative integer")
        with self._lock:
            self._expire_all_locked(self._now())
            if node_id not in self._records:
                raise RelayStateStoreError("scheduler node is not registered")
            self._scheduler_status[node_id] = (healthy, draining, claimed_work)

    def select_and_reserve(
        self,
        client_public_key: str,
        request_identity: str,
        *,
        requested_model: str,
        context_tier: str,
        request_deadline_epoch: float,
    ) -> SchedulerReservation:
        identity = self._canonical_identity(client_public_key, request_identity)
        model, tier, deadline = self._selection_parameters(
            requested_model, context_tier, request_deadline_epoch
        )
        parameters = (model, tier, deadline)
        with self._lock:
            now = self._now()
            self._expire_all_locked(now)
            if deadline <= now:
                raise RelayStateReservationRejected("request deadline has expired")
            if deadline - now > self.config.max_request_deadline_seconds:
                raise RelayStateStoreError(
                    "request deadline exceeds its configured bound"
                )
            lifecycle = self._lifecycles.get(identity)
            if lifecycle is not None:
                if lifecycle.selection_parameters != parameters:
                    raise RelayStateConflict(
                        "request identity has conflicting selection parameters"
                    )
                if lifecycle.reservation_digest is None:
                    raise RelayStateReservationRejected(
                        "request identity cannot be reserved again"
                    )
                stored = self._reservations[lifecycle.reservation_digest]
                return self._reservation_view(stored, token=None, created=False)
            if len(self._reservations) >= self.config.max_reservations:
                raise RelayStateCapacityExceeded("reservation capacity reached")
            if len(self._lifecycles) >= self.config.max_request_identities:
                raise RelayStateCapacityExceeded("request identity capacity reached")
            client_count = sum(
                r.identity[0] == identity[0] for r in self._reservations.values()
            )
            if client_count >= self.config.max_reservations_per_client:
                raise RelayStateCapacityExceeded("client reservation capacity reached")
            eligible = self._eligible_nodes(model, tier)
            if not eligible:
                raise RelayStateNoEligibleNode("no eligible compute node")
            smallest = min(
                CONTEXT_TIER_TOKEN_BOUNDS[r.capabilities.active_context_tier]
                for r in eligible
            )
            eligible = [
                r
                for r in eligible
                if CONTEXT_TIER_TOKEN_BOUNDS[r.capabilities.active_context_tier]
                == smallest
            ]
            loads = {r.node_id: self._node_load(r.node_id) for r in eligible}
            least = min(loads.values())
            eligible = [r for r in eligible if loads[r.node_id] == least]
            fingerprint = (model, tier)
            if (
                fingerprint not in self._cursors
                and len(self._cursors) >= self.config.max_scheduler_fingerprints
            ):
                raise RelayStateCapacityExceeded(
                    "scheduler fingerprint capacity reached"
                )
            ordered = [r.node_id for r in self._records.values() if r in eligible]
            cursor = self._cursors.get(fingerprint)
            selected_index = (
                0
                if cursor not in ordered
                else (ordered.index(cursor) + 1) % len(ordered)
            )
            node_id = ordered[selected_index]
            raw_token = secrets.token_urlsafe(32)
            token_digest = self._digest(raw_token)
            expires = min(deadline, now + self.config.reservation_ttl_seconds)
            stored = _StoredReservation(
                identity, node_id, model, tier, deadline, expires, token_digest
            )
            self._reservations[token_digest] = stored
            self._lifecycles[identity] = _Lifecycle(parameters, token_digest)
            self._cursors[fingerprint] = node_id
            return self._reservation_view(stored, token=raw_token, created=True)

    def consume_reservation_and_enqueue(
        self,
        client_public_key: str,
        request_identity: str,
        *,
        reservation_token: str,
        selected_node_id: str,
        requested_model: str,
        context_tier: str,
        request_deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult:
        identity = self._canonical_identity(client_public_key, request_identity)
        model, tier, deadline = self._selection_parameters(
            requested_model, context_tier, request_deadline_epoch
        )
        if not isinstance(envelope, EncryptedRequestEnvelope):
            raise RelayStateStoreError("envelope must be EncryptedRequestEnvelope")
        envelope_digest = self._envelope_digest(envelope)
        with self._lock:
            now = self._now()
            self._expire_all_locked(now)
            if deadline - now > self.config.max_request_deadline_seconds:
                raise RelayStateStoreError(
                    "request deadline exceeds its configured bound"
                )
            lifecycle = self._lifecycles.get(identity)
            parameters = (model, tier, deadline)
            if lifecycle is not None and lifecycle.queued is not None:
                if (
                    lifecycle.selection_parameters == parameters
                    and lifecycle.queued.selected_node_id == selected_node_id
                    and hmac.compare_digest(
                        lifecycle.envelope_digest or "", envelope_digest
                    )
                ):
                    return EnqueueResult(replace(lifecycle.queued), False)
                raise RelayStateConflict(
                    "request identity has conflicting enqueue parameters"
                )
            if lifecycle is None or lifecycle.selection_parameters != parameters:
                raise RelayStateReservationRejected(
                    "reservation does not match request identity"
                )
            if not isinstance(reservation_token, str) or not reservation_token:
                raise RelayStateReservationRejected("invalid reservation token")
            stored = self._reservations.get(self._digest(reservation_token))
            if (
                stored is None
                or stored.identity != identity
                or stored.node_id != selected_node_id
            ):
                raise RelayStateReservationRejected("invalid reservation token")
            if deadline <= now or stored.expires_at <= now:
                raise RelayStateReservationRejected(
                    "reservation or request deadline has expired"
                )
            if len(self._envelope_bytes(envelope)) > self.config.max_envelope_bytes:
                raise RelayStateCapacityExceeded(
                    "encrypted envelope byte bound exceeded"
                )
            queue = self._queues.setdefault(selected_node_id, [])
            if len(queue) >= self.config.max_queue_depth_per_node:
                raise RelayStateCapacityExceeded("compute-node queue capacity reached")
            record = QueuedEncryptedRequest(
                identity[0],
                identity[1],
                selected_node_id,
                model,
                tier,
                deadline,
                envelope,
                now,
            )
            queue.append(record)
            del self._reservations[stored.token_digest]
            self._lifecycles[identity] = _Lifecycle(
                parameters, None, record, envelope_digest
            )
            return EnqueueResult(replace(record), True)

    def list_queued(self, node_id: str) -> tuple[QueuedEncryptedRequest, ...]:
        self._validate_node_id(node_id)
        with self._lock:
            self._expire_all_locked(self._now())
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
            self._scheduler_status.pop(record.node_id, None)
        return expired

    def _expire_all_locked(self, now: float) -> None:
        self._expire_locked(now)
        for token_digest, reservation in tuple(self._reservations.items()):
            if (
                reservation.expires_at <= now
                or reservation.request_deadline <= now
                or reservation.node_id not in self._records
            ):
                del self._reservations[token_digest]
                lifecycle = self._lifecycles.get(reservation.identity)
                if (
                    lifecycle is not None
                    and lifecycle.reservation_digest == token_digest
                ):
                    self._lifecycles[reservation.identity] = replace(
                        lifecycle, reservation_digest=None
                    )
        for node_id, queue in tuple(self._queues.items()):
            live = [item for item in queue if item.request_deadline_epoch > now]
            if live:
                self._queues[node_id] = live
            else:
                self._queues.pop(node_id, None)

    def _eligible_nodes(self, model: str, tier: str) -> list[ComputeNodeRegistration]:
        required_tokens = CONTEXT_TIER_TOKEN_BOUNDS[tier]
        eligible = []
        for record in self._records.values():
            healthy, draining, _ = self._scheduler_status.get(
                record.node_id, (True, False, 0)
            )
            reservations = sum(
                r.node_id == record.node_id for r in self._reservations.values()
            )
            load = self._node_load(record.node_id)
            if (
                healthy
                and not draining
                and model in record.capabilities.supported_model_ids
                and CONTEXT_TIER_TOKEN_BOUNDS[record.capabilities.active_context_tier]
                >= required_tokens
                and reservations < self.config.max_reservations_per_node
                and load < record.capabilities.max_concurrency
                and load < self.config.max_queue_depth_per_node
            ):
                eligible.append(record)
        return eligible

    def _node_load(self, node_id: str) -> int:
        reservations = sum(r.node_id == node_id for r in self._reservations.values())
        queued = len(self._queues.get(node_id, ()))
        claimed = self._scheduler_status.get(node_id, (True, False, 0))[2]
        return reservations + queued + claimed

    def _canonical_identity(
        self, client_public_key: str, request_identity: str
    ) -> tuple[str, str]:
        for name, value in (
            ("client public key", client_public_key),
            ("request identity", request_identity),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > self.config.max_identity_bytes
            ):
                raise RelayStateStoreError(
                    f"{name} is empty or exceeds its configured byte bound"
                )
        return self._digest(client_public_key), self._digest(request_identity)

    def _selection_parameters(
        self, model: str, tier: str, deadline: float
    ) -> tuple[str, str, float]:
        if not isinstance(model, str) or not model.strip():
            raise RelayStateStoreError("requested model must be non-empty")
        normalized_model = model.strip().lower()
        if len(normalized_model.encode()) > self.config.max_model_id_bytes:
            raise RelayStateStoreError(
                "requested model exceeds its configured byte bound"
            )
        normalized_tier = tier.strip().lower() if isinstance(tier, str) else ""
        if (
            len(normalized_tier.encode()) > self.config.max_context_tier_bytes
            or normalized_tier not in CONTEXT_TIER_TOKEN_BOUNDS
        ):
            raise RelayStateStoreError("unsupported requested context tier")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise RelayStateStoreError("request deadline must be a finite UTC epoch")
        return normalized_model, normalized_tier, float(deadline)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _envelope_bytes(envelope: EncryptedRequestEnvelope) -> bytes:
        return json.dumps(
            {
                "protocol": envelope.protocol,
                "version": envelope.version,
                "ciphertext": envelope.ciphertext,
                "cipherkey": envelope.cipherkey,
                "iv": envelope.iv,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def _envelope_digest(self, envelope: EncryptedRequestEnvelope) -> str:
        return hashlib.sha256(self._envelope_bytes(envelope)).hexdigest()

    @staticmethod
    def _reservation_view(
        stored: _StoredReservation, *, token: str | None, created: bool
    ) -> SchedulerReservation:
        return SchedulerReservation(
            stored.identity[0],
            stored.identity[1],
            stored.node_id,
            stored.model,
            stored.tier,
            stored.request_deadline,
            stored.expires_at,
            token,
            created,
        )

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
