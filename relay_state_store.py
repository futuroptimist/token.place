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
import struct
import threading
import time
from itertools import islice
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
    max_request_lifecycles: int = 4096
    max_queued_requests: int = 4096
    max_queued_requests_per_client: int = 8
    max_identity_bytes: int = 8192
    max_model_id_bytes: int = 128
    max_scheduler_fingerprints: int = 4096
    max_envelope_bytes: int = 1_048_576
    claim_ttl_seconds: float = 30.0
    max_claims: int = 4096
    max_claims_per_node: int = 128
    max_consumer_identity_bytes: int = 1024
    max_response_envelope_bytes: int = 1_048_576
    max_progress_envelope_bytes: int = 1_048_576
    max_progress_records: int = 4096
    max_progress_records_per_client: int = 8
    max_responses: int = 4096
    max_responses_per_client: int = 8
    response_replay_ttl_seconds: float = 300.0
    max_terminal_records: int = 4096
    max_terminal_records_per_client: int = 8
    terminal_retention_seconds: float = 3600.0
    max_cancellation_token_bytes: int = 1024
    control_tombstone_ttl_seconds: float = 300.0
    max_control_tombstones: int = 4096
    max_control_tombstones_per_node: int = 128
    node_transition_batch_size: int = 64
    max_pending_node_transitions: int = 1024
    max_node_tombstones: int = 1024
    node_tombstone_ttl_seconds: float = 300.0
    max_removed_owner_fences: int = 4096

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
        self._validate_float_bound(self.claim_ttl_seconds, "claim TTL", 0.001, 3600.0)
        self._validate_float_bound(
            self.response_replay_ttl_seconds, "response replay TTL", 0.001, 86_400.0
        )
        self._validate_float_bound(
            self.terminal_retention_seconds, "terminal retention", 0.001, 604_800.0
        )
        self._validate_float_bound(
            self.control_tombstone_ttl_seconds, "control tombstone TTL", 0.001, 300.0
        )
        self._validate_float_bound(
            self.node_tombstone_ttl_seconds, "node tombstone TTL", 0.001, 300.0
        )
        if self.terminal_retention_seconds < self.response_replay_ttl_seconds:
            raise RelayStateStoreError(
                "terminal retention must cover response replay retention"
            )
        if self.terminal_retention_seconds < self.control_tombstone_ttl_seconds:
            raise RelayStateStoreError(
                "terminal retention must cover control tombstone TTL"
            )
        for value, name, maximum in (
            (self.max_reservations, "reservation bound", 1_000_000),
            (self.max_reservations_per_client, "per-client reservation bound", 10_000),
            (self.max_reservations_per_node, "per-node reservation bound", 10_000),
            (self.max_queue_depth_per_node, "per-node queue bound", 1_000_000),
            (self.max_request_lifecycles, "request lifecycle bound", 1_000_000),
            (self.max_queued_requests, "global queue bound", 1_000_000),
            (
                self.max_queued_requests_per_client,
                "per-client queued-work bound",
                10_000,
            ),
            (self.max_identity_bytes, "identity byte bound", 65_536),
            (self.max_model_id_bytes, "model-id byte bound", 1024),
            (self.max_scheduler_fingerprints, "scheduler fingerprint bound", 1_000_000),
            (
                self.max_envelope_bytes,
                "encrypted-envelope byte bound",
                64 * 1024 * 1024,
            ),
            (self.max_claims, "claim bound", 1_000_000),
            (self.max_claims_per_node, "per-node claim bound", 10_000),
            (self.max_consumer_identity_bytes, "consumer identity byte bound", 65_536),
            (
                self.max_response_envelope_bytes,
                "response encrypted-envelope byte bound",
                64 * 1024 * 1024,
            ),
            (self.max_responses, "response bound", 1_000_000),
            (self.max_responses_per_client, "per-client response bound", 10_000),
            (
                self.max_progress_envelope_bytes,
                "progress encrypted-envelope byte bound",
                64 * 1024 * 1024,
            ),
            (self.max_progress_records, "progress-record bound", 1_000_000),
            (
                self.max_progress_records_per_client,
                "per-client progress-record bound",
                10_000,
            ),
            (self.max_terminal_records, "terminal-record bound", 1_000_000),
            (
                self.max_cancellation_token_bytes,
                "cancellation-token byte bound",
                65_536,
            ),
            (self.max_control_tombstones, "control-tombstone bound", 1_000_000),
            (self.node_transition_batch_size, "node-transition batch bound", 10_000),
            (
                self.max_pending_node_transitions,
                "pending node-transition bound",
                1_000_000,
            ),
            (self.max_node_tombstones, "node-tombstone bound", 1_000_000),
            (self.max_removed_owner_fences, "removed-owner fence bound", 1_000_000),
            (
                self.max_control_tombstones_per_node,
                "per-node control-tombstone bound",
                10_000,
            ),
            (
                self.max_terminal_records_per_client,
                "per-client terminal-record bound",
                10_000,
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
    ``state`` distinguishes a live reservation from an already queued lifecycle.
    Queued results never expose a token or reservation expiry.
    """

    selected_node_id: str
    requested_model_id: str
    requested_context_tier: str
    request_deadline_epoch: float
    reservation_expires_at_epoch: float | None
    reservation_token: str | None
    created: bool
    state: str = "reserved"

    def __repr__(self) -> str:
        return (
            "SelectionResult(selected_node_id=<redacted>, "
            f"requested_model_id={self.requested_model_id!r}, "
            f"requested_context_tier={self.requested_context_tier!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, "
            f"reservation_expires_at_epoch={self.reservation_expires_at_epoch!r}, "
            f"token_present={self.reservation_token is not None}, created={self.created!r}, "
            f"state={self.state!r})"
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
    client_public_key: str
    request_id: str
    selected_node_id: str
    requested_model_id: str
    requested_context_tier: str
    request_deadline_epoch: float
    envelope: EncryptedRequestEnvelope
    enqueued_at_epoch: float
    sequence: int


@dataclass(frozen=True, slots=True, repr=False)
class ControlTombstoneRecord:
    """Bounded, owner-authenticated stop-compute instruction."""

    client_identity_digest: str
    request_identity_digest: str
    selected_node_id: str
    control_credential_digest: str
    consumer_identity_digest: str
    generation: int
    status: str
    reason: str
    request_deadline_epoch: float
    acknowledged: bool
    expires_at_epoch: float

    def __repr__(self) -> str:
        return (
            "ControlTombstoneRecord(identities=<redacted>, selected_node_id=<redacted>, "
            f"generation={self.generation!r}, status={self.status!r}, reason={self.reason!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, "
            f"acknowledged={self.acknowledged!r}, expires_at_epoch={self.expires_at_epoch!r}, "
            "credentials=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NodeTombstoneRecord:
    """Owner-bound marker for a removed node; it is never scheduler authority."""

    node_identity_digest: str
    control_credential_digest: str
    cause: str
    status: str
    transition_epoch: float
    completed: bool
    expires_at_epoch: float

    def __repr__(self) -> str:
        return (
            "NodeTombstoneRecord(identity=<redacted>, credentials=<redacted>, "
            f"cause={self.cause!r}, status={self.status!r}, "
            f"transition_epoch={self.transition_epoch!r}, completed={self.completed!r}, "
            f"expires_at_epoch={self.expires_at_epoch!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NodeTransitionResult:
    """Bounded, cursor-free result of removing a node and its live work."""

    state: str
    cause: str
    transition_epoch: float | None
    processed_count: int
    reservations_terminalized: int
    queued_terminalized: int
    claims_terminalized: int
    new_outcomes: int
    continuation_required: bool

    def __repr__(self) -> str:
        return (
            f"NodeTransitionResult(state={self.state!r}, cause={self.cause!r}, "
            f"transition_epoch={self.transition_epoch!r}, processed_count={self.processed_count!r}, "
            f"reservations_terminalized={self.reservations_terminalized!r}, "
            f"queued_terminalized={self.queued_terminalized!r}, "
            f"claims_terminalized={self.claims_terminalized!r}, "
            f"new_outcomes={self.new_outcomes!r}, "
            f"continuation_required={self.continuation_required!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _PendingNodeTransition:
    node_id: str
    node_identity_digest: str
    control_credential_digest: str
    cause: str
    status: str
    reason: str
    transition_epoch: float


@dataclass(frozen=True, slots=True, repr=False)
class _FormerNodeAuthority:
    control_credential_digest: str
    cause: str
    status: str
    transition_epoch: float
    expires_at_epoch: float


@dataclass(frozen=True, slots=True, repr=False)
class TerminalTransitionResult:
    """Safe result of a cancellation or authoritative deadline transition."""

    state: str
    reason: str
    new_outcome: bool

    def __repr__(self) -> str:
        return f"TerminalTransitionResult(state={self.state!r}, reason={self.reason!r}, new_outcome={self.new_outcome!r})"


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Safe idempotent lifecycle result."""

    state: str
    selected_node_id: str
    request_deadline_epoch: float
    sequence: int
    created: bool


@dataclass(frozen=True, slots=True, repr=False)
class ClaimRecord:
    """Active, fenced claim; the consumer is retained only as a digest."""

    client_identity_digest: str
    request_identity_digest: str
    consumer_identity_digest: str
    selected_node_id: str
    control_credential_digest: str
    request_deadline_epoch: float
    envelope: EncryptedRequestEnvelope
    sequence: int
    generation: int
    lease_expires_at_epoch: float

    def __repr__(self) -> str:
        return (
            "ClaimRecord(identities=<redacted>, selected_node_id=<redacted>, "
            f"sequence={self.sequence!r}, generation={self.generation!r}, "
            f"lease_expires_at_epoch={self.lease_expires_at_epoch!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, envelope=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ClaimResult:
    """Typed poll result containing only compute-protocol fields."""

    state: str
    generation: int | None = None
    lease_expires_at_epoch: float | None = None
    request_deadline_epoch: float | None = None
    client_public_key: str | None = None
    request_id: str | None = None
    envelope: EncryptedRequestEnvelope | None = None

    def __repr__(self) -> str:
        return (
            f"ClaimResult(state={self.state!r}, generation={self.generation!r}, "
            f"lease_expires_at_epoch={self.lease_expires_at_epoch!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, "
            "routing_identity=<redacted>, envelope=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ClaimRenewalResult:
    """Fixed-result renewal outcome; no identity or payload is exposed."""

    state: str
    generation: int | None = None
    lease_expires_at_epoch: float | None = None
    reason: str | None = None
    acknowledged: bool = False

    def __repr__(self) -> str:
        return (
            f"ClaimRenewalResult(state={self.state!r}, generation={self.generation!r}, "
            f"lease_expires_at_epoch={self.lease_expires_at_epoch!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedResponseEnvelope:
    """Exact API-v1 encrypted response allowlist, with no plaintext surface."""

    protocol: str
    version: int
    ciphertext: str
    cipherkey: str
    iv: str

    def __post_init__(self) -> None:
        if self.protocol != "tokenplace_api_v1_relay_e2ee":
            raise RelayStateStoreError("unsupported encrypted response protocol")
        if isinstance(self.version, bool) or self.version != 1:
            raise RelayStateStoreError("unsupported encrypted response version")
        for value in (self.ciphertext, self.cipherkey, self.iv):
            if not isinstance(value, str) or not value:
                raise RelayStateStoreError(
                    "encrypted response values must be non-empty strings"
                )

    def __repr__(self) -> str:
        return "EncryptedResponseEnvelope(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedProgressEnvelope:
    """Exact API-v1 encrypted progress allowlist, with no plaintext surface."""

    protocol: str
    version: int
    ciphertext: str
    cipherkey: str
    iv: str

    def __post_init__(self) -> None:
        if self.protocol != "tokenplace_api_v1_relay_e2ee":
            raise RelayStateStoreError("unsupported encrypted progress protocol")
        if isinstance(self.version, bool) or self.version != 1:
            raise RelayStateStoreError("unsupported encrypted progress version")
        for value in (self.ciphertext, self.cipherkey, self.iv):
            if not isinstance(value, str) or not value:
                raise RelayStateStoreError(
                    "encrypted progress values must be non-empty strings"
                )

    def __repr__(self) -> str:
        return "EncryptedProgressEnvelope(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ProgressRecord:
    """Latest advisory progress for one canonical, live request identity."""

    client_identity_digest: str
    request_identity_digest: str
    envelope: EncryptedProgressEnvelope
    expires_at_epoch: float

    def __repr__(self) -> str:
        return (
            "ProgressRecord(identities=<redacted>, envelope=<redacted>, "
            f"expires_at_epoch={self.expires_at_epoch!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProgressReplacementResult:
    """Payload- and identity-free progress replacement outcome."""

    state: str

    def __repr__(self) -> str:
        return f"ProgressReplacementResult(state={self.state!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ResponseRecord:
    """One authoritative relay-blind response retained for later retrieval."""

    client_identity_digest: str
    request_identity_digest: str
    client_public_key: str
    request_id: str
    selected_node_id: str
    consumer_identity_digest: str
    generation: int
    envelope: EncryptedResponseEnvelope
    accepted_at_epoch: float
    response_digest: str
    replay_expires_at_epoch: float
    status: str = "response_ready"

    def __repr__(self) -> str:
        return (
            "ResponseRecord(identities=<redacted>, selected_node_id=<redacted>, "
            f"generation={self.generation!r}, accepted_at_epoch={self.accepted_at_epoch!r}, "
            f"replay_expires_at_epoch={self.replay_expires_at_epoch!r}, "
            "response_digest=<redacted>, envelope=<redacted>, "
            f"status={self.status!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TerminalOutcomeRecord:
    """Authoritative once-only completion and fencing record."""

    client_identity_digest: str
    request_identity_digest: str
    selected_node_id: str
    control_credential_digest: str
    consumer_identity_digest: str
    generation: int
    response_digest: str
    accepted_at_epoch: float
    replay_expires_at_epoch: float
    expires_at_epoch: float
    outcome: str = "completed"
    retrieval_state: str = "response_ready"
    retrieval_credential_digest: str = ""
    acknowledgement_digest: str = ""
    reason: str = "response_completed"
    cancellation_token_digest: str = ""

    def __repr__(self) -> str:
        return (
            "TerminalOutcomeRecord(identities=<redacted>, selected_node_id=<redacted>, "
            f"generation={self.generation!r}, accepted_at_epoch={self.accepted_at_epoch!r}, "
            f"replay_expires_at_epoch={self.replay_expires_at_epoch!r}, "
            f"expires_at_epoch={self.expires_at_epoch!r}, outcome={self.outcome!r}, "
            f"retrieval_state={self.retrieval_state!r}, credentials=<redacted>, "
            "response_digest=<redacted>, acknowledgement_digest=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ResponseAcceptanceResult:
    """Payload-free result suitable for exactly-once outcome accounting."""

    state: str
    generation: int
    accepted_at_epoch: float
    replay_expires_at_epoch: float
    new_outcome: bool

    def __repr__(self) -> str:
        return (
            f"ResponseAcceptanceResult(state={self.state!r}, generation={self.generation!r}, "
            f"accepted_at_epoch={self.accepted_at_epoch!r}, "
            f"replay_expires_at_epoch={self.replay_expires_at_epoch!r}, "
            f"new_outcome={self.new_outcome!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ResponseRetrievalResult:
    """Fixed retrieval transition result with all sensitive fields redacted in repr."""

    state: str
    envelope: EncryptedResponseEnvelope | None = None
    acknowledgement_token: str | None = None
    replay_expires_at_epoch: float | None = None
    request_deadline_epoch: float | None = None
    progress: EncryptedProgressEnvelope | None = None

    def __repr__(self) -> str:
        return (
            f"ResponseRetrievalResult(state={self.state!r}, envelope=<redacted>, "
            f"acknowledgement_token_present={self.acknowledgement_token is not None}, "
            f"replay_expires_at_epoch={self.replay_expires_at_epoch!r}, "
            f"request_deadline_epoch={self.request_deadline_epoch!r}, progress=<redacted>)"
        )


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
    def unregister_node_and_transition_work(
        self,
        node_id: str,
        control_credential_digest: str | None = None,
        *,
        cause: str = "explicit_unregister",
    ) -> NodeTransitionResult: ...
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
        cancellation_token: str | None = None,
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
        cancellation_token: str,
    ) -> EnqueueResult: ...
    def list_reservations(self) -> tuple[ReservationRecord, ...]: ...
    def queued_requests(self, node_id: str) -> tuple[QueuedRequest, ...]: ...
    def claim_queued_request(
        self, node_id: str, control_credential_digest: str, consumer_identity: str
    ) -> ClaimResult: ...
    def renew_claim(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
    ) -> ClaimRenewalResult: ...
    def renew_claim_or_read_control(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
        *,
        acknowledge: bool = False,
    ) -> ClaimRenewalResult: ...
    def cancel_or_expire_request(
        self,
        client_public_key: str,
        request_id: str,
        cancellation_token: str | None = None,
        *,
        status: str = "cancelled",
        reason: str = "requester_cancelled",
    ) -> TerminalTransitionResult: ...
    def active_claims(self, node_id: str) -> tuple[ClaimRecord, ...]: ...
    def accept_encrypted_response(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
        envelope: EncryptedResponseEnvelope,
    ) -> ResponseAcceptanceResult: ...
    def replace_encrypted_progress_if_claimed(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
        envelope: EncryptedProgressEnvelope,
    ) -> ProgressReplacementResult: ...
    def retrieve_encrypted_response(
        self,
        client_public_key: str,
        request_id: str,
        retrieval_credential: str,
        acknowledgement_token: str | None = None,
    ) -> ResponseRetrievalResult: ...
    def response_records(self) -> tuple[ResponseRecord, ...]: ...
    def progress_records(self) -> tuple[ProgressRecord, ...]: ...
    def terminal_records(self) -> tuple[TerminalOutcomeRecord, ...]: ...
    def control_tombstones(self) -> tuple[ControlTombstoneRecord, ...]: ...
    def node_tombstones(self) -> tuple[NodeTombstoneRecord, ...]: ...


class InMemoryRelayStateStore:
    """Lock-protected, process-local implementation of :class:`RelayStateStore`."""

    def __init__(
        self,
        config: RelayStateStoreConfig,
        *,
        acknowledgement_key: bytes,
        epoch_time: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(acknowledgement_key, bytes) or len(acknowledgement_key) < 32:
            raise RelayStateStoreError(
                "acknowledgement key must be bytes containing at least 256 bits"
            )
        self._config = config
        self._acknowledgement_key = bytes(acknowledgement_key)
        self._epoch_time = epoch_time
        self._records: dict[str, ComputeNodeRegistration] = {}
        self._scheduler_states: dict[str, SchedulerNodeState] = {}
        self._registration_order: dict[str, int] = {}
        self._next_registration_order = 0
        self._reservations: dict[tuple[str, str], ReservationRecord] = {}
        self._queued: dict[tuple[str, str], QueuedRequest] = {}
        self._queued_token_digests: dict[tuple[str, str], str] = {}
        self._cancellation_token_digests: dict[tuple[str, str], str] = {}
        self._node_queues: dict[str, list[QueuedRequest]] = {}
        self._claims: dict[tuple[str, str], ClaimRecord] = {}
        self._responses: dict[tuple[str, str], ResponseRecord] = {}
        self._progress: dict[tuple[str, str], ProgressRecord] = {}
        self._terminals: dict[tuple[str, str], TerminalOutcomeRecord] = {}
        self._control_tombstones: dict[tuple[str, str], ControlTombstoneRecord] = {}
        self._node_tombstones: dict[str, NodeTombstoneRecord] = {}
        self._pending_node_transitions: dict[str, _PendingNodeTransition] = {}
        self._former_node_authorities: dict[str, dict[str, _FormerNodeAuthority]] = {}
        # Ordered-set indexes make teardown proportional to its batch, rather
        # than to all work in the store.  They are private continuation state.
        self._node_work_identities: dict[str, dict[tuple[str, str], None]] = {}
        self._deferred_deadline_identities: set[tuple[str, str]] = set()
        self._next_claim_generation = 0
        self._fairness_cursors: dict[str, tuple[str, int]] = {}
        self._fairness_activity = 0
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
            self._reap_retained_locked(now)
            self._expire_locked(now)
            if node_id in self._pending_node_transitions:
                raise RelayStateConflict("node transition is incomplete")
            existing = self._records.get(node_id)
            if existing is None and len(self._records) >= self.config.max_compute_nodes:
                raise RelayStateCapacityExceeded(
                    "compute-node registration capacity reached"
                )
            if existing is not None:
                self._require_digest(existing, control_credential_digest)
            elif any(
                hmac.compare_digest(removed_digest, control_credential_digest)
                for removed_digest in self._former_node_authorities.get(
                    self._node_digest(node_id), {}
                )
            ):
                raise RelayStateCredentialMismatch(
                    "control credential digest has stale registration authority"
                )
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
            return tuple(
                replace(record)
                for record in self._expire_locked(self._now(), continue_pending=True)
            )

    def unregister(self, node_id: str, control_credential_digest: str) -> bool:
        result = self.unregister_node_and_transition_work(
            node_id, control_credential_digest, cause="explicit_unregister"
        )
        return result.state not in {"not_found", "already_complete"}

    def unregister_node_and_transition_work(
        self,
        node_id: str,
        control_credential_digest: str | None = None,
        *,
        cause: str = "explicit_unregister",
    ) -> NodeTransitionResult:
        self._validate_node_id(node_id)
        if cause not in {"explicit_unregister", "registration_lease_expired"}:
            raise RelayStateStoreError("node transition cause is invalid")
        if cause == "explicit_unregister":
            self._validate_digest(control_credential_digest)
        with self._lock:
            now = self._now()
            self._reap_retained_locked(now)
            return self._transition_node_locked(
                node_id, control_credential_digest, cause, now
            )

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
        cancellation_token: str | None = None,
    ) -> SelectionResult:
        client_digest, request_digest = self._identity(client_public_key, request_id)
        model_id = self._model_id(requested_model_id)
        tier = self._context_tier(requested_context_tier)
        deadline = self._deadline(request_deadline_epoch)
        fingerprint = self._scheduler_fingerprint(model_id, tier)
        identity = (client_digest, request_digest)
        cancellation_digest = (
            self._cancellation_token_digest(cancellation_token)
            if cancellation_token is not None
            else None
        )
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            if deadline <= now:
                raise RelayStateInvalidReservation("request deadline expired")
            if deadline > now + self.config.max_request_ttl_seconds:
                raise RelayStateStoreError(
                    "request deadline exceeds its configured bound"
                )
            if identity in self._terminals:
                raise RelayStateConflict("request lifecycle is terminal")
            queued = self._queued.get(identity)
            if queued is not None:
                self._require_same_parameters(queued, model_id, tier, deadline)
                stored_cancellation_digest = self._cancellation_token_digests.get(
                    identity
                )
                if (
                    stored_cancellation_digest is not None
                    and cancellation_digest is not None
                    and not hmac.compare_digest(
                        stored_cancellation_digest, cancellation_digest
                    )
                ):
                    raise RelayStateConflict("request identity conflict")
                claim = self._claims.get(identity)
                lifecycle_state = (
                    "claimed"
                    if claim is not None and claim.lease_expires_at_epoch > now
                    else "queued"
                )
                return SelectionResult(
                    queued.selected_node_id,
                    model_id,
                    tier,
                    deadline,
                    None,
                    None,
                    False,
                    lifecycle_state,
                )
            existing = self._reservations.get(identity)
            if existing is not None:
                self._require_same_parameters(existing, model_id, tier, deadline)
                stored_cancellation_digest = self._cancellation_token_digests.get(
                    identity
                )
                if stored_cancellation_digest is not None and (
                    cancellation_digest is None
                    or not hmac.compare_digest(
                        stored_cancellation_digest, cancellation_digest
                    )
                ):
                    raise RelayStateConflict("request identity conflict")
                return self._selection_result(existing, None, False)
            if len(self._reservations) >= self.config.max_reservations:
                raise RelayStateNoCapacity("no scheduler capacity")
            if (
                len(self._reservations) + len(self._queued)
                >= self.config.max_request_lifecycles
            ):
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
            cursor_record = self._fairness_cursors.get(fingerprint)
            cursor = cursor_record[0] if cursor_record is not None else None
            selected = tied[0]
            if cursor is not None:
                cursor_order = self._registration_order.get(cursor, -1)
                selected = next(
                    (item for item in tied if item[2] > cursor_order), tied[0]
                )
            node_id = selected[3]
            evicted_fingerprint = self._cursor_eviction_candidate_locked(fingerprint)
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
            if evicted_fingerprint is not None:
                del self._fairness_cursors[evicted_fingerprint]
            self._reservations[identity] = record
            self._node_work_identities.setdefault(node_id, {})[identity] = None
            if cancellation_digest is not None:
                self._cancellation_token_digests[identity] = cancellation_digest
            self._fairness_activity += 1
            self._fairness_cursors[fingerprint] = (node_id, self._fairness_activity)
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
        cancellation_token: str,
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
        cancellation_digest = self._cancellation_token_digest(cancellation_token)
        identity = (client_digest, request_digest)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            if deadline <= now:
                raise RelayStateInvalidReservation("reservation invalid")
            if identity in self._terminals:
                raise RelayStateConflict("request lifecycle is terminal")
            existing = self._queued.get(identity)
            if existing is not None:
                self._require_same_parameters(existing, model_id, tier, deadline)
                if (
                    existing.selected_node_id != selected_node_id
                    or existing.envelope != envelope
                ):
                    raise RelayStateConflict("request identity conflict")
                if not hmac.compare_digest(
                    self._queued_token_digests[identity], token_digest
                ):
                    raise RelayStateInvalidReservation("reservation invalid")
                if not hmac.compare_digest(
                    self._cancellation_token_digests[identity], cancellation_digest
                ):
                    raise RelayStateConflict("request identity conflict")
                return self._enqueue_result(
                    existing,
                    False,
                    (
                        "claimed"
                        if identity in self._claims
                        and self._claims[identity].lease_expires_at_epoch > now
                        else "queued"
                    ),
                )
            if deadline > now + self.config.max_request_ttl_seconds:
                raise RelayStateStoreError(
                    "request deadline exceeds its configured bound"
                )
            reservation = self._reservations.get(identity)
            if reservation is None:
                raise RelayStateInvalidReservation("reservation invalid")
            registration = self._records.get(reservation.selected_node_id)
            if (
                registration is None
                or registration.lease_expires_at_epoch <= now
                or reservation.selected_node_id in self._pending_node_transitions
            ):
                raise RelayStateInvalidReservation("reservation invalid")
            self._require_same_parameters(reservation, model_id, tier, deadline)
            reserved_cancellation_digest = self._cancellation_token_digests.get(
                identity
            )
            if reserved_cancellation_digest is not None and not hmac.compare_digest(
                reserved_cancellation_digest, cancellation_digest
            ):
                raise RelayStateConflict("request identity conflict")
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
            queued_for_client = sum(
                item.client_identity_digest == client_digest
                for item in self._queued.values()
            )
            if (
                len(self._queued) >= self.config.max_queued_requests
                or queued_for_client >= self.config.max_queued_requests_per_client
            ):
                raise RelayStateNoCapacity("no scheduler capacity")
            self._next_queue_sequence += 1
            queued = QueuedRequest(
                client_digest,
                request_digest,
                client_public_key,
                request_id,
                selected_node_id,
                model_id,
                tier,
                deadline,
                envelope,
                now,
                self._next_queue_sequence,
            )
            self._queued[identity] = queued
            self._queued_token_digests[identity] = token_digest
            self._cancellation_token_digests[identity] = cancellation_digest
            self._node_queues.setdefault(selected_node_id, []).append(queued)
            del self._reservations[identity]
            return self._enqueue_result(queued, True)

    def list_reservations(self) -> tuple[ReservationRecord, ...]:
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            return tuple(
                replace(record)
                for record in self._reservations.values()
                if record.request_deadline_epoch > now
            )

    def queued_requests(self, node_id: str) -> tuple[QueuedRequest, ...]:
        self._validate_node_id(node_id)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            return tuple(
                replace(record)
                for record in self._node_queues.get(node_id, ())
                if record.request_deadline_epoch > now
                if (
                    (
                        claim := self._claims.get(
                            (
                                record.client_identity_digest,
                                record.request_identity_digest,
                            )
                        )
                    )
                    is None
                    or claim.lease_expires_at_epoch <= now
                )
            )

    def claim_queued_request(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
    ) -> ClaimResult:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        consumer_digest = self._consumer_digest(consumer_identity)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            registration = self._records.get(node_id)
            if registration is None:
                raise RelayStateCredentialMismatch("claim owner is invalid")
            self._require_digest(registration, control_credential_digest)
            active_claims = tuple(
                claim
                for claim in self._claims.values()
                if claim.lease_expires_at_epoch > now
            )
            node_claims = sum(
                claim.selected_node_id == node_id for claim in active_claims
            )
            for queued in self._node_queues.get(node_id, ()):
                identity = (
                    queued.client_identity_digest,
                    queued.request_identity_digest,
                )
                if queued.request_deadline_epoch <= now:
                    continue
                existing = self._claims.get(identity)
                if existing is not None and existing.lease_expires_at_epoch > now:
                    continue
                if (
                    len(active_claims) >= self.config.max_claims
                    or node_claims >= self.config.max_claims_per_node
                ):
                    raise RelayStateCapacityExceeded("claim capacity reached")
                self._next_claim_generation += 1
                lease = min(
                    now + self.config.claim_ttl_seconds, queued.request_deadline_epoch
                )
                if not math.isfinite(lease):
                    raise RelayStateStoreError("claim deadline must be finite")
                claim = ClaimRecord(
                    queued.client_identity_digest,
                    queued.request_identity_digest,
                    consumer_digest,
                    node_id,
                    registration.control_credential_digest,
                    queued.request_deadline_epoch,
                    queued.envelope,
                    queued.sequence,
                    self._next_claim_generation,
                    lease,
                )
                self._claims[identity] = claim
                return ClaimResult(
                    "reclaimed" if existing is not None else "claimed",
                    claim.generation,
                    claim.lease_expires_at_epoch,
                    claim.request_deadline_epoch,
                    queued.client_public_key,
                    queued.request_id,
                    replace(queued.envelope),
                )
            return ClaimResult("empty")

    def renew_claim(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
    ) -> ClaimRenewalResult:
        return self.renew_claim_or_read_control(
            node_id,
            control_credential_digest,
            consumer_identity,
            client_public_key,
            request_id,
            generation,
        )

    def renew_claim_or_read_control(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
        *,
        acknowledge: bool = False,
    ) -> ClaimRenewalResult:
        """Renew a live claim or atomically read/ack its owner-bound tombstone."""
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        consumer_digest = self._consumer_digest(consumer_identity)
        identity = self._identity(client_public_key, request_id)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise RelayStateStoreError("claim generation is invalid")
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            registration = self._records.get(node_id)
            tombstone = self._control_tombstones.get(identity)
            if tombstone is not None:
                if not (
                    tombstone.selected_node_id == node_id
                    and tombstone.generation == generation
                    and hmac.compare_digest(
                        tombstone.control_credential_digest, control_credential_digest
                    )
                    and hmac.compare_digest(
                        tombstone.consumer_identity_digest, consumer_digest
                    )
                ):
                    return ClaimRenewalResult("owner_mismatch")
                if acknowledge and not tombstone.acknowledged:
                    tombstone = replace(tombstone, acknowledged=True)
                    self._control_tombstones[identity] = tombstone
                return ClaimRenewalResult(
                    "acknowledged" if tombstone.acknowledged else tombstone.status,
                    generation,
                    None,
                    tombstone.reason,
                    tombstone.acknowledged,
                )
            if registration is None or not hmac.compare_digest(
                registration.control_credential_digest, control_credential_digest
            ):
                return ClaimRenewalResult("owner_mismatch")
            claim = self._claims.get(identity)
            if claim is None or claim.request_deadline_epoch <= now:
                return ClaimRenewalResult("missing_or_expired")
            if claim.generation != generation:
                return ClaimRenewalResult("stale_generation", claim.generation)
            if claim.selected_node_id != node_id or not hmac.compare_digest(
                claim.consumer_identity_digest, consumer_digest
            ):
                return ClaimRenewalResult("owner_mismatch")
            if claim.lease_expires_at_epoch <= now:
                return ClaimRenewalResult("missing_or_expired")
            lease = min(
                now + self.config.claim_ttl_seconds, claim.request_deadline_epoch
            )
            renewed = replace(claim, lease_expires_at_epoch=lease)
            self._claims[identity] = renewed
            return ClaimRenewalResult("continued", generation, lease)

    def active_claims(self, node_id: str) -> tuple[ClaimRecord, ...]:
        self._validate_node_id(node_id)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            return tuple(
                replace(claim)
                for claim in sorted(
                    self._claims.values(), key=lambda item: item.sequence
                )
                if claim.selected_node_id == node_id
                and claim.lease_expires_at_epoch > now
            )

    def replace_encrypted_progress_if_claimed(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
        envelope: EncryptedProgressEnvelope,
    ) -> ProgressReplacementResult:
        """Atomically replace advisory ciphertext for the exact live claim."""
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        consumer_digest = self._consumer_digest(consumer_identity)
        identity = self._identity(client_public_key, request_id)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise RelayStateStoreError("claim generation is invalid")
        if not isinstance(envelope, EncryptedProgressEnvelope):
            raise RelayStateStoreError(
                "progress envelope must be EncryptedProgressEnvelope"
            )
        if (
            len(self._serialized_encrypted_envelope(envelope))
            > self.config.max_progress_envelope_bytes
        ):
            raise RelayStateStoreError(
                "encrypted progress exceeds its configured byte bound"
            )
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            registration = self._records.get(node_id)
            if registration is None or not hmac.compare_digest(
                registration.control_credential_digest, control_credential_digest
            ):
                raise RelayStateCredentialMismatch("progress owner is invalid")
            claim = self._claims.get(identity)
            if claim is None:
                raise RelayStateConflict("progress claim is missing or expired")
            if claim.generation != generation:
                raise RelayStateConflict("progress claim generation is stale")
            if claim.selected_node_id != node_id or not hmac.compare_digest(
                claim.consumer_identity_digest, consumer_digest
            ):
                raise RelayStateCredentialMismatch("progress owner is invalid")
            if (
                claim.lease_expires_at_epoch <= now
                or claim.request_deadline_epoch <= now
            ):
                raise RelayStateConflict("progress claim is missing or expired")
            queued = self._queued.get(identity)
            if (
                queued is None
                or queued.selected_node_id != node_id
                or queued.client_public_key != client_public_key
                or queued.request_id != request_id
            ):
                raise RelayStateConflict("progress lifecycle identity conflict")
            existing = self._progress.get(identity)
            if existing is None and (
                len(self._progress) >= self.config.max_progress_records
                or sum(
                    record.client_identity_digest == identity[0]
                    for record in self._progress.values()
                )
                >= self.config.max_progress_records_per_client
            ):
                raise RelayStateCapacityExceeded("progress capacity reached")
            self._progress[identity] = ProgressRecord(
                identity[0],
                identity[1],
                replace(envelope),
                claim.request_deadline_epoch,
            )
            return ProgressReplacementResult(
                "replaced" if existing is not None else "accepted"
            )

    def accept_encrypted_response(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
        envelope: EncryptedResponseEnvelope,
    ) -> ResponseAcceptanceResult:
        """Atomically finalize the exact live fenced claim with one ciphertext."""
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        consumer_digest = self._consumer_digest(consumer_identity)
        identity = self._identity(client_public_key, request_id)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise RelayStateStoreError("claim generation is invalid")
        if not isinstance(envelope, EncryptedResponseEnvelope):
            raise RelayStateStoreError(
                "response envelope must be EncryptedResponseEnvelope"
            )
        serialized = self._serialized_response_envelope(envelope)
        if len(serialized) > self.config.max_response_envelope_bytes:
            raise RelayStateStoreError(
                "encrypted response exceeds its configured byte bound"
            )
        response_digest = hashlib.sha256(serialized).hexdigest()
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            terminal = self._terminals.get(identity)
            if terminal is not None:
                # The retained terminal authenticates an exact non-mutating retry
                # after registration expiry or unregister; no live registration is needed.
                if (
                    terminal.selected_node_id == node_id
                    and terminal.generation == generation
                    and hmac.compare_digest(
                        terminal.control_credential_digest,
                        control_credential_digest,
                    )
                    and hmac.compare_digest(
                        terminal.consumer_identity_digest, consumer_digest
                    )
                    and hmac.compare_digest(terminal.response_digest, response_digest)
                ):
                    return self._terminal_response_result(terminal)
                raise RelayStateConflict("response lifecycle conflict")

            registration = self._records.get(node_id)
            if registration is None or not hmac.compare_digest(
                registration.control_credential_digest, control_credential_digest
            ):
                raise RelayStateCredentialMismatch("response owner is invalid")
            claim = self._claims.get(identity)
            if claim is None:
                raise RelayStateConflict("response claim is missing or expired")
            if claim.generation != generation:
                raise RelayStateConflict("response claim generation is stale")
            if claim.selected_node_id != node_id or not hmac.compare_digest(
                claim.consumer_identity_digest, consumer_digest
            ):
                raise RelayStateCredentialMismatch("response owner is invalid")
            if (
                claim.lease_expires_at_epoch <= now
                or claim.request_deadline_epoch <= now
            ):
                raise RelayStateConflict("response claim is missing or expired")
            queued = self._queued.get(identity)
            if (
                queued is None
                or queued.selected_node_id != node_id
                or queued.client_public_key != client_public_key
                or queued.request_id != request_id
            ):
                raise RelayStateConflict("response lifecycle identity conflict")
            client_digest = identity[0]
            if (
                len(self._responses) >= self.config.max_responses
                or sum(
                    item.client_identity_digest == client_digest
                    for item in self._responses.values()
                )
                >= self.config.max_responses_per_client
                or len(self._terminals) >= self.config.max_terminal_records
                or sum(
                    item.client_identity_digest == client_digest
                    for item in self._terminals.values()
                )
                >= self.config.max_terminal_records_per_client
            ):
                raise RelayStateCapacityExceeded("response lifecycle capacity reached")
            replay_expires = now + self.config.response_replay_ttl_seconds
            terminal_expires = now + self.config.terminal_retention_seconds
            if not math.isfinite(replay_expires) or not math.isfinite(terminal_expires):
                raise RelayStateStoreError("response retention deadline must be finite")
            response = ResponseRecord(
                identity[0],
                identity[1],
                queued.client_public_key,
                queued.request_id,
                node_id,
                consumer_digest,
                generation,
                replace(envelope),
                now,
                response_digest,
                replay_expires,
            )
            terminal = TerminalOutcomeRecord(
                identity[0],
                identity[1],
                node_id,
                registration.control_credential_digest,
                consumer_digest,
                generation,
                response_digest,
                now,
                replay_expires,
                terminal_expires,
                retrieval_credential_digest=self._queued_token_digests[identity],
                acknowledgement_digest=self._acknowledgement_digest(
                    self._derive_acknowledgement_token(identity, now, response_digest)
                ),
                cancellation_token_digest=self._cancellation_token_digests[identity],
            )
            self._responses[identity] = response
            self._terminals[identity] = terminal
            self._remove_queued_identity_locked(identity, queued)
            return self._response_result(response, True)

    def retrieve_encrypted_response(
        self,
        client_public_key: str,
        request_id: str,
        retrieval_credential: str,
        acknowledgement_token: str | None = None,
    ) -> ResponseRetrievalResult:
        """Replay or atomically acknowledge one identity-bound encrypted response."""
        identity = self._identity(client_public_key, request_id)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            terminal = self._terminals.get(identity)
            queued = self._queued.get(identity)
            if terminal is None and queued is not None:
                expected = self._queued_token_digests.get(identity, "")
                if not hmac.compare_digest(
                    self._safe_retrieval_credential_digest(retrieval_credential),
                    expected,
                ):
                    return ResponseRetrievalResult("invalid_retrieval_credential")
                progress = self._progress.pop(identity, None)
                return ResponseRetrievalResult(
                    "pending",
                    request_deadline_epoch=queued.request_deadline_epoch,
                    progress=(
                        replace(progress.envelope) if progress is not None else None
                    ),
                )
            if terminal is None:
                # Retrieval is credential-gated even when no record exists.  A
                # single failure prevents routing metadata from becoming an
                # identity oracle and keeps cross-identity credentials safe.
                return ResponseRetrievalResult("invalid_retrieval_credential")
            if not hmac.compare_digest(
                self._safe_retrieval_credential_digest(retrieval_credential),
                terminal.retrieval_credential_digest,
            ):
                return ResponseRetrievalResult("invalid_retrieval_credential")
            if terminal.retrieval_state != "response_ready":
                if (
                    terminal.retrieval_state == "acknowledged"
                    and acknowledgement_token is not None
                    and not self._acknowledgement_matches(
                        identity, terminal, acknowledgement_token
                    )
                ):
                    return ResponseRetrievalResult("invalid_acknowledgement")
                return ResponseRetrievalResult(terminal.retrieval_state)
            response = self._responses.get(identity)
            if response is None:
                # Defensive fail-closed repair for a backend-inconsistent ready record.
                self._terminals[identity] = replace(
                    terminal, retrieval_state="retrieval_expired"
                )
                return ResponseRetrievalResult("retrieval_expired")
            raw_token = self._derive_acknowledgement_token(
                identity, terminal.accepted_at_epoch, terminal.response_digest
            )
            if acknowledgement_token is None:
                return ResponseRetrievalResult(
                    "response_ready",
                    replace(response.envelope),
                    raw_token,
                    response.replay_expires_at_epoch,
                )
            supplied_digest = self._safe_acknowledgement_digest(acknowledgement_token)
            expected_digest = self._acknowledgement_digest(raw_token)
            if not (
                hmac.compare_digest(expected_digest, terminal.acknowledgement_digest)
                and hmac.compare_digest(
                    supplied_digest, terminal.acknowledgement_digest
                )
            ):
                return ResponseRetrievalResult("invalid_acknowledgement")
            del self._responses[identity]
            self._terminals[identity] = replace(
                terminal, retrieval_state="acknowledged"
            )
            return ResponseRetrievalResult("acknowledged")

    def response_records(self) -> tuple[ResponseRecord, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(record, envelope=replace(record.envelope))
                for record in self._responses.values()
            )

    def progress_records(self) -> tuple[ProgressRecord, ...]:
        """Return immutable defensive records for backend contract inspection."""
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(record, envelope=replace(record.envelope))
                for record in self._progress.values()
            )

    def cancel_or_expire_request(
        self,
        client_public_key: str,
        request_id: str,
        cancellation_token: str | None = None,
        *,
        status: str = "cancelled",
        reason: str = "requester_cancelled",
    ) -> TerminalTransitionResult:
        """Atomically cancel authenticated work or expire it at its deadline."""
        if (status, reason) not in {
            ("cancelled", "requester_cancelled"),
            ("expired", "request_deadline_expired"),
        }:
            raise RelayStateStoreError("terminal status or reason is invalid")
        try:
            identity = self._identity(client_public_key, request_id)
        except (RelayStateStoreError, UnicodeError):
            return TerminalTransitionResult(
                "invalid_cancellation_proof", "invalid_cancellation_proof", False
            )
        supplied = self._safe_cancellation_token_digest(cancellation_token)
        with self._lock:
            now = self._now()
            self._reap_locked(now)
            terminal = self._terminals.get(identity)
            if terminal is not None:
                if status == "cancelled" and not hmac.compare_digest(
                    terminal.cancellation_token_digest, supplied
                ):
                    return TerminalTransitionResult(
                        "invalid_cancellation_proof",
                        "invalid_cancellation_proof",
                        False,
                    )
                return TerminalTransitionResult(
                    terminal.outcome, terminal.reason, False
                )
            if status == "cancelled":
                expected = self._cancellation_token_digests.get(identity)
                if expected is None or not hmac.compare_digest(expected, supplied):
                    return TerminalTransitionResult(
                        "invalid_cancellation_proof",
                        "invalid_cancellation_proof",
                        False,
                    )
            record = self._reservations.get(identity) or self._queued.get(identity)
            if record is None:
                return TerminalTransitionResult(
                    "invalid_cancellation_proof", "invalid_cancellation_proof", False
                )
            if status == "expired" and record.request_deadline_epoch > now:
                return TerminalTransitionResult(
                    "not_expired", "request_deadline_active", False
                )
            return self._terminalize_locked(identity, record, status, reason, now)

    def control_tombstones(self) -> tuple[ControlTombstoneRecord, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(record) for record in self._control_tombstones.values()
            )

    def node_tombstones(self) -> tuple[NodeTombstoneRecord, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(
                replace(self._node_tombstones[key])
                for key in sorted(self._node_tombstones)
            )

    def terminal_records(self) -> tuple[TerminalOutcomeRecord, ...]:
        with self._lock:
            self._reap_locked(self._now())
            return tuple(replace(record) for record in self._terminals.values())

    def _terminalize_locked(
        self, identity, record, status, reason, now, *, force_claim_tombstone=False
    ):
        claim = self._claims.get(identity)
        tombstone_claim = None
        if claim is not None and (
            force_claim_tombstone
            or (status == "cancelled" and claim.lease_expires_at_epoch > now)
            or (
                status == "expired"
                and claim.lease_expires_at_epoch >= record.request_deadline_epoch
            )
        ):
            tombstone_claim = claim
        if (
            len(self._terminals) >= self.config.max_terminal_records
            or sum(
                t.client_identity_digest == identity[0]
                for t in self._terminals.values()
            )
            >= self.config.max_terminal_records_per_client
        ):
            raise RelayStateCapacityExceeded("terminal lifecycle capacity reached")
        if tombstone_claim is not None:
            node_count = sum(
                t.selected_node_id == tombstone_claim.selected_node_id
                for t in self._control_tombstones.values()
            )
            if (
                len(self._control_tombstones) >= self.config.max_control_tombstones
                or node_count >= self.config.max_control_tombstones_per_node
            ):
                raise RelayStateCapacityExceeded("control tombstone capacity reached")
        terminal = TerminalOutcomeRecord(
            identity[0],
            identity[1],
            record.selected_node_id,
            tombstone_claim.control_credential_digest if tombstone_claim else "",
            tombstone_claim.consumer_identity_digest if tombstone_claim else "",
            tombstone_claim.generation if tombstone_claim else 0,
            "",
            now,
            now,
            now + self.config.terminal_retention_seconds,
            outcome=status,
            retrieval_state="completed_unavailable",
            reason=reason,
            cancellation_token_digest=self._cancellation_token_digests.get(
                identity, ""
            ),
        )
        tombstone = None
        if tombstone_claim is not None:
            tombstone = ControlTombstoneRecord(
                identity[0],
                identity[1],
                tombstone_claim.selected_node_id,
                tombstone_claim.control_credential_digest,
                tombstone_claim.consumer_identity_digest,
                tombstone_claim.generation,
                status,
                reason,
                tombstone_claim.request_deadline_epoch,
                False,
                min(now + self.config.control_tombstone_ttl_seconds, now + 300.0),
            )
        self._terminals[identity] = terminal
        if tombstone is not None:
            self._control_tombstones[identity] = tombstone
        reservation = self._reservations.pop(identity, None)
        queued = self._queued.get(identity)
        if queued is not None:
            self._remove_queued_identity_locked(identity, queued)
        self._cancellation_token_digests.pop(identity, None)
        self._queued_token_digests.pop(identity, None)
        self._progress.pop(identity, None)
        self._deferred_deadline_identities.discard(identity)
        self._discard_node_work_identity_locked(record.selected_node_id, identity)
        return TerminalTransitionResult(status, reason, True)

    def _transition_node_locked(
        self,
        node_id: str,
        supplied_digest: str | None,
        cause: str,
        now: float,
    ) -> NodeTransitionResult:
        pending = self._pending_node_transitions.get(node_id)
        record = self._records.get(node_id)
        if pending is None:
            if record is None:
                tombstone = self._node_tombstones.get(self._node_digest(node_id))
                if tombstone is not None and cause == "explicit_unregister":
                    if not hmac.compare_digest(
                        tombstone.control_credential_digest, supplied_digest or ""
                    ):
                        raise RelayStateCredentialMismatch(
                            "control credential digest does not own this transition"
                        )
                    return NodeTransitionResult(
                        "already_complete",
                        tombstone.cause,
                        tombstone.transition_epoch,
                        0,
                        0,
                        0,
                        0,
                        0,
                        False,
                    )
                former = self._former_node_authorities.get(
                    self._node_digest(node_id), {}
                )
                if cause == "explicit_unregister":
                    matching = (
                        authority
                        for owner_digest, authority in former.items()
                        if hmac.compare_digest(owner_digest, supplied_digest or "")
                    )
                else:
                    matching = (
                        authority
                        for authority in former.values()
                        if authority.cause == cause
                    )
                completed = max(
                    matching,
                    key=lambda authority: authority.transition_epoch,
                    default=None,
                )
                if completed is not None:
                    return NodeTransitionResult(
                        "already_complete",
                        completed.cause,
                        completed.transition_epoch,
                        0,
                        0,
                        0,
                        0,
                        0,
                        False,
                    )
                return NodeTransitionResult(
                    "not_found", cause, None, 0, 0, 0, 0, 0, False
                )
            if cause == "explicit_unregister":
                self._require_digest(record, supplied_digest or "")
            elif record.lease_expires_at_epoch > now:
                return NodeTransitionResult(
                    "lease_active", cause, None, 0, 0, 0, 0, 0, False
                )
            if (
                len(self._pending_node_transitions)
                >= self.config.max_pending_node_transitions
            ):
                raise RelayStateCapacityExceeded(
                    "pending node-transition capacity reached"
                )
            node_digest = self._node_digest(node_id)
            owner_fences = self._former_node_authorities.get(node_digest, {})
            owner_key = record.control_credential_digest
            fence_count = sum(
                len(authorities)
                for authorities in self._former_node_authorities.values()
            )
            if (
                owner_key not in owner_fences
                and fence_count >= self.config.max_removed_owner_fences
            ):
                raise RelayStateCapacityExceeded(
                    "removed-owner fencing capacity reached"
                )
            if (
                node_digest not in self._node_tombstones
                and len(self._node_tombstones) >= self.config.max_node_tombstones
            ):
                raise RelayStateCapacityExceeded("node tombstone capacity reached")
            status = "cancelled"
            reason = "server_unregistered"
            pending = _PendingNodeTransition(
                node_id,
                node_digest,
                record.control_credential_digest,
                cause,
                status,
                reason,
                now,
            )
            # Eligibility is removed before any work batch is attempted.
            del self._records[node_id]
            self._scheduler_states.pop(node_id, None)
            self._registration_order.pop(node_id, None)
            for fingerprint, cursor in tuple(self._fairness_cursors.items()):
                if cursor[0] == node_id:
                    del self._fairness_cursors[fingerprint]
            self._pending_node_transitions[node_id] = pending
            fence_expiry = now + self.config.terminal_retention_seconds
            self._former_node_authorities.setdefault(node_digest, {})[owner_key] = (
                _FormerNodeAuthority(owner_key, cause, status, now, fence_expiry)
            )
            self._node_tombstones[node_digest] = NodeTombstoneRecord(
                node_digest,
                record.control_credential_digest,
                cause,
                status,
                now,
                not self._node_work_identities.get(node_id),
                min(now + self.config.node_tombstone_ttl_seconds, now + 300.0),
            )
        else:
            if cause != pending.cause:
                raise RelayStateConflict("node transition cause conflicts")
            if cause == "explicit_unregister" and not hmac.compare_digest(
                pending.control_credential_digest, supplied_digest or ""
            ):
                raise RelayStateCredentialMismatch(
                    "control credential digest does not own this transition"
                )

        node_work = self._node_work_identities.get(node_id, {})
        batch = tuple(islice(node_work, self.config.node_transition_batch_size))
        reservations = queued_count = claims = outcomes = 0
        processed = 0
        for identity in batch:
            terminal = self._terminals.get(identity)
            if terminal is not None:
                self._discard_node_work_identity_locked(node_id, identity)
                processed += 1
                continue
            reservation = self._reservations.get(identity)
            queued = self._queued.get(identity)
            work = reservation or queued
            if work is None:
                self._discard_node_work_identity_locked(node_id, identity)
                processed += 1
                continue
            claim = self._claims.get(identity)
            try:
                result = self._terminalize_locked(
                    identity,
                    work,
                    pending.status,
                    pending.reason,
                    now,
                    force_claim_tombstone=claim is not None,
                )
            except RelayStateCapacityExceeded:
                break
            reservations += reservation is not None
            queued_count += queued is not None
            claims += claim is not None
            outcomes += result.new_outcome
            processed += 1
        continuation = bool(self._node_work_identities.get(node_id))
        if continuation:
            self._pending_node_transitions[node_id] = pending
        else:
            fence_expiry = now + self.config.terminal_retention_seconds
            if not math.isfinite(fence_expiry):
                raise RelayStateStoreError(
                    "former-owner retention deadline must be finite"
                )
            owner_fences = self._former_node_authorities.setdefault(
                pending.node_identity_digest, {}
            )
            owner_fences[pending.control_credential_digest] = replace(
                owner_fences[pending.control_credential_digest],
                expires_at_epoch=fence_expiry,
            )
            self._pending_node_transitions.pop(node_id, None)
            tombstone = self._node_tombstones.get(pending.node_identity_digest)
            if tombstone is not None:
                self._node_tombstones[pending.node_identity_digest] = replace(
                    tombstone, completed=True
                )
        return NodeTransitionResult(
            "transitioning" if continuation else "complete",
            pending.cause,
            pending.transition_epoch,
            processed,
            reservations,
            queued_count,
            claims,
            outcomes,
            continuation,
        )

    def _expire_locked(
        self, now: float, *, continue_pending: bool = False
    ) -> list[ComputeNodeRegistration]:
        # Lease eviction consumes retained transition capacity, so reclaim
        # expired entries before attempting any new transition.
        self._reap_retained_locked(now)
        expired = sorted(
            (
                record
                for record in self._records.values()
                if record.lease_expires_at_epoch <= now
            ),
            key=lambda record: record.node_id,
        )
        for record in expired:
            self._transition_node_locked(
                record.node_id, None, "registration_lease_expired", now
            )
        # One already-started transition is advanced per sweep.  It uses the
        # identical operation and batch bound; there is no weaker cleanup path.
        continuation = (
            next(iter(sorted(self._pending_node_transitions)), None)
            if continue_pending
            else None
        )
        if continuation is not None and not any(
            record.node_id == continuation for record in expired
        ):
            pending = self._pending_node_transitions[continuation]
            self._transition_node_locked(
                continuation,
                (
                    pending.control_credential_digest
                    if pending.cause == "explicit_unregister"
                    else None
                ),
                pending.cause,
                now,
            )
        return expired

    def _reap_locked(self, now: float) -> None:
        # Reclaim retained-result capacity before deadline terminalization.  A
        # due lifecycle must not be blocked by terminal records or control
        # tombstones whose retention windows have already elapsed.
        self._reap_retained_locked(now)

        # Absolute request deadlines are lifecycle authority and must win a CAS
        # before ordinary node/short-reservation housekeeping deletes state.
        due = [
            (identity, record)
            for identity, record in tuple(self._reservations.items())
            if record.request_deadline_epoch <= now
        ]
        due.extend(
            (identity, record)
            for identity, record in tuple(self._queued.items())
            if record.request_deadline_epoch <= now
        )
        for identity, record in due:
            if identity not in self._terminals and (
                identity in self._reservations or identity in self._queued
            ):
                try:
                    self._terminalize_locked(
                        identity, record, "expired", "request_deadline_expired", now
                    )
                except RelayStateCapacityExceeded:
                    # Keep the complete lifecycle for a later authoritative
                    # transition, but do not let one capacity-bound identity
                    # prevent reaping or unrelated store operations.
                    self._deferred_deadline_identities.add(identity)
        self._expire_locked(now)
        for identity, reservation in tuple(self._reservations.items()):
            if (
                reservation.reservation_expires_at_epoch <= now
                and identity not in self._deferred_deadline_identities
            ):
                del self._reservations[identity]
                self._cancellation_token_digests.pop(identity, None)
                self._discard_node_work_identity_locked(
                    reservation.selected_node_id, identity
                )

    def _reap_retained_locked(self, now: float) -> None:
        for identity, response in tuple(self._responses.items()):
            if response.replay_expires_at_epoch <= now:
                del self._responses[identity]
                terminal = self._terminals.get(identity)
                if (
                    terminal is not None
                    and terminal.retrieval_state == "response_ready"
                ):
                    self._terminals[identity] = replace(
                        terminal, retrieval_state="retrieval_expired"
                    )
        for identity, terminal in tuple(self._terminals.items()):
            if terminal.expires_at_epoch <= now:
                del self._terminals[identity]
        for identity, tombstone in tuple(self._control_tombstones.items()):
            if tombstone.expires_at_epoch <= now:
                del self._control_tombstones[identity]
        for node_digest, tombstone in tuple(self._node_tombstones.items()):
            if tombstone.expires_at_epoch <= now:
                del self._node_tombstones[node_digest]
        pending_authorities = {
            transition.node_identity_digest: transition.control_credential_digest
            for transition in self._pending_node_transitions.values()
        }
        for node_digest, authorities in tuple(self._former_node_authorities.items()):
            pending_owner_digest = pending_authorities.get(node_digest)
            for owner_digest, authority in tuple(authorities.items()):
                reserved_by_pending = (
                    pending_owner_digest is not None
                    and hmac.compare_digest(pending_owner_digest, owner_digest)
                )
                if authority.expires_at_epoch <= now and not reserved_by_pending:
                    del authorities[owner_digest]
            if not authorities:
                del self._former_node_authorities[node_digest]

    def _discard_node_work_identity_locked(
        self, node_id: str, identity: tuple[str, str]
    ) -> None:
        identities = self._node_work_identities.get(node_id)
        if identities is None:
            return
        identities.pop(identity, None)
        if not identities:
            self._node_work_identities.pop(node_id, None)

    def _remove_queued_identity_locked(
        self, identity: tuple[str, str], queued: QueuedRequest
    ) -> None:
        self._queued.pop(identity, None)
        self._queued_token_digests.pop(identity, None)
        self._cancellation_token_digests.pop(identity, None)
        self._progress.pop(identity, None)
        queue = self._node_queues.get(queued.selected_node_id, [])
        remaining = [item for item in queue if item != queued]
        if remaining:
            self._node_queues[queued.selected_node_id] = remaining
        else:
            self._node_queues.pop(queued.selected_node_id, None)
        self._claims.pop(identity, None)
        self._discard_node_work_identity_locked(queued.selected_node_id, identity)

    def _remove_node_reservations_locked(self, node_id: str) -> None:
        for identity, reservation in tuple(self._reservations.items()):
            if reservation.selected_node_id == node_id:
                if identity in self._deferred_deadline_identities:
                    continue
                del self._reservations[identity]
                self._cancellation_token_digests.pop(identity, None)
                self._discard_node_work_identity_locked(node_id, identity)

    def _remove_node_queue_locked(self, node_id: str) -> None:
        for queued in self._node_queues.pop(node_id, ()):
            identity = (
                queued.client_identity_digest,
                queued.request_identity_digest,
            )
            if identity in self._deferred_deadline_identities:
                self._node_queues.setdefault(node_id, []).append(queued)
                continue
            self._queued.pop(identity, None)
            self._queued_token_digests.pop(identity, None)
            self._cancellation_token_digests.pop(identity, None)
            self._claims.pop(identity, None)
            self._progress.pop(identity, None)
            self._discard_node_work_identity_locked(node_id, identity)

    def _active_fairness_fingerprints_locked(self) -> set[str]:
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
        return active_fingerprints

    def _cursor_eviction_candidate_locked(self, fingerprint: str) -> str | None:
        if fingerprint in self._fairness_cursors:
            return None
        if len(self._fairness_cursors) < self.config.max_scheduler_fingerprints:
            return None
        active = self._active_fairness_fingerprints_locked()
        candidate = min(
            (
                (activity, existing_fingerprint)
                for existing_fingerprint, (
                    _,
                    activity,
                ) in self._fairness_cursors.items()
                if existing_fingerprint not in active
            ),
            default=None,
        )
        if candidate is None:
            raise RelayStateNoCapacity("no scheduler capacity")
        return candidate[1]

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

    def _consumer_digest(self, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise RelayStateStoreError("consumer identity is invalid")
        encoded = value.encode("utf-8")
        if len(encoded) > self.config.max_consumer_identity_bytes:
            raise RelayStateStoreError("consumer identity is invalid")
        return hashlib.sha256(b"consumer\0" + encoded).hexdigest()

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
    def _enqueue_result(
        record: QueuedRequest, created: bool, state: str = "queued"
    ) -> EnqueueResult:
        return EnqueueResult(
            state,
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
    def _serialized_encrypted_envelope(
        envelope: EncryptedResponseEnvelope | EncryptedProgressEnvelope,
    ) -> bytes:
        return json.dumps(
            {
                "protocol": envelope.protocol,
                "version": envelope.version,
                "ciphertext": envelope.ciphertext,
                "cipherkey": envelope.cipherkey,
                "iv": envelope.iv,
            },
            separators=(",", ":"),
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")

    _serialized_response_envelope = _serialized_encrypted_envelope

    def _derive_acknowledgement_token(
        self, identity: tuple[str, str], accepted_at_epoch: float, response_digest: str
    ) -> str:
        """Derive a replica-stable token from canonical fixed-width state."""
        message = (
            b"token.place/relay-response-ack/v1\0"
            + bytes.fromhex(identity[0])
            + bytes.fromhex(identity[1])
            + struct.pack("!d", accepted_at_epoch)
            + bytes.fromhex(response_digest)
        )
        return hmac.new(self._acknowledgement_key, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _acknowledgement_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _acknowledgement_matches(
        self,
        identity: tuple[str, str],
        terminal: TerminalOutcomeRecord,
        supplied_token: object,
    ) -> bool:
        expected_token = self._derive_acknowledgement_token(
            identity, terminal.accepted_at_epoch, terminal.response_digest
        )
        return hmac.compare_digest(
            self._acknowledgement_digest(expected_token),
            terminal.acknowledgement_digest,
        ) and hmac.compare_digest(
            self._safe_acknowledgement_digest(supplied_token),
            terminal.acknowledgement_digest,
        )

    @classmethod
    def _safe_acknowledgement_digest(cls, token: object) -> str:
        if not isinstance(token, str) or not _SHA256_RE.fullmatch(token):
            return "0" * 64
        return cls._acknowledgement_digest(token)

    @staticmethod
    def _response_result(
        response: ResponseRecord, new_outcome: bool
    ) -> ResponseAcceptanceResult:
        return ResponseAcceptanceResult(
            "response_ready",
            response.generation,
            response.accepted_at_epoch,
            response.replay_expires_at_epoch,
            new_outcome,
        )

    @staticmethod
    def _terminal_response_result(
        terminal: TerminalOutcomeRecord,
    ) -> ResponseAcceptanceResult:
        return ResponseAcceptanceResult(
            "response_ready",
            terminal.generation,
            terminal.accepted_at_epoch,
            terminal.replay_expires_at_epoch,
            False,
        )

    @staticmethod
    def _token_digest(token: str) -> str:
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise RelayStateInvalidReservation("reservation invalid")
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _cancellation_token_digest(self, token: object) -> str:
        if not isinstance(token, str) or not token:
            raise RelayStateStoreError("cancellation proof is invalid")
        encoded = token.encode("utf-8")
        if len(encoded) > self.config.max_cancellation_token_bytes:
            raise RelayStateStoreError("cancellation proof is invalid")
        return hashlib.sha256(b"cancel\0" + encoded).hexdigest()

    def _safe_cancellation_token_digest(self, token: object) -> str:
        try:
            return self._cancellation_token_digest(token)
        except (RelayStateStoreError, UnicodeError):
            return "0" * 64

    @staticmethod
    def _safe_retrieval_credential_digest(credential: object) -> str:
        """Digest a bounded reservation credential without raising or retaining it."""
        if not isinstance(credential, str) or not _SHA256_RE.fullmatch(credential):
            return "0" * 64
        return hashlib.sha256(credential.encode("ascii")).hexdigest()

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
    def _node_digest(node_id: str) -> str:
        return hashlib.sha256(b"node\0" + node_id.encode("utf-8")).hexdigest()

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
