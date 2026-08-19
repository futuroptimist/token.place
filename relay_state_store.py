"""Typed state-store boundary for relay coordination transitions.

This module is deliberately not wired into the relay runtime yet.  It defines the
memory-only contract that future shared backends must implement.
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
    """A canonical request identity was reused with different parameters."""


class RelayStateNoCapacity(RelayStateStoreError):
    """No live compatible node has bounded scheduler capacity."""


class RelayStateInvalidReservation(RelayStateStoreError):
    """A reservation token is invalid, expired, consumed, or wrongly bound."""


@dataclass(frozen=True, slots=True)
class RelayStateStoreConfig:
    """Explicit key-space, expiry, and size policy shared by all backends."""

    namespace: str
    schema_version: int = RELAY_STATE_SCHEMA_VERSION
    lease_ttl_seconds: float = 30.0
    max_compute_nodes: int = 1024
    max_node_id_bytes: int = 8192
    reservation_ttl_seconds: float = 15.0
    max_request_ttl_seconds: float = 3600.0
    max_reservations: int = 4096
    max_reservations_per_client: int = 8
    max_reservations_per_node: int = 128
    max_queue_depth_per_node: int = 128
    max_identity_bytes: int = 8192
    max_model_id_bytes: int = 128
    max_scheduler_fingerprints: int = 4096
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
        self._validate_float_bound(
            self.reservation_ttl_seconds, "reservation TTL", 0.001, 300.0
        )
        self._validate_float_bound(
            self.max_request_ttl_seconds, "request TTL", 0.001, 86_400.0
        )
        for value, name, maximum in (
            (self.max_reservations, "reservation bound", 1_000_000),
            (self.max_reservations_per_client, "per-client reservation bound", 10_000),
            (self.max_reservations_per_node, "per-node reservation bound", 10_000),
            (self.max_queue_depth_per_node, "per-node queue bound", 1_000_000),
            (self.max_identity_bytes, "identity byte bound", 65_536),
            (self.max_model_id_bytes, "model-id byte bound", 1024),
            (self.max_scheduler_fingerprints, "scheduler fingerprint bound", 1_000_000),
            (
                self.max_envelope_bytes,
                "encrypted-envelope byte bound",
                64 * 1024 * 1024,
            ),
        ):
            self._validate_int_bound(value, name, maximum)

    @staticmethod
    def _validate_int_bound(value: int, name: str, maximum: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise RelayStateStoreError(
                f"{name} must be an integer between 1 and {maximum}"
            )

    @staticmethod
    def _validate_float_bound(
        value: float, name: str, minimum: float, maximum: float
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise RelayStateStoreError(
                f"{name} must be finite and between {minimum} and {maximum}"
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
class SchedulerNodeState:
    """Mutable scheduler facts kept separately from immutable capabilities."""

    healthy: bool = True
    draining: bool = False
    claimed_work: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.healthy, bool) or not isinstance(self.draining, bool):
            raise RelayStateStoreError("scheduler health flags must be booleans")
        if (
            isinstance(self.claimed_work, bool)
            or not isinstance(self.claimed_work, int)
            or not 0 <= self.claimed_work <= 1_000_000
        ):
            raise RelayStateStoreError(
                "claimed work must be an integer between 0 and 1,000,000"
            )


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    """Stored reservation. Only the opaque token's SHA-256 digest is retained."""

    client_identity_digest: str
    request_identity_digest: str
    scheduler_fingerprint: str
    selected_node_id: str
    requested_model_id: str
    requested_context_tier: str
    request_deadline_epoch: float
    reservation_expires_at_epoch: float
    token_digest: str


