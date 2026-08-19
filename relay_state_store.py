"""Typed, backend-neutral relay coordination state transitions.

The in-memory implementation is deliberately not wired into ``relay.py``.  Raw
identity values are accepted only at the transition boundary and canonicalized
to digests; encrypted application content is constrained to the API-v1 E2EE
envelope.
"""

from __future__ import annotations

import hashlib
import hmac
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
    """Raised when a configured admission bound is reached."""


class RelayStateCredentialMismatch(RelayStateStoreError):
    """Raised when a live registration is addressed with the wrong digest."""


class RelayStateConflict(RelayStateStoreError):
    """Raised for a fixed identity whose immutable request parameters differ."""


class RelayStateReservationInvalid(RelayStateStoreError):
    """Raised when a reservation token cannot authorize an enqueue."""


class RelayStateNoEligibleNode(RelayStateStoreError):
    """Raised when no live compatible node has admission capacity."""


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise RelayStateStoreError(f"{name} must be between {minimum} and {maximum}")


def _positive_finite(value: float, name: str, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= maximum
    ):
        raise RelayStateStoreError(
            f"{name} must be a finite positive value no greater than {maximum}"
        )


@dataclass(frozen=True, slots=True)
class RelayStateStoreConfig:
    """Explicit key-space, expiry, identity, and admission bounds."""

    namespace: str
    schema_version: int = RELAY_STATE_SCHEMA_VERSION
    lease_ttl_seconds: float = 30.0
    max_compute_nodes: int = 1024
    max_node_id_bytes: int = 8192
    reservation_ttl_seconds: float = 10.0
    max_reservations: int = 4096
    max_reservations_per_client: int = 16
    max_reservations_per_node: int = 128
    max_queue_depth_per_node: int = 128
    max_identity_bytes: int = 8192
    max_model_id_bytes: int = 128
    max_scheduler_fingerprints: int = 4096
    max_deadline_seconds: float = 3600.0
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
        _positive_finite(self.lease_ttl_seconds, "lease TTL", float("inf"))
        _positive_finite(self.reservation_ttl_seconds, "reservation TTL", 300)
        _positive_finite(self.max_deadline_seconds, "deadline bound", 86_400)
        _bounded_int(self.max_compute_nodes, "compute-node bound", 1, 1_000_000)
        _bounded_int(self.max_node_id_bytes, "node-id bound", 1, 65_536)
        _bounded_int(self.max_reservations, "reservation bound", 1, 1_000_000)
        _bounded_int(
            self.max_reservations_per_client, "per-client reservation bound", 1, 10_000
        )
        _bounded_int(
            self.max_reservations_per_node, "per-node reservation bound", 1, 10_000
        )
        _bounded_int(
            self.max_queue_depth_per_node, "per-node queue bound", 1, 1_000_000
        )
        _bounded_int(self.max_identity_bytes, "identity bound", 1, 65_536)
        _bounded_int(self.max_model_id_bytes, "model ID bound", 1, 1024)
        _bounded_int(
            self.max_scheduler_fingerprints, "scheduler fingerprint bound", 1, 1_000_000
        )
        _bounded_int(
            self.max_envelope_bytes, "encrypted-envelope bound", 1, 64 * 1024 * 1024
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
        for model_id in self.supported_model_ids:
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or len(model_id.strip().encode()) > 128
            ):
                raise RelayStateStoreError(
                    "model IDs must be non-empty strings of at most 128 bytes"
                )
            value = model_id.strip().lower()
            if value not in normalized:
                normalized.append(value)
        object.__setattr__(self, "supported_model_ids", tuple(normalized))
        tier = (
            self.active_context_tier.strip().lower()
            if isinstance(self.active_context_tier, str)
            else ""
        )
        if tier not in CONTEXT_TIER_TOKEN_BOUNDS:
            raise RelayStateStoreError("unsupported active context tier")
        object.__setattr__(self, "active_context_tier", tier)
        for value, name, maximum in (
            (self.maximum_total_context_tokens, "maximum context tokens", 1_000_000),
            (
                self.default_output_token_reservation,
                "default output reservation",
                1_000_000,
            ),
            (self.maximum_output_tokens, "maximum output tokens", 1_000_000),
            (self.max_concurrency, "maximum concurrency", 128),
        ):
            _bounded_int(value, name, 1, maximum)
        if self.maximum_total_context_tokens < CONTEXT_TIER_TOKEN_BOUNDS[tier]:
            raise RelayStateStoreError(
                "maximum context tokens are below the active tier"
            )
        if self.default_output_token_reservation > self.maximum_output_tokens:
            raise RelayStateStoreError(
                "default output reservation exceeds maximum output tokens"
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
    node_id: str
    capabilities: ComputeNodeCapabilities
    control_credential_digest: str
    registered_at_epoch: float
    lease_expires_at_epoch: float
    healthy: bool = True
    draining: bool = False
    registration_order: int = 0


@dataclass(frozen=True, slots=True)
class EncryptedRequestEnvelope:
    """Exact API-v1 relay-blind ciphertext allowlist."""

    protocol: str
    version: int
    ciphertext: str
    cipherkey: str
    iv: str

    def __post_init__(self) -> None:
        if self.protocol != "tokenplace_api_v1_relay_e2ee" or self.version != 1:
            raise RelayStateStoreError(
                "unsupported encrypted-envelope protocol or version"
            )
        for name in ("ciphertext", "cipherkey", "iv"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RelayStateStoreError(
                    "encrypted-envelope fields must be non-empty strings"
                )

    @property
    def byte_size(self) -> int:
        return (
            sum(
                len(value.encode("utf-8"))
                for value in (self.protocol, self.ciphertext, self.cipherkey, self.iv)
            )
            + 8
        )


@dataclass(frozen=True, slots=True)
class SchedulerReservation:
    client_identity_digest: str
    request_identity_digest: str
    node_id: str
    model_id: str
    context_tier: str
    deadline_epoch: float
    expires_at_epoch: float
    token_digest: str


@dataclass(frozen=True, slots=True)
class ReservationResult:
    reservation: SchedulerReservation
    reservation_token: str | None = None
    created: bool = True

    def __repr__(self) -> str:
        return f"ReservationResult(reservation={self.reservation!r}, reservation_token=<redacted>, created={self.created!r})"


@dataclass(frozen=True, slots=True)
class QueuedEncryptedRequest:
    client_identity_digest: str
    request_identity_digest: str
    node_id: str
    model_id: str
    context_tier: str
    deadline_epoch: float
    envelope: EncryptedRequestEnvelope
    enqueued_at_epoch: float


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    request: QueuedEncryptedRequest
    created: bool
    state: str = "queued"


@runtime_checkable
class RelayStateStore(Protocol):
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
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        healthy: bool,
        draining: bool,
    ) -> ComputeNodeRegistration | None: ...
    def select_and_reserve(
        self,
        client_public_key: str,
        request_id: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
    ) -> ReservationResult: ...
    def enqueue_encrypted_request(
        self,
        client_public_key: str,
        request_id: str,
        reservation_token: str,
        node_id: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult: ...
    def reservations(self) -> tuple[SchedulerReservation, ...]: ...
    def queued(
        self, node_id: str | None = None
    ) -> tuple[QueuedEncryptedRequest, ...]: ...


class InMemoryRelayStateStore:
    """Lock-protected process-local implementation of the transition contract."""

    def __init__(
        self,
        config: RelayStateStoreConfig,
        *,
        epoch_time: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._epoch_time = epoch_time
        self._records: dict[str, ComputeNodeRegistration] = {}
        self._reservations: dict[tuple[str, str], SchedulerReservation] = {}
        self._queued: dict[tuple[str, str], QueuedEncryptedRequest] = {}
        self._queue_order: list[tuple[str, str]] = []
        self._enqueue_token_digests: dict[tuple[str, str], str] = {}
        self._enqueue_fingerprints: dict[tuple[str, str], str] = {}
        self._cursors: dict[str, str] = {}
        self._registration_sequence = 0
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
            self._reap_locked(now)
            existing = self._records.get(node_id)
            if existing is None and len(self._records) >= self.config.max_compute_nodes:
                raise RelayStateCapacityExceeded(
                    "compute-node registration capacity reached"
                )
            if existing:
                self._require_digest(existing, control_credential_digest)
                order = existing.registration_order
            else:
                self._registration_sequence += 1
                order = self._registration_sequence
            record = ComputeNodeRegistration(
                node_id,
                capabilities,
                (
                    existing.control_credential_digest
                    if existing
                    else control_credential_digest
                ),
                existing.registered_at_epoch if existing else now,
                self._deadline(now, self.config.lease_ttl_seconds),
                existing.healthy if existing else True,
                existing.draining if existing else False,
                order,
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
            self._reap_locked(now)
            record = self._records.get(node_id)
            if not record:
                return None
            self._require_digest(record, control_credential_digest)
            record = replace(
                record,
                capabilities=capabilities or record.capabilities,
                lease_expires_at_epoch=self._deadline(
                    now, self.config.lease_ttl_seconds
                ),
            )
            self._records[node_id] = record
            return replace(record)

    def set_scheduler_state(
        self,
        node_id: str,
        control_credential_digest: str,
        *,
        healthy: bool,
        draining: bool,
    ) -> ComputeNodeRegistration | None:
        if type(healthy) is not bool or type(draining) is not bool:
            raise RelayStateStoreError("scheduler state flags must be booleans")
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        with self._lock:
            self._reap_locked(self._now())
            record = self._records.get(node_id)
            if not record:
                return None
            self._require_digest(record, control_credential_digest)
            updated = replace(record, healthy=healthy, draining=draining)
            self._records[node_id] = updated
            return replace(updated)

    def get(self, node_id: str) -> ComputeNodeRegistration | None:
        self._validate_node_id(node_id)
        with self._lock:
            self._reap_locked(self._now())
            record = self._records.get(node_id)
            return replace(record) if record else None

    def list(self) -> tuple[ComputeNodeRegistration, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(replace(self._records[node]) for node in sorted(self._records))

    def expire(self) -> tuple[ComputeNodeRegistration, ...]:
        with self._lock:
            return tuple(
                replace(record)
                for record in self._expire_registrations_locked(self._now())
            )

    def unregister(self, node_id: str, control_credential_digest: str) -> bool:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        with self._lock:
            self._reap_locked(self._now())
            record = self._records.get(node_id)
            if not record:
                return False
            self._require_digest(record, control_credential_digest)
            del self._records[node_id]
            return True

    def select_and_reserve(
        self,
        client_public_key: str,
        request_id: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
    ) -> ReservationResult:
        identity = self._identity(client_public_key, request_id)
        model, tier = self._selection_values(model_id, context_tier)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            deadline = self._validate_deadline(deadline_epoch, now)
            existing_queue = self._queued.get(identity)
            existing = self._reservations.get(identity)
            if existing_queue:
                if (
                    existing_queue.model_id,
                    existing_queue.context_tier,
                    existing_queue.deadline_epoch,
                ) != (model, tier, deadline):
                    raise RelayStateConflict(
                        "request identity already has different parameters"
                    )
                raise RelayStateConflict("request identity is already queued")
            if existing:
                if (
                    existing.model_id,
                    existing.context_tier,
                    existing.deadline_epoch,
                ) != (model, tier, deadline):
                    raise RelayStateConflict(
                        "request identity already has different parameters"
                    )
                # Digest-only persistence makes reconstructing a lost raw token impossible.
                return ReservationResult(replace(existing), None, False)
            if len(self._reservations) >= self.config.max_reservations:
                raise RelayStateCapacityExceeded("reservation capacity reached")
            if (
                sum(key[0] == identity[0] for key in self._reservations)
                >= self.config.max_reservations_per_client
            ):
                raise RelayStateCapacityExceeded("client reservation capacity reached")
            candidates = self._eligible_nodes(model, tier)
            if not candidates:
                raise RelayStateNoEligibleNode("no eligible compute node")
            minimum_tier = min(
                CONTEXT_TIER_TOKEN_BOUNDS[r.capabilities.active_context_tier]
                for r in candidates
            )
            candidates = [
                r
                for r in candidates
                if CONTEXT_TIER_TOKEN_BOUNDS[r.capabilities.active_context_tier]
                == minimum_tier
            ]
            loads = {r.node_id: self._node_load(r.node_id) for r in candidates}
            minimum_load = min(loads.values())
            tied = sorted(
                (r for r in candidates if loads[r.node_id] == minimum_load),
                key=lambda r: r.registration_order,
            )
            cursor_key = hashlib.sha256(f"{model}\0{tier}".encode()).hexdigest()
            if (
                len(self._cursors) >= self.config.max_scheduler_fingerprints
                and cursor_key not in self._cursors
            ):
                raise RelayStateCapacityExceeded(
                    "scheduler fingerprint capacity reached"
                )
            anchor = self._cursors.get(cursor_key)
            selected = tied[0]
            if anchor:
                after = [
                    r
                    for r in tied
                    if r.registration_order
                    > next(
                        (x.registration_order for x in tied if x.node_id == anchor), -1
                    )
                ]
                if after:
                    selected = after[0]
                elif any(r.node_id == anchor for r in tied):
                    selected = tied[0]
            token = secrets.token_hex(32)
            reservation = SchedulerReservation(
                *identity,
                selected.node_id,
                model,
                tier,
                deadline,
                min(self._deadline(now, self.config.reservation_ttl_seconds), deadline),
                self._token_digest(token),
            )
            self._reservations[identity] = reservation
            self._cursors[cursor_key] = selected.node_id
            return ReservationResult(replace(reservation), token, True)

    def enqueue_encrypted_request(
        self,
        client_public_key: str,
        request_id: str,
        reservation_token: str,
        node_id: str,
        model_id: str,
        context_tier: str,
        deadline_epoch: float,
        envelope: EncryptedRequestEnvelope,
    ) -> EnqueueResult:
        identity = self._identity(client_public_key, request_id)
        self._validate_node_id(node_id)
        model, tier = self._selection_values(model_id, context_tier)
        if not isinstance(envelope, EncryptedRequestEnvelope):
            raise RelayStateStoreError("envelope must be EncryptedRequestEnvelope")
        if envelope.byte_size > self.config.max_envelope_bytes:
            raise RelayStateCapacityExceeded(
                "encrypted envelope exceeds its byte bound"
            )
        token_digest = self._token_digest(reservation_token)
        fingerprint = self._enqueue_fingerprint(
            node_id, model, tier, deadline_epoch, envelope
        )
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            deadline = self._validate_deadline(deadline_epoch, now)
            existing = self._queued.get(identity)
            if existing:
                if not hmac.compare_digest(
                    self._enqueue_token_digests[identity], token_digest
                ):
                    raise RelayStateReservationInvalid("reservation token is invalid")
                if not hmac.compare_digest(
                    self._enqueue_fingerprints[identity], fingerprint
                ):
                    raise RelayStateConflict(
                        "request identity already has different enqueue parameters"
                    )
                return EnqueueResult(replace(existing), False)
            reservation = self._reservations.get(identity)
            if not reservation or not hmac.compare_digest(
                reservation.token_digest, token_digest
            ):
                raise RelayStateReservationInvalid("reservation token is invalid")
            if (
                reservation.node_id,
                reservation.model_id,
                reservation.context_tier,
                reservation.deadline_epoch,
            ) != (node_id, model, tier, deadline):
                raise RelayStateReservationInvalid("reservation token is invalid")
            if reservation.expires_at_epoch <= now or deadline <= now:
                self._reservations.pop(identity, None)
                raise RelayStateReservationInvalid("reservation token is invalid")
            if self._node_queue_depth(node_id) >= self.config.max_queue_depth_per_node:
                raise RelayStateCapacityExceeded("compute-node queue capacity reached")
            queued = QueuedEncryptedRequest(
                *identity, node_id, model, tier, deadline, envelope, now
            )
            self._queued[identity] = queued
            self._queue_order.append(identity)
            self._enqueue_token_digests[identity] = token_digest
            self._enqueue_fingerprints[identity] = fingerprint
            del self._reservations[identity]
            return EnqueueResult(replace(queued), True)

    def reservations(self) -> tuple[SchedulerReservation, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(self._reservations[key]) for key in sorted(self._reservations)
            )

    def queued(self, node_id: str | None = None) -> tuple[QueuedEncryptedRequest, ...]:
        if node_id is not None:
            self._validate_node_id(node_id)
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(self._queued[key])
                for key in self._queue_order
                if key in self._queued
                and (node_id is None or self._queued[key].node_id == node_id)
            )

    def _eligible_nodes(self, model: str, tier: str) -> list[ComputeNodeRegistration]:
        requested_tokens = CONTEXT_TIER_TOKEN_BOUNDS[tier]
        return [
            r
            for r in self._records.values()
            if r.healthy
            and not r.draining
            and model in r.capabilities.supported_model_ids
            and r.capabilities.maximum_total_context_tokens >= requested_tokens
            and self._node_load(r.node_id) < r.capabilities.max_concurrency
            and sum(x.node_id == r.node_id for x in self._reservations.values())
            < self.config.max_reservations_per_node
            and self._node_load(r.node_id) < self.config.max_queue_depth_per_node
        ]

    def _node_queue_depth(self, node_id: str) -> int:
        return sum(item.node_id == node_id for item in self._queued.values())

    def _node_load(self, node_id: str) -> int:
        return self._node_queue_depth(node_id) + sum(
            item.node_id == node_id for item in self._reservations.values()
        )

    def _reap_locked(self, now: float) -> None:
        self._expire_registrations_locked(now)
        for identity, reservation in tuple(self._reservations.items()):
            if (
                reservation.expires_at_epoch <= now
                or reservation.deadline_epoch <= now
                or reservation.node_id not in self._records
            ):
                del self._reservations[identity]
        for identity, item in tuple(self._queued.items()):
            if item.deadline_epoch <= now:
                del self._queued[identity]

    def _expire_registrations_locked(self, now: float) -> list[ComputeNodeRegistration]:
        expired = sorted(
            (r for r in self._records.values() if r.lease_expires_at_epoch <= now),
            key=lambda r: r.node_id,
        )
        for record in expired:
            del self._records[record.node_id]
        return expired

    def _identity(self, client_public_key: str, request_id: str) -> tuple[str, str]:
        return self._identity_digest(
            client_public_key, "client identity"
        ), self._identity_digest(request_id, "request identity")

    def _identity_digest(self, value: str, name: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode()) > self.config.max_identity_bytes
        ):
            raise RelayStateStoreError(
                f"{name} is empty or exceeds its configured byte bound"
            )
        return hashlib.sha256(value.encode()).hexdigest()

    def _selection_values(self, model_id: str, context_tier: str) -> tuple[str, str]:
        if (
            not isinstance(model_id, str)
            or not model_id.strip()
            or len(model_id.strip().encode()) > self.config.max_model_id_bytes
        ):
            raise RelayStateStoreError(
                "model ID is empty or exceeds its configured byte bound"
            )
        model = model_id.strip().lower()
        tier = context_tier.strip().lower() if isinstance(context_tier, str) else ""
        if tier not in CONTEXT_TIER_TOKEN_BOUNDS:
            raise RelayStateStoreError("unsupported requested context tier")
        return model, tier

    def _validate_deadline(self, value: float, now: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RelayStateStoreError("deadline must be a finite UTC epoch")
        deadline = float(value)
        if deadline <= now or deadline > now + self.config.max_deadline_seconds:
            raise RelayStateStoreError(
                "deadline is expired or exceeds its configured bound"
            )
        return deadline

    @staticmethod
    def _enqueue_fingerprint(
        node: str,
        model: str,
        tier: str,
        deadline: float,
        envelope: EncryptedRequestEnvelope,
    ) -> str:
        parts = (
            node,
            model,
            tier,
            repr(float(deadline)),
            envelope.protocol,
            str(envelope.version),
            envelope.ciphertext,
            envelope.cipherkey,
            envelope.iv,
        )
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    @staticmethod
    def _token_digest(token: str) -> str:
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise RelayStateReservationInvalid("reservation token is invalid")
        return hashlib.sha256(token.encode()).hexdigest()

    def _now(self) -> float:
        value = self._epoch_time()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RelayStateStoreError("epoch clock must return a finite number")
        return float(value)

    @staticmethod
    def _deadline(now: float, ttl: float) -> float:
        deadline = now + ttl
        if not math.isfinite(deadline):
            raise RelayStateStoreError("expiry deadline must be finite")
        return deadline

    def _validate_node_id(self, node_id: str) -> None:
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id.encode()) > self.config.max_node_id_bytes
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