@dataclass(frozen=True, slots=True, repr=False)
class SelectionResult:
    """Safe selection result.

    ``reservation_token`` is returned only when this call created the reservation.
    An idempotent retry returns the same metadata with ``None``: the store cannot
    reconstruct a raw token from the only persisted value, its digest. Callers must
    retain the first token or retry selection after the short reservation expiry.
    Once a request is queued, ``reservation_expires_at_epoch`` reports its stable
    enqueue time rather than implying that a consumed reservation remains valid.
    """

    selected_node_id: str
    requested_model_id: str
    requested_context_tier: str
    request_deadline_epoch: float
    reservation_expires_at_epoch: float
    reservation_token: str | None
    created: bool

    def __repr__(self) -> str:
        return (
            "SelectionResult(selected_node_id=<redacted>, "
            f"requested_model_id={self.requested_model_id!r}, "
            f"requested_context_tier={self.requested_context_tier!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, "
            f"reservation_expires_at_epoch={self.reservation_expires_at_epoch!r}, "
            f"token_present={self.reservation_token is not None}, created={self.created!r})"
        )


@dataclass(frozen=True, slots=True)
class EncryptedRequestEnvelope:
    """Exact API-v1 relay-blind ciphertext allowlist."""

    protocol: str
    version: int
    ciphertext: str
    cipherkey: str
    iv: str

    def __post_init__(self) -> None:
        if self.protocol != "tokenplace_api_v1_relay_e2ee":
            raise RelayStateStoreError("unsupported encrypted-envelope protocol")
        if isinstance(self.version, bool) or self.version != 1:
            raise RelayStateStoreError("unsupported encrypted-envelope version")
        for value in (self.ciphertext, self.cipherkey, self.iv):
            if not isinstance(value, str) or not value:
                raise RelayStateStoreError(
                    "encrypted-envelope values must be non-empty strings"
                )


@dataclass(frozen=True, slots=True)
class QueuedRequest:
    """Immutable relay-blind queued request record."""

    client_identity_digest: str
    request_identity_digest: str
    selected_node_id: str
    requested_model_id: str
    requested_context_tier: str
    request_deadline_epoch: float
    envelope: EncryptedRequestEnvelope
    enqueued_at_epoch: float
    sequence: int


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Safe idempotent lifecycle result."""

    state: str
    selected_node_id: str
    request_deadline_epoch: float
    sequence: int
    created: bool


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
    def set_scheduler_state(
        self, node_id: str, control_credential_digest: str, state: SchedulerNodeState
    ) -> bool: ...
    def select_and_reserve(
        self,
        client_public_key: str,
        request_id: str,
        requested_model_id: str,
        requested_context_tier: str,
        request_deadline_epoch: float,
    ) -> SelectionResult: ...
    def enqueue_encrypted_request(
        self,
        client_public_key: str,
        request_id: str,
        reservation_token: str,
        selected_node_id: str,
        requested_model_id: str,
        requested_context_tier: str,
        request_deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult: ...
    def list_reservations(self) -> tuple[ReservationRecord, ...]: ...
    def queued_requests(self, node_id: str) -> tuple[QueuedRequest, ...]: ...


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
        self._scheduler_states: dict[str, SchedulerNodeState] = {}
        self._registration_order: dict[str, int] = {}
        self._next_registration_order = 0
        self._reservations: dict[tuple[str, str], ReservationRecord] = {}
        self._queued: dict[tuple[str, str], QueuedRequest] = {}
        self._node_queues: dict[str, list[QueuedRequest]] = {}
        self._fairness_cursors: dict[str, str] = {}
        self._next_queue_sequence = 0
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
            if existing is None:
                self._scheduler_states[node_id] = SchedulerNodeState()
                self._registration_order[node_id] = self._next_registration_order
                self._next_registration_order += 1
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
            self._scheduler_states.pop(node_id, None)
            self._registration_order.pop(node_id, None)
            self._remove_node_reservations_locked(node_id)
            self._remove_node_queue_locked(node_id)
            self._reap_fairness_cursors_locked()
            return True

    def set_scheduler_state(
        self, node_id: str, control_credential_digest: str, state: SchedulerNodeState
    ) -> bool:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        if not isinstance(state, SchedulerNodeState):
            raise RelayStateStoreError("scheduler state must be SchedulerNodeState")
        with self._lock:
            self._expire_locked(self._now())
            record = self._records.get(node_id)
            if record is None:
                return False
            self._require_digest(record, control_credential_digest)
            self._scheduler_states[node_id] = state
            return True

    def select_and_reserve(
        self,
        client_public_key: str,
        request_id: str,
        requested_model_id: str,
        requested_context_tier: str,
        request_deadline_epoch: float,
    ) -> SelectionResult:
        client_digest, request_digest = self._identity(client_public_key, request_id)
        model_id = self._model_id(requested_model_id)
        tier = self._context_tier(requested_context_tier)
        deadline = self._deadline(request_deadline_epoch)
        fingerprint = self._scheduler_fingerprint(model_id, tier)
        identity = (client_digest, request_digest)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            if deadline <= now:
                raise RelayStateInvalidReservation("request deadline expired")
            if deadline > now + self.config.max_request_ttl_seconds:
                raise RelayStateStoreError(
                    "request deadline exceeds its configured bound"
                )
            queued = self._queued.get(identity)
            if queued is not None:
                self._require_same_parameters(queued, model_id, tier, deadline)
                return SelectionResult(
                    queued.selected_node_id,
                    model_id,
                    tier,
                    deadline,
                    queued.enqueued_at_epoch,
                    None,
                    False,
                )
            existing = self._reservations.get(identity)
            if existing is not None:
                self._require_same_parameters(existing, model_id, tier, deadline)
                return self._selection_result(existing, None, False)
            if len(self._reservations) >= self.config.max_reservations:
                raise RelayStateNoCapacity("no scheduler capacity")
            client_lifecycle_count = sum(
                item.client_identity_digest == client_digest
                for item in self._reservations.values()
            ) + sum(
                item.client_identity_digest == client_digest
                for item in self._queued.values()
            )
            if client_lifecycle_count >= self.config.max_reservations_per_client:
                raise RelayStateNoCapacity("no scheduler capacity")

            eligible: list[tuple[int, int, int, str]] = []
            requested_tokens = CONTEXT_TIER_TOKEN_BOUNDS[tier]
            reservation_counts: dict[str, int] = {}
            for reservation in self._reservations.values():
                node_id = reservation.selected_node_id
                reservation_counts[node_id] = reservation_counts.get(node_id, 0) + 1
            for node_id, record in self._records.items():
                caps = record.capabilities
                state = self._scheduler_states[node_id]
                if (
                    model_id not in caps.supported_model_ids
                    or CONTEXT_TIER_TOKEN_BOUNDS[caps.active_context_tier]
                    < requested_tokens
                    or caps.maximum_total_context_tokens < requested_tokens
                    or not state.healthy
                    or state.draining
                ):
                    continue
                reservations = reservation_counts.get(node_id, 0)
                queued_count = len(self._node_queues.get(node_id, ()))
                load = reservations + queued_count + state.claimed_work
                if (
                    load >= caps.max_concurrency
                    or reservations >= self.config.max_reservations_per_node
                    or reservations + queued_count
                    >= self.config.max_queue_depth_per_node
                ):
                    continue
                tier_size = CONTEXT_TIER_TOKEN_BOUNDS[caps.active_context_tier]
                eligible.append(
                    (tier_size, load, self._registration_order[node_id], node_id)
                )
            if not eligible:
                raise RelayStateNoCapacity("no scheduler capacity")
            smallest_tier = min(item[0] for item in eligible)
            eligible = [item for item in eligible if item[0] == smallest_tier]
            least_load = min(item[1] for item in eligible)
            tied = sorted(
                (item for item in eligible if item[1] == least_load), key=lambda x: x[2]
            )
            cursor = self._fairness_cursors.get(fingerprint)
            selected = tied[0]
            if cursor is not None:
                cursor_order = self._registration_order.get(cursor, -1)
                selected = next(
                    (item for item in tied if item[2] > cursor_order), tied[0]
                )
            node_id = selected[3]
            raw_token = secrets.token_hex(32)
            expires = min(now + self.config.reservation_ttl_seconds, deadline)
            if not math.isfinite(expires):
                raise RelayStateStoreError("reservation deadline must be finite")
            record = ReservationRecord(
                client_digest,
                request_digest,
                fingerprint,
                node_id,
                model_id,
                tier,
                deadline,
                expires,
                hashlib.sha256(raw_token.encode("ascii")).hexdigest(),
            )
            self._reservations[identity] = record
            if (
                fingerprint not in self._fairness_cursors
                and len(self._fairness_cursors)
                >= self.config.max_scheduler_fingerprints
            ):
                del self._reservations[identity]
                raise RelayStateNoCapacity("no scheduler capacity")
            self._fairness_cursors[fingerprint] = node_id
            return self._selection_result(record, raw_token, True)

    def enqueue_encrypted_request(
        self,
        client_public_key: str,
        request_id: str,
        reservation_token: str,
        selected_node_id: str,
        requested_model_id: str,
        requested_context_tier: str,
        request_deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult:
        client_digest, request_digest = self._identity(client_public_key, request_id)
        self._validate_node_id(selected_node_id)
        model_id = self._model_id(requested_model_id)
        tier = self._context_tier(requested_context_tier)
        deadline = self._deadline(request_deadline_epoch)
        if not isinstance(envelope, EncryptedRequestEnvelope):
            raise RelayStateStoreError("envelope must be EncryptedRequestEnvelope")
        self._validate_envelope_size(envelope)
        token_digest = self._token_digest(reservation_token)
        identity = (client_digest, request_digest)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            existing = self._queued.get(identity)
            if existing is not None:
                self._require_same_parameters(existing, model_id, tier, deadline)
                if (
                    existing.selected_node_id != selected_node_id
                    or existing.envelope != envelope
                ):
                    raise RelayStateConflict("request identity conflict")
                return self._enqueue_result(existing, False)
            if deadline <= now:
                raise RelayStateInvalidReservation("reservation invalid")
            if deadline > now + self.config.max_request_ttl_seconds:
                raise RelayStateStoreError(
                    "request deadline exceeds its configured bound"
                )
            reservation = self._reservations.get(identity)
            if reservation is None:
                raise RelayStateInvalidReservation("reservation invalid")
            self._require_same_parameters(reservation, model_id, tier, deadline)
            if (
                reservation.selected_node_id != selected_node_id
                or not hmac.compare_digest(reservation.token_digest, token_digest)
            ):
                raise RelayStateInvalidReservation("reservation invalid")
            if (
                len(self._node_queues.get(selected_node_id, ()))
                >= self.config.max_queue_depth_per_node
            ):
                raise RelayStateNoCapacity("no scheduler capacity")
            self._next_queue_sequence += 1
            queued = QueuedRequest(
                client_digest,
                request_digest,
                selected_node_id,
                model_id,
                tier,
                deadline,
                envelope,
                now,
                self._next_queue_sequence,
            )
            self._queued[identity] = queued
            self._node_queues.setdefault(selected_node_id, []).append(queued)
            del self._reservations[identity]
            return self._enqueue_result(queued, True)

    def list_reservations(self) -> tuple[ReservationRecord, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(replace(record) for record in self._reservations.values())

    def queued_requests(self, node_id: str) -> tuple[QueuedRequest, ...]:
        self._validate_node_id(node_id)
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(record) for record in self._node_queues.get(node_id, ())
            )

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
            self._scheduler_states.pop(record.node_id, None)
            self._registration_order.pop(record.node_id, None)
            self._remove_node_reservations_locked(record.node_id)
            self._remove_node_queue_locked(record.node_id)
        if expired:
            self._reap_fairness_cursors_locked()
        return expired

    def _reap_locked(self, now: float) -> None:
        self._expire_locked(now)
        for identity, reservation in tuple(self._reservations.items()):
            if (
                reservation.reservation_expires_at_epoch <= now
                or reservation.request_deadline_epoch <= now
            ):
                del self._reservations[identity]
        for identity, queued in tuple(self._queued.items()):
            if queued.request_deadline_epoch <= now:
                del self._queued[identity]
                queue = self._node_queues.get(queued.selected_node_id, [])
                self._node_queues[queued.selected_node_id] = [
                    item for item in queue if item != queued
                ]
        self._reap_fairness_cursors_locked()

    def _remove_node_reservations_locked(self, node_id: str) -> None:
        for identity, reservation in tuple(self._reservations.items()):
            if reservation.selected_node_id == node_id:
                del self._reservations[identity]

    def _remove_node_queue_locked(self, node_id: str) -> None:
        for queued in self._node_queues.pop(node_id, ()):
            identity = (
                queued.client_identity_digest,
                queued.request_identity_digest,
            )
            self._queued.pop(identity, None)

    def _reap_fairness_cursors_locked(self) -> None:
        active_fingerprints = {
            reservation.scheduler_fingerprint
            for reservation in self._reservations.values()
        }
        active_fingerprints.update(
            self._scheduler_fingerprint(
                queued.requested_model_id, queued.requested_context_tier
            )
            for queued in self._queued.values()
        )
        for fingerprint in tuple(self._fairness_cursors):
            if fingerprint not in active_fingerprints:
                del self._fairness_cursors[fingerprint]

    def _identity(self, client_public_key: str, request_id: str) -> tuple[str, str]:
        return (
            self._identity_digest(client_public_key, b"client\0"),
            self._identity_digest(request_id, b"request\0"),
        )

    def _identity_digest(self, value: str, domain: bytes) -> str:
        if not isinstance(value, str) or not value:
            raise RelayStateStoreError("request identity is invalid")
        encoded = value.encode("utf-8")
        if len(encoded) > self.config.max_identity_bytes:
            raise RelayStateStoreError("request identity is invalid")
        return hashlib.sha256(domain + encoded).hexdigest()

    def _model_id(self, value: str) -> str:
        if not isinstance(value, str):
            raise RelayStateStoreError("requested model is invalid")
        normalized = value.strip().lower()
        if (
            not normalized
            or len(normalized.encode("utf-8")) > self.config.max_model_id_bytes
        ):
            raise RelayStateStoreError("requested model is invalid")
        return normalized

    @staticmethod
    def _context_tier(value: str) -> str:
        normalized = value.strip().lower() if isinstance(value, str) else ""
        if normalized not in CONTEXT_TIER_TOKEN_BOUNDS:
            raise RelayStateStoreError("requested context tier is invalid")
        return normalized

    @staticmethod
    def _deadline(value: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RelayStateStoreError("request deadline must be finite")
        return float(value)

    @staticmethod
    def _scheduler_fingerprint(model_id: str, tier: str) -> str:
        return hashlib.sha256(f"{model_id}\0{tier}".encode()).hexdigest()

    @staticmethod
    def _require_same_parameters(
        record: object, model_id: str, tier: str, deadline: float
    ) -> None:
        if (
            getattr(record, "requested_model_id") != model_id
            or getattr(record, "requested_context_tier") != tier
            or getattr(record, "request_deadline_epoch") != deadline
        ):
            raise RelayStateConflict("request identity conflict")

    @staticmethod
    def _selection_result(
        record: ReservationRecord, token: str | None, created: bool
    ) -> SelectionResult:
        return SelectionResult(
            record.selected_node_id,
            record.requested_model_id,
            record.requested_context_tier,
            record.request_deadline_epoch,
            record.reservation_expires_at_epoch,
            token,
            created,
        )

    @staticmethod
    def _enqueue_result(record: QueuedRequest, created: bool) -> EnqueueResult:
        return EnqueueResult(
            "queued",
            record.selected_node_id,
            record.request_deadline_epoch,
            record.sequence,
            created,
        )

    def _validate_envelope_size(self, envelope: EncryptedRequestEnvelope) -> None:
        serialized = json.dumps(
            {
                "protocol": envelope.protocol,
                "version": envelope.version,
                "ciphertext": envelope.ciphertext,
                "cipherkey": envelope.cipherkey,
                "iv": envelope.iv,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(serialized) > self.config.max_envelope_bytes:
            raise RelayStateStoreError(
                "encrypted envelope exceeds its configured byte bound"
            )

    @staticmethod
    def _token_digest(token: str) -> str:
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise RelayStateInvalidReservation("reservation invalid")
        return hashlib.sha256(token.encode("ascii")).hexdigest()

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
