"""Internal Valkey connection, schema, key, and reviewed-script primitives.

This foundation intentionally implements no relay lifecycle operation and is not
imported by :mod:`relay`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import redis
from redis.exceptions import NoScriptError, RedisError, ResponseError
from redis.sentinel import Sentinel

from relay_state_store import (
    CONTEXT_TIER_TOKEN_BOUNDS,
    ComputeNodeCapabilities,
    ComputeNodeRegistration,
    EncryptedRequestEnvelope,
    EnqueueResult,
    ClaimRecord,
    ClaimResult,
    ClaimRenewalResult,
    QueuedRequest,
    RelayStateCapacityExceeded,
    RelayStateConflict,
    RelayStateCredentialMismatch,
    RelayStateInvalidReservation,
    RelayStateNoCapacity,
    RelayStateStoreConfig,
    RelayStateStoreError,
    ReservationRecord,
    SchedulerNodeState,
    SelectionResult,
)

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 5
_MAX_RESULT_BYTES = 65_536
_MAX_RESULT_ITEMS = 1_024
_MAX_RESULT_DEPTH = 8
_CLAIM_RESULT_METADATA_BYTES = 4_096
_MAX_MANIFEST_SCRIPTS = 64
_MAX_CONNECTIONS = 32
_MAX_RATE_LIMIT_WINDOW = 2**63 - 1
_REGISTRATION_FIELDS = (
    b"node_id",
    b"control_credential_digest",
    b"registered_at_epoch",
    b"supported_model_ids",
    b"active_context_tier",
    b"maximum_total_context_tokens",
    b"default_output_token_reservation",
    b"maximum_output_tokens",
    b"max_concurrency",
    b"backend_class",
    b"api_version",
    b"lease_expires_at_epoch",
)
_ROUTE_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KEY_COMPONENT_COUNTS = {
    "schema": 0,
    "nodes:lease": 0,
    "cursor": 0,
    "reservations:expiry": 0,
    "requests:deadline": 0,
    "claims:expiry": 0,
    "responses:expiry": 0,
    "control:expiry": 0,
    "node_tombstones:expiry": 0,
    "terminals:expiry": 0,
    "node": 1,
    "reservation": 1,
    "queue": 1,
    "node_tombstone": 1,
    "request": 2,
    "claim": 2,
    "response": 2,
    "progress": 2,
    "terminal": 2,
    "control": 3,
}


def _validate_optional_strings(*values: object, message: str) -> None:
    if any(
        value is not None and (not isinstance(value, str) or not value)
        for value in values
    ):
        raise ValkeyConfigurationError(message)


def _validate_script_result(
    result: object, *, max_result_bytes: int = _MAX_RESULT_BYTES
) -> None:
    total_bytes = 0
    total_items = 0
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        total_items += 1
        if total_items > _MAX_RESULT_ITEMS or depth > _MAX_RESULT_DEPTH:
            raise ValkeyScriptError("invalid reviewed script result")
        if value is None or isinstance(value, bool):
            total_bytes += 1
        elif isinstance(value, int) and not isinstance(value, bool):
            total_bytes += len(str(value))
        elif isinstance(value, bytes):
            total_bytes += len(value)
        elif isinstance(value, (list, tuple)):
            pending.extend((item, depth + 1) for item in value)
        else:
            raise ValkeyScriptError("invalid reviewed script result")
        if total_bytes > max_result_bytes:
            raise ValkeyScriptError("invalid reviewed script result")


class ValkeyFoundationError(RuntimeError):
    """A bounded, detail-free Valkey foundation failure."""


class ValkeyConfigurationError(ValkeyFoundationError):
    pass


class ValkeyUnavailableError(ValkeyFoundationError):
    pass


class ValkeyReadOnlyError(ValkeyFoundationError):
    pass


class ValkeySchemaIncompatibleError(ValkeyFoundationError):
    pass


class ValkeyScriptError(ValkeyFoundationError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class DirectPrimary:
    host: str
    port: int = 6379

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or not self.host
            or len(self.host) > 253
            or any(character.isspace() for character in self.host)
            or any(character in self.host for character in "/@")
        ):
            raise ValkeyConfigurationError("invalid direct-primary discovery")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValkeyConfigurationError("invalid direct-primary discovery")

    def __repr__(self) -> str:
        return "DirectPrimary(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class SentinelPrimary:
    sentinels: tuple[tuple[str, int], ...]
    service_name: str
    sentinel_username: str | None = None
    sentinel_password: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sentinels, tuple)
            or not self.sentinels
            or len(self.sentinels) > 32
        ):
            raise ValkeyConfigurationError("invalid Sentinel discovery")
        for endpoint in self.sentinels:
            if (
                not isinstance(endpoint, tuple)
                or len(endpoint) != 2
                or not isinstance(endpoint[0], str)
                or not endpoint[0]
                or any(character.isspace() for character in endpoint[0])
                or any(character in endpoint[0] for character in "/@")
                or isinstance(endpoint[1], bool)
                or not isinstance(endpoint[1], int)
                or not 1 <= endpoint[1] <= 65535
            ):
                raise ValkeyConfigurationError("invalid Sentinel discovery")
        if not isinstance(self.service_name, str) or not _NAMESPACE_RE.fullmatch(
            self.service_name
        ):
            raise ValkeyConfigurationError("invalid Sentinel discovery")
        _validate_optional_strings(
            self.sentinel_username,
            self.sentinel_password,
            message="invalid Sentinel credentials",
        )

    def __repr__(self) -> str:
        return "SentinelPrimary(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ValkeyConfig:
    environment: str
    cluster: str
    schema_major: int
    reader_revision: int
    writer_revision: int
    supported_schema_read_min: int
    supported_schema_read_max: int
    supported_writer_min: int
    supported_writer_max: int
    direct: DirectPrimary | None = None
    sentinel: SentinelPrimary | None = None
    connect_timeout_seconds: float = 1.0
    socket_timeout_seconds: float = 2.0
    command_timeout_seconds: float = 2.0
    retry_timeout_seconds: float = 1.0
    retry_attempts: int = 1
    tls: bool = False
    tls_ca_cert: str | None = None
    tls_client_cert: str | None = None
    tls_client_key: str | None = None
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        for value in (self.environment, self.cluster):
            if not isinstance(value, str) or not _NAMESPACE_RE.fullmatch(value):
                raise ValkeyConfigurationError("invalid namespace coordinates")
        values = (
            self.schema_major,
            self.reader_revision,
            self.writer_revision,
            self.supported_schema_read_min,
            self.supported_schema_read_max,
            self.supported_writer_min,
            self.supported_writer_max,
        )
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in values):
            raise ValkeyConfigurationError("invalid schema revisions")
        if (
            self.supported_schema_read_min > self.supported_schema_read_max
            or self.supported_writer_min > self.supported_writer_max
        ):
            raise ValkeyConfigurationError("invalid revision range")
        if self.direct is not None and not isinstance(self.direct, DirectPrimary):
            raise ValkeyConfigurationError("invalid discovery configuration")
        if self.sentinel is not None and not isinstance(self.sentinel, SentinelPrimary):
            raise ValkeyConfigurationError("invalid discovery configuration")
        if (self.direct is None) == (self.sentinel is None):
            raise ValkeyConfigurationError("exactly one discovery mode is required")
        if not isinstance(self.tls, bool):
            raise ValkeyConfigurationError("TLS flag must be boolean")
        _validate_optional_strings(
            self.tls_ca_cert,
            self.tls_client_cert,
            self.tls_client_key,
            self.username,
            self.password,
            message="invalid authentication or certificate input",
        )
        for timeout in (
            self.connect_timeout_seconds,
            self.socket_timeout_seconds,
            self.command_timeout_seconds,
            self.retry_timeout_seconds,
        ):
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
            ):
                raise ValkeyConfigurationError("timeouts must be finite and bounded")
        if (
            isinstance(self.retry_attempts, bool)
            or not isinstance(self.retry_attempts, int)
            or not 0 <= self.retry_attempts <= _MAX_RETRIES
        ):
            raise ValkeyConfigurationError("retry attempts must be bounded")
        if (
            any((self.tls_ca_cert, self.tls_client_cert, self.tls_client_key))
            and not self.tls
        ):
            raise ValkeyConfigurationError("TLS material requires TLS")
        if bool(self.tls_client_cert) != bool(self.tls_client_key):
            raise ValkeyConfigurationError("TLS client certificate and key are paired")

    @property
    def key_prefix(self) -> str:
        return f"tokenplace:{{{self.environment}:{self.cluster}}}:relay:v{self.schema_major}:"

    def key(self, family: str, *components: object) -> str:
        if not isinstance(family, str):
            raise ValkeyConfigurationError("invalid key suffix")
        component_count = _KEY_COMPONENT_COUNTS.get(family)
        if component_count is not None:
            if len(components) != component_count or any(
                not isinstance(component, str) or not _SHA256_RE.fullmatch(component)
                for component in components
            ):
                raise ValkeyConfigurationError("invalid key suffix")
        elif family == "ratelimit":
            if len(components) != 3:
                raise ValkeyConfigurationError("invalid key suffix")
            route_class, identity_digest, window = components
            if (
                not isinstance(route_class, str)
                or not _ROUTE_CLASS_RE.fullmatch(route_class)
                or not isinstance(identity_digest, str)
                or not _SHA256_RE.fullmatch(identity_digest)
                or isinstance(window, bool)
                or not isinstance(window, int)
                or not 0 <= window <= _MAX_RATE_LIMIT_WINDOW
            ):
                raise ValkeyConfigurationError("invalid key suffix")
        else:
            raise ValkeyConfigurationError("invalid key suffix")
        suffix = ":".join((family, *(str(component) for component in components)))
        key = self.key_prefix + suffix
        if key.count("{") != 1 or key.count("}") != 1:
            raise ValkeyConfigurationError("invalid key suffix")
        return key

    def __repr__(self) -> str:
        return "ValkeyConfig(<redacted>)"


@dataclass(frozen=True, slots=True)
class SchemaManifest:
    schema_major: int
    active_schema_revision: int
    active_writer_revision: int
    reader_min: int
    reader_max: int
    writer_min: int
    writer_max: int
    script_digests: Mapping[str, str]
    migration_epoch: int

    def __post_init__(self) -> None:
        ints = (
            self.schema_major,
            self.active_schema_revision,
            self.active_writer_revision,
            self.reader_min,
            self.reader_max,
            self.writer_min,
            self.writer_max,
        )
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in ints):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if self.reader_min > self.reader_max or self.writer_min > self.writer_max:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if (
            isinstance(self.migration_epoch, bool)
            or not isinstance(self.migration_epoch, int)
            or self.migration_epoch < 0
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if (
            not isinstance(self.script_digests, Mapping)
            or len(self.script_digests) > _MAX_MANIFEST_SCRIPTS
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        digests: dict[str, str] = {}
        for name, digest in self.script_digests.items():
            if (
                not isinstance(name, str)
                or len(name.encode()) > 128
                or not _NAMESPACE_RE.fullmatch(name)
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            digests[name] = digest
        object.__setattr__(self, "script_digests", MappingProxyType(digests))

    def encode(self) -> bytes:
        value = {
            "schema_major": self.schema_major,
            "active_schema_revision": self.active_schema_revision,
            "active_writer_revision": self.active_writer_revision,
            "reader_min": self.reader_min,
            "reader_max": self.reader_max,
            "writer_min": self.writer_min,
            "writer_max": self.writer_max,
            "script_digests": dict(self.script_digests),
            "migration_epoch": self.migration_epoch,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return encoded

    @classmethod
    def decode(cls, raw: bytes) -> "SchemaManifest":
        if not isinstance(raw, bytes) or len(raw) > _MAX_RESULT_BYTES:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "schema_major",
                "active_schema_revision",
                "active_writer_revision",
                "reader_min",
                "reader_max",
                "writer_min",
                "writer_max",
                "script_digests",
                "migration_epoch",
            }:
                raise ValueError
            return cls(**value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None


@dataclass(frozen=True, slots=True)
class ReviewedScript:
    name: str
    source: str
    sha256: str
    mutates: bool

    def __post_init__(self) -> None:
        actual = hashlib.sha256(self.source.encode()).hexdigest()
        if (
            not _NAMESPACE_RE.fullmatch(self.name)
            or not isinstance(self.mutates, bool)
            or not _SHA256_RE.fullmatch(self.sha256)
            or actual != self.sha256
        ):
            raise ValkeyScriptError("reviewed script digest mismatch")

    @property
    def eval_sha1(self) -> str:
        """Return the server's EVALSHA identifier; ``sha256`` is the manifest digest."""
        return hashlib.sha1(self.source.encode(), usedforsecurity=False).hexdigest()


SERVER_TIME_SOURCE = "local t = redis.call('TIME')\nreturn {t[1], t[2]}\n"
SERVER_TIME_SCRIPT = ReviewedScript(
    "server_time_v1",
    SERVER_TIME_SOURCE,
    "b60030a7ea7b76a01601d26aba460e94c4fa0d52fa472333c111f18c6701bd94",  # pragma: allowlist secret
    False,
)

REGISTRATION_TRANSITION_SOURCE = """\
local leases, node, cursor = KEYS[1], KEYS[2], KEYS[3]
local prefix, operation, digest, owner = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local ttl, capacity, batch = tonumber(ARGV[5]), tonumber(ARGV[6]), tonumber(ARGV[7])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local fields = {'node_id', 'control_credential_digest', 'registered_at_epoch',
  'supported_model_ids', 'active_context_tier', 'maximum_total_context_tokens',
  'default_output_token_reservation', 'maximum_output_tokens', 'max_concurrency',
  'backend_class', 'api_version', 'lease_expires_at_epoch'}
local function fixed_record(key)
  local record = redis.call('HMGET', key, unpack(fields))
  local bytes = 0
  for _, value in ipairs(record) do
    if not value then return nil end
    bytes = bytes + string.len(value)
  end
  local json_ok, models = pcall(cjson.decode, record[4])
  if not json_ok or type(models) ~= 'table' then return nil end
  return record, bytes
end
local addressed_expiry = redis.call('ZSCORE', leases, digest)
if addressed_expiry and tonumber(addressed_expiry) <= now then
  redis.call('DEL', node)
  redis.call('ZREM', leases, digest)
end
local due = redis.call('ZRANGEBYSCORE', leases, '-inf', now, 'LIMIT', 0, batch)
local expired = {}
local reply_bytes, reply_items = 2, 3
for _, expired_digest in ipairs(due) do
  local expired_node = prefix .. 'node:' .. expired_digest
  if operation == 'reap' then
    local record, record_bytes = fixed_record(expired_node)
    if not record then return {'schema'} end
    if reply_items + 13 > 1024 or reply_bytes + record_bytes > 65536 then break end
    table.insert(expired, record)
    reply_items = reply_items + 13
    reply_bytes = reply_bytes + record_bytes
  end
  redis.call('DEL', expired_node)
  redis.call('ZREM', leases, expired_digest)
end
local exists = redis.call('EXISTS', node) == 1
if operation == 'register' or operation == 'renew' then
  if operation == 'register' then
    if exists then
      if redis.call('HGET', node, 'control_credential_digest') ~= owner then
        return {'credential_mismatch'}
      end
    elseif redis.call('ZCOUNT', leases, '(' .. now, '+inf') >= capacity then
      return {'capacity'}
    end
  else
    if not exists then return {'not_found'} end
    if redis.call('HGET', node, 'control_credential_digest') ~= owner then
      return {'credential_mismatch'}
    end
  end
  if ttl < 0.000001 then ttl = 0.000001 end
  local deadline = now + ttl
  if deadline ~= deadline or deadline == math.huge or deadline == -math.huge then
    return {'deadline'}
  end
  if operation == 'register' and not exists then
    redis.call('HSET', node, 'node_id', ARGV[8],
      'control_credential_digest', owner, 'registered_at_epoch', now,
      'supported_model_ids', ARGV[9], 'active_context_tier', ARGV[10],
      'maximum_total_context_tokens', ARGV[11],
      'default_output_token_reservation', ARGV[12],
      'maximum_output_tokens', ARGV[13], 'max_concurrency', ARGV[14],
      'backend_class', ARGV[15], 'api_version', ARGV[16],
      'scheduler_healthy', '1', 'scheduler_draining', '0',
      'scheduler_claimed_work', '0')
  elseif operation == 'register' or ARGV[8] == '1' then
    redis.call('HSET', node, 'supported_model_ids', ARGV[9],
      'active_context_tier', ARGV[10], 'maximum_total_context_tokens', ARGV[11],
      'default_output_token_reservation', ARGV[12],
      'maximum_output_tokens', ARGV[13], 'max_concurrency', ARGV[14],
      'backend_class', ARGV[15], 'api_version', ARGV[16])
  end
  -- Backfill additive scheduler fields on every registration transition so
  -- records written by the preceding schema remain usable during rollout.
  redis.call('HSETNX', node, 'scheduler_healthy', '1')
  redis.call('HSETNX', node, 'scheduler_draining', '0')
  redis.call('HSETNX', node, 'scheduler_claimed_work', '0')
  -- Registration order is allocated once and is independent of wall-clock time.
  -- HSETNX preserves it across renewal and capability replacement, including
  -- records produced by the preceding compatible writer.
  local registration_order = redis.call('HGET', node, 'registration_order')
  if not registration_order then
    registration_order = redis.call('HINCRBY', cursor, '_registration_sequence', 1)
    redis.call('HSETNX', node, 'registration_order', registration_order)
  end
  redis.call('HSET', node, 'lease_expires_at_epoch', deadline)
  redis.call('ZADD', leases, deadline, digest)
  local record = fixed_record(node)
  if not record then return {'schema'} end
  return {'ok', record}
elseif operation == 'unregister' then
  if not exists then return {'not_found'} end
  if redis.call('HGET', node, 'control_credential_digest') ~= owner then
    return {'credential_mismatch'}
  end
  redis.call('DEL', node)
  redis.call('ZREM', leases, digest)
  return {'ok'}
elseif operation == 'reap' then
  return {'ok', expired}
end
return {'invalid'}
"""
REGISTRATION_TRANSITION_SCRIPT = ReviewedScript(
    "registration_transition_v1",
    REGISTRATION_TRANSITION_SOURCE,
    "ce651ae95d14c5980782937816e5d10c628c3ce4663f0a85f7d0470c3988e092",  # pragma: allowlist secret
    True,
)

SCHEDULER_STATE_SOURCE = """\
local leases, node = KEYS[1], KEYS[2]
local digest, owner = ARGV[1], ARGV[2]
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local expiry = redis.call('ZSCORE', leases, digest)
if not expiry or tonumber(expiry) <= now then
  if expiry then redis.call('DEL', node); redis.call('ZREM', leases, digest) end
  return {'not_found'}
end
if redis.call('HGET', node, 'control_credential_digest') ~= owner then
  return {'credential_mismatch'}
end
if redis.call('HGET', node, 'scheduler_healthy') == false or
   redis.call('HGET', node, 'scheduler_draining') == false or
   redis.call('HGET', node, 'scheduler_claimed_work') == false then
  return {'schema'}
end
redis.call('HSET', node, 'scheduler_healthy', ARGV[3],
  'scheduler_draining', ARGV[4], 'scheduler_claimed_work', ARGV[5])
return {'ok'}
"""

SCHEDULER_STATE_SCRIPT = ReviewedScript(
    "scheduler_state_v1",
    SCHEDULER_STATE_SOURCE,
    "f0305f850e9145ff71b6a2212ea03e94f7d958ebdf1b0255d8dee04787c26dea",  # pragma: allowlist secret
    True,
)

SELECT_AND_RESERVE_SOURCE = """\
local leases, expiries, deadlines, cursor, request_key, reservation_key = unpack(KEYS)
local prefix, client, request, model, tier, deadline, cancel, fingerprint,
  token_digest = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], tonumber(ARGV[6]),
  ARGV[7], ARGV[8], ARGV[9]
local ttl, requested_tokens = tonumber(ARGV[10]), tonumber(ARGV[11])
local max_res, max_client, max_node, max_depth, max_lifecycles,
  max_fingerprints, max_nodes, batch = tonumber(ARGV[12]), tonumber(ARGV[13]),
  tonumber(ARGV[14]), tonumber(ARGV[15]), tonumber(ARGV[16]), tonumber(ARGV[17]),
  tonumber(ARGV[18]), tonumber(ARGV[19])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local function valid_queue_authority(c, q, node_digest, entry)
  if not node_digest or node_digest == '' or not entry or entry == '' then return false end
  local entries = redis.call('XRANGE', prefix .. 'queue:' .. node_digest,
    entry, entry, 'COUNT', 1)
  if #entries ~= 1 or entries[1][1] ~= entry then return false end
  local ec, eq = nil, nil
  local fields = entries[1][2]
  for i=1,#fields,2 do
    if fields[i] == 'client' then ec = fields[i+1] end
    if fields[i] == 'request' then eq = fields[i+1] end
  end
  return ec == c and eq == q
end

local function reclaim(c, q)
  local qkey = prefix .. 'request:' .. c .. ':' .. q
  local v = redis.call('HMGET', qkey, 'state', 'client', 'request', 'node_digest',
    'deadline', 'token_digest', 'reservation_expires', 'queue_entry')
  local state, lifecycle_deadline = v[1], tonumber(v[5])
  if not state then return false, nil end
  if not v[2] or not v[3] or not v[4] or not lifecycle_deadline or
     v[2] ~= c or v[3] ~= q or
     (state ~= 'reserved' and state ~= 'queued' and state ~= 'claimed') then
    return false, 'schema'
  end
  local indexed_deadline = redis.call('ZSCORE', deadlines, c .. ':' .. q)
  if not indexed_deadline or math.abs(tonumber(indexed_deadline) - lifecycle_deadline) > 0.000001 then
    return false, 'schema'
  end
  if state == 'queued' or state == 'claimed' then
    if lifecycle_deadline > now then return false, nil end
    if not v[8] or v[8] == '' then return false, 'schema' end
    local entries = redis.call('XRANGE', prefix .. 'queue:' .. v[4], v[8], v[8], 'COUNT', 1)
    if #entries ~= 1 or entries[1][1] ~= v[8] then return false, 'schema' end
    local ec, eq = nil, nil
    local fields = entries[1][2]
    for i=1,#fields,2 do
      if fields[i] == 'client' then ec = fields[i+1] end
      if fields[i] == 'request' then eq = fields[i+1] end
    end
    if ec ~= c or eq ~= q then return false, 'schema' end
    redis.call('XDEL', prefix .. 'queue:' .. v[4], v[8])
    if state == 'claimed' then
      redis.call('DEL', prefix .. 'claim:' .. c .. ':' .. q)
      redis.call('ZREM', prefix .. 'claims:expiry', c .. ':' .. q)
    end
  else
    local expires = tonumber(v[7])
    if not v[6] or not expires then return false, 'schema' end
    if lifecycle_deadline > now and expires > now then return false, nil end
    local indexed_expiry = redis.call('ZSCORE', expiries, v[6])
    local rkey = prefix .. 'reservation:' .. v[6]
    local r = redis.call('HMGET', rkey, 'client', 'request', 'node_digest',
      'deadline', 'reservation_expires', 'token_digest')
    for _, value in ipairs(r) do if not value then return false, 'schema' end end
    if not indexed_expiry or math.abs(tonumber(indexed_expiry) - expires) > 0.000001 or r[1] ~= c or
       r[2] ~= q or r[3] ~= v[4] or tonumber(r[4]) ~= lifecycle_deadline or
       tonumber(r[5]) ~= expires or r[6] ~= v[6] then return false, 'schema' end
    redis.call('DEL', rkey)
    redis.call('ZREM', expiries, v[6])
  end
  redis.call('DEL', qkey)
  redis.call('ZREM', deadlines, c .. ':' .. q)
  return true, nil, v[6]
end

local expired_nodes = redis.call('ZRANGEBYSCORE', leases, '-inf', now, 'LIMIT', 0, batch)
for _, expired_node in ipairs(expired_nodes) do
  redis.call('DEL', prefix .. 'node:' .. expired_node)
  redis.call('ZREM', leases, expired_node)
end

-- Reclaim the addressed identity independently of the bounded general backlog.
local addressed, reclaim_error, addressed_token = reclaim(client, request)
if reclaim_error then return {reclaim_error} end
local cleaned = 0
local due = redis.call('ZRANGEBYSCORE', deadlines, '-inf', now, 'LIMIT', 0, batch + 1)
for _, member in ipairs(due) do
  if cleaned < batch and member ~= client .. ':' .. request then
    local colon = string.find(member, ':', 1, true)
    if not colon then return {'schema'} end
    local due_c, due_q = string.sub(member, 1, colon - 1), string.sub(member, colon + 1)
    if redis.call('EXISTS', prefix .. 'request:' .. due_c .. ':' .. due_q) == 0 then
      return {'schema'}
    end
    local ok, err = reclaim(due_c, due_q)
    if err then return {err} end
    if ok then cleaned = cleaned + 1 end
  end
end
local expired = redis.call('ZRANGEBYSCORE', expiries, '-inf', now, 'LIMIT', 0, batch + 1)
for _, token in ipairs(expired) do
  if cleaned < batch and token ~= addressed_token then
    local rkey = prefix .. 'reservation:' .. token
    local c, q = redis.call('HMGET', rkey, 'client', 'request')
    if not c or not q then return {'schema'} end
    if c ~= client or q ~= request or not addressed then
      local ok, err = reclaim(c, q)
      if err then return {err} end
      if ok then cleaned = cleaned + 1 end
    end
  end
end

if deadline <= now then return {'invalid'} end
if deadline > now + tonumber(ARGV[22]) then return {'deadline_bound'} end

local state = redis.call('HGET', request_key, 'state')
if state then
  local values = redis.call('HMGET', request_key, 'model', 'tier', 'deadline',
    'cancellation_digest', 'node_id', 'reservation_expires', 'token_digest',
    'node_digest', 'queue_entry')
  if values[1] ~= model or values[2] ~= tier or tonumber(values[3]) ~= deadline then
    return {'conflict'}
  end
  if state == 'reserved' then
    if cancel ~= '' and values[4] and values[4] ~= '' and values[4] ~= cancel then
      return {'conflict'}
    end
    if not values[7] or not values[6] or tonumber(values[6]) <= now then return {'schema'} end
    local indexed = redis.call('ZSCORE', expiries, values[7])
    local authority_key = prefix .. 'reservation:' .. values[7]
    local authority = redis.call('HMGET', authority_key, 'client', 'request', 'node_id',
      'model', 'tier', 'deadline', 'reservation_expires', 'token_digest',
      'cancellation_digest')
    for _, value in ipairs(authority) do if not value then return {'schema'} end end
    if not indexed or tonumber(indexed) ~= tonumber(values[6]) or
       authority[1] ~= client or authority[2] ~= request or authority[3] ~= values[5] or
       authority[4] ~= model or authority[5] ~= tier or tonumber(authority[6]) ~= deadline or
       tonumber(authority[7]) ~= tonumber(values[6]) or authority[8] ~= values[7] or
       authority[9] ~= values[4] then return {'schema'} end
    return {'existing', values[5], values[6], 'reserved'}
  end
  if state == 'queued' or state == 'claimed' then
    if cancel ~= '' and values[4] and values[4] ~= '' and values[4] ~= cancel then
      return {'conflict'}
    end
    if not valid_queue_authority(client, request, values[8], values[9]) then
      return {'schema'}
    end
    return {'existing', values[5], '', state}
  end
  return {'conflict'}
end
if redis.call('ZCARD', expiries) >= max_res then return {'capacity'} end

local lifecycle_members = redis.call('ZRANGE', deadlines, 0, max_lifecycles)
if #lifecycle_members >= max_lifecycles then return {'capacity'} end
local client_count = 0
local node_reservations, node_queued = {}, {}
local active_fingerprints = {}
for _, member in ipairs(lifecycle_members) do
  local colon = string.find(member, ':', 1, true)
  if not colon then return {'schema'} end
  local c, q = string.sub(member, 1, colon - 1), string.sub(member, colon + 1)
  if string.len(c) ~= 64 or string.len(q) ~= 64 or
     string.find(c, '[^0-9a-f]') or string.find(q, '[^0-9a-f]') then return {'schema'} end
  local qkey = prefix .. 'request:' .. c .. ':' .. q
  local s, node, fp = unpack(redis.call('HMGET', qkey, 'state', 'node_digest', 'fingerprint'))
  if not s or not node or not fp or
     (s ~= 'reserved' and s ~= 'queued' and s ~= 'claimed') then return {'schema'} end
  if c == client and (s == 'reserved' or s == 'queued' or s == 'claimed') then client_count = client_count + 1 end
  if s == 'reserved' then node_reservations[node] = (node_reservations[node] or 0) + 1 end
  if s == 'queued' or s == 'claimed' then node_queued[node] = (node_queued[node] or 0) + 1 end
  active_fingerprints[fp] = true
end
if client_count >= max_client then return {'capacity'} end

local candidates = redis.call('ZRANGEBYSCORE', leases, '(' .. now, '+inf', 'LIMIT', 0, max_nodes + 1)
if #candidates > max_nodes then return {'capacity'} end
local eligible = {}
for _, node_digest in ipairs(candidates) do
  local nkey = prefix .. 'node:' .. node_digest
  local values = redis.call('HMGET', nkey, 'node_id', 'supported_model_ids',
    'active_context_tier', 'maximum_total_context_tokens', 'max_concurrency',
    'registration_order', 'scheduler_healthy', 'scheduler_draining',
    'scheduler_claimed_work')
  local complete = true
  for _, value in ipairs(values) do if not value then complete = false end end
  if not complete then return {'schema'} end
  local ok, models = pcall(cjson.decode, values[2])
  if not ok or type(models) ~= 'table' then return {'schema'} end
  local supports = false
  for _, candidate_model in ipairs(models) do if candidate_model == model then supports = true end end
  local tier_tokens = nil
  if values[3] == '8k-fast' then tier_tokens = tonumber(ARGV[20]) end
  if values[3] == '64k-full' then tier_tokens = tonumber(ARGV[21]) end
  local reservations = node_reservations[node_digest] or 0
  local queued = node_queued[node_digest] or 0
  local claimed = tonumber(values[9])
  local load = reservations + queued + claimed
  if supports and tier_tokens and tier_tokens >= requested_tokens and
     tonumber(values[4]) >= requested_tokens and values[7] == '1' and values[8] == '0' and
     load < tonumber(values[5]) and reservations < max_node and
     reservations + queued < max_depth then
    table.insert(eligible, {tier_tokens, load, tonumber(values[6]), node_digest, values[1]})
  end
end
if #eligible == 0 then return {'capacity'} end
table.sort(eligible, function(a,b)
  if a[1] ~= b[1] then return a[1] < b[1] end
  if a[2] ~= b[2] then return a[2] < b[2] end
  if a[3] ~= b[3] then return a[3] < b[3] end
  return a[4] < b[4]
end)
local best_tier, best_load = eligible[1][1], eligible[1][2]
local tied = {}
for _, item in ipairs(eligible) do
  if item[1] == best_tier and item[2] == best_load then table.insert(tied, item) end
end
local previous = redis.call('HGET', cursor, fingerprint)
local selected = tied[1]
if previous then
  local previous_order = tonumber(redis.call('HGET', prefix .. 'node:' .. previous,
    'registration_order') or '-1')
  for _, item in ipairs(tied) do
    if item[3] > previous_order then selected = item; break end
  end
end

local count_raw = redis.call('HGET', cursor, '_count')
local cursor_count = count_raw and tonumber(count_raw) or 0
local activity_raw = redis.call('HGET', cursor, '_activity')
local function canonical_decimal(value)
  return value and (value == '0' or string.match(value, '^[1-9]%d*$'))
end
local function decimal_lte(left, right)
  return string.len(left) < string.len(right) or
    (string.len(left) == string.len(right) and left <= right)
end
if (count_raw and (not canonical_decimal(count_raw) or not cursor_count)) or
   cursor_count < 0 or cursor_count > max_fingerprints or
   (cursor_count == 0 and activity_raw and activity_raw ~= '0') or
   (cursor_count > 0 and not activity_raw) or
   (activity_raw and (not canonical_decimal(activity_raw) or
    not decimal_lte(activity_raw, '9223372036854775806'))) then
  return {'schema'}
end
local occupied, free_slot, fingerprint_slot = 0, nil, nil
local indexed = {}
for slot=1,max_fingerprints do
  local slot_field = '_fp:' .. slot
  local fp = redis.call('HGET', cursor, slot_field)
  if fp then
    if string.len(fp) ~= 64 or string.find(fp, '[^0-9a-f]') or indexed[fp] then
      return {'schema'}
    end
    local mapping, fp_activity = unpack(redis.call('HMGET', cursor, fp, 'a:' .. fp))
    if not mapping or string.len(mapping) ~= 64 or string.find(mapping, '[^0-9a-f]') or
       not activity_raw or not canonical_decimal(fp_activity) or fp_activity == '0' or
       not decimal_lte(fp_activity, activity_raw) then
      return {'schema'}
    end
    indexed[fp], occupied = true, occupied + 1
    if fp == fingerprint then fingerprint_slot = slot_field end
  elseif not free_slot then
    free_slot = slot_field
  end
end
if occupied ~= cursor_count or (previous and not fingerprint_slot) or
   (not previous and (fingerprint_slot or redis.call('HGET', cursor, 'a:' .. fingerprint))) then
  return {'schema'}
end

local selected_slot = fingerprint_slot
if not previous then
  if cursor_count >= max_fingerprints then
    local oldest_fp, oldest_activity, oldest_slot = nil, nil, nil
    for slot=1,max_fingerprints do
      local slot_field = '_fp:' .. slot
      local fp = redis.call('HGET', cursor, slot_field)
      local activity = tonumber(redis.call('HGET', cursor, 'a:' .. fp))
      if not active_fingerprints[fp] and (not oldest_activity or activity < oldest_activity or
         (activity == oldest_activity and fp < oldest_fp)) then
        oldest_fp, oldest_activity, oldest_slot = fp, activity, slot_field
      end
    end
    if not oldest_fp then return {'capacity'} end
    redis.call('HDEL', cursor, oldest_fp, 'a:' .. oldest_fp)
    selected_slot = oldest_slot
  else
    if not free_slot then return {'schema'} end
    redis.call('HINCRBY', cursor, '_count', 1)
    selected_slot = free_slot
  end
end
local expires = math.min(now + ttl, deadline)
redis.call('HSET', reservation_key, 'client', client, 'request', request,
  'fingerprint', fingerprint, 'node_digest', selected[4], 'node_id', selected[5],
  'model', model, 'tier', tier, 'deadline', deadline, 'reservation_expires', expires,
  'token_digest', token_digest, 'cancellation_digest', cancel)
redis.call('HSET', request_key, 'state', 'reserved', 'client', client, 'request', request,
  'node_digest', selected[4], 'node_id', selected[5], 'model', model, 'tier', tier,
  'deadline', deadline, 'reservation_expires', expires, 'token_digest', token_digest,
  'cancellation_digest', cancel, 'fingerprint', fingerprint)
redis.call('ZADD', expiries, expires, token_digest)
redis.call('ZADD', deadlines, deadline, client .. ':' .. request)
local activity = redis.call('HINCRBY', cursor, '_activity', 1)
redis.call('HSET', cursor, fingerprint, selected[4], 'a:' .. fingerprint, activity,
  selected_slot, fingerprint)
return {'created', selected[5], tostring(expires)}
"""

SELECT_AND_RESERVE_SCRIPT = ReviewedScript(
    "select_and_reserve_v1",
    SELECT_AND_RESERVE_SOURCE,
    "389fc52552c89aa804834830ef66fa197e4e97f6b62b7e0af6c0b5ee67c605a8",  # pragma: allowlist secret
    True,
)

ENQUEUE_SOURCE = """\
local leases, expiries, deadlines, request_key, reservation_key, node, queue, cursor = unpack(KEYS)
local node_digest, client, request, model, tier, deadline, token_digest,
  cancel_digest, envelope_json = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5],
  tonumber(ARGV[6]), ARGV[7], ARGV[8], ARGV[9]
local max_depth, max_queued, max_client = tonumber(ARGV[10]), tonumber(ARGV[11]), tonumber(ARGV[12])
local prefix, max_lifecycles = ARGV[13], tonumber(ARGV[14])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local batch = tonumber(ARGV[17])
local function valid_queue_authority(c, q, queued_node, entry)
  if not queued_node or queued_node == '' or not entry or entry == '' then return false end
  local entries = redis.call('XRANGE', prefix .. 'queue:' .. queued_node,
    entry, entry, 'COUNT', 1)
  if #entries ~= 1 or entries[1][1] ~= entry then return false end
  local ec, eq = nil, nil
  local fields = entries[1][2]
  for i=1,#fields,2 do
    if fields[i] == 'client' then ec = fields[i+1] end
    if fields[i] == 'request' then eq = fields[i+1] end
  end
  return ec == c and eq == q
end
local function reclaim(c, q)
  local qkey = prefix .. 'request:' .. c .. ':' .. q
  local v = redis.call('HMGET', qkey, 'state', 'client', 'request', 'node_digest',
    'deadline', 'token_digest', 'reservation_expires', 'queue_entry')
  local lifecycle_state, lifecycle_deadline = v[1], tonumber(v[5])
  if not lifecycle_state then return false, nil end
  if not v[2] or not v[3] or not v[4] or not lifecycle_deadline or
     v[2] ~= c or v[3] ~= q or
     (lifecycle_state ~= 'reserved' and lifecycle_state ~= 'queued' and
      lifecycle_state ~= 'claimed') then return false, 'schema' end
  local indexed_deadline = redis.call('ZSCORE', deadlines, c .. ':' .. q)
  if not indexed_deadline or math.abs(tonumber(indexed_deadline) - lifecycle_deadline) > 0.000001 then
    return false, 'schema'
  end
  if lifecycle_state == 'queued' or lifecycle_state == 'claimed' then
    if lifecycle_deadline > now then return false, nil end
    if not v[8] or v[8] == '' then return false, 'schema' end
    local entries = redis.call('XRANGE', prefix .. 'queue:' .. v[4], v[8], v[8], 'COUNT', 1)
    if #entries ~= 1 or entries[1][1] ~= v[8] then return false, 'schema' end
    local ec, eq = nil, nil
    local fields = entries[1][2]
    for i=1,#fields,2 do
      if fields[i] == 'client' then ec = fields[i+1] end
      if fields[i] == 'request' then eq = fields[i+1] end
    end
    if ec ~= c or eq ~= q then return false, 'schema' end
    redis.call('XDEL', prefix .. 'queue:' .. v[4], v[8])
    if lifecycle_state == 'claimed' then
      redis.call('DEL', prefix .. 'claim:' .. c .. ':' .. q)
      redis.call('ZREM', prefix .. 'claims:expiry', c .. ':' .. q)
    end
  else
    local expires = tonumber(v[7])
    if not v[6] or not expires then return false, 'schema' end
    if lifecycle_deadline > now and expires > now then return false, nil end
    local indexed_expiry = redis.call('ZSCORE', expiries, v[6])
    local rkey = prefix .. 'reservation:' .. v[6]
    local r = redis.call('HMGET', rkey, 'client', 'request', 'node_digest',
      'deadline', 'reservation_expires', 'token_digest')
    for _, value in ipairs(r) do if not value then return false, 'schema' end end
    if not indexed_expiry or math.abs(tonumber(indexed_expiry) - expires) > 0.000001 or r[1] ~= c or
       r[2] ~= q or r[3] ~= v[4] or tonumber(r[4]) ~= lifecycle_deadline or
       tonumber(r[5]) ~= expires or r[6] ~= v[6] then return false, 'schema' end
    redis.call('DEL', rkey)
    redis.call('ZREM', expiries, v[6])
  end
  redis.call('DEL', qkey)
  redis.call('ZREM', deadlines, c .. ':' .. q)
  return true, nil, v[6]
end
local addressed, reclaim_error, addressed_token = reclaim(client, request)
if reclaim_error then return {reclaim_error} end
local cleaned = 0
local due = redis.call('ZRANGEBYSCORE', deadlines, '-inf', now, 'LIMIT', 0, batch + 1)
for _, member in ipairs(due) do
  if cleaned < batch and member ~= client .. ':' .. request then
    local colon = string.find(member, ':', 1, true)
    if not colon then return {'schema'} end
    local due_c, due_q = string.sub(member, 1, colon - 1), string.sub(member, colon + 1)
    if redis.call('EXISTS', prefix .. 'request:' .. due_c .. ':' .. due_q) == 0 then
      return {'schema'}
    end
    local ok, err = reclaim(due_c, due_q)
    if err then return {err} end
    if ok then cleaned = cleaned + 1 end
  end
end
local expired = redis.call('ZRANGEBYSCORE', expiries, '-inf', now, 'LIMIT', 0, batch + 1)
for _, expired_token in ipairs(expired) do
  if cleaned < batch and expired_token ~= addressed_token then
    local c, q = redis.call('HMGET', prefix .. 'reservation:' .. expired_token,
      'client', 'request')
    if not c or not q then return {'schema'} end
    if c ~= client or q ~= request or not addressed then
      local ok, err = reclaim(c, q)
      if err then return {err} end
      if ok then cleaned = cleaned + 1 end
    end
  end
end
local state = redis.call('HGET', request_key, 'state')
if state == 'queued' or state == 'claimed' then
  local values = redis.call('HMGET', request_key, 'node_digest', 'model', 'tier', 'deadline',
    'token_digest', 'cancellation_digest', 'envelope', 'sequence', 'node_id',
    'queue_entry')
  if values[1] ~= node_digest or values[2] ~= model or values[3] ~= tier or
     tonumber(values[4]) ~= deadline or values[7] ~= envelope_json or
     values[6] ~= cancel_digest then return {'conflict'} end
  if values[5] ~= token_digest then return {'invalid'} end
  if not valid_queue_authority(client, request, values[1], values[10]) then
    return {'schema'}
  end
  return {'existing', state, values[9], values[8]}
end
if state ~= 'reserved' or deadline <= now then return {'invalid'} end
local values = redis.call('HMGET', request_key, 'node_digest', 'model', 'tier', 'deadline',
  'token_digest', 'cancellation_digest', 'node_id', 'reservation_expires')
if values[1] ~= node_digest or values[2] ~= model or values[3] ~= tier or
   tonumber(values[4]) ~= deadline or values[5] ~= token_digest then return {'invalid'} end
if values[6] ~= '' and values[6] ~= cancel_digest then return {'conflict'} end
local authority = redis.call('HMGET', reservation_key, 'client', 'request', 'node_digest',
  'node_id', 'model', 'tier', 'deadline', 'reservation_expires', 'token_digest',
  'cancellation_digest')
for _, value in ipairs(authority) do if not value then return {'invalid'} end end
if authority[1] ~= client or authority[2] ~= request or authority[3] ~= node_digest or
   authority[4] ~= values[7] or authority[5] ~= model or authority[6] ~= tier or
   tonumber(authority[7]) ~= deadline or authority[9] ~= token_digest or
   authority[10] ~= values[6] or tonumber(authority[8]) ~= tonumber(values[8]) then
  return {'invalid'}
end
local indexed_expiry = redis.call('ZSCORE', expiries, token_digest)
if tonumber(values[8]) <= now or not indexed_expiry or
   tonumber(indexed_expiry) ~= tonumber(values[8]) or
   redis.call('ZSCORE', leases, node_digest) == false or
   tonumber(redis.call('ZSCORE', leases, node_digest)) <= now or redis.call('EXISTS', node) == 0 then
  return {'invalid'}
end
if redis.call('XLEN', queue) >= max_depth then return {'capacity'} end
-- The shared deadline index contains reservations as well as queued requests.
-- Scan its configured lifecycle bound so reservations cannot hide queued work.
local members = redis.call('ZRANGE', deadlines, 0, max_lifecycles)
local queued, client_queued = 0, 0
for _, member in ipairs(members) do
  local colon = string.find(member, ':', 1, true)
  if not colon then return {'schema'} end
  local c, q = string.sub(member, 1, colon - 1), string.sub(member, colon + 1)
  if string.len(c) ~= 64 or string.len(q) ~= 64 or
     string.find(c, '[^0-9a-f]') or string.find(q, '[^0-9a-f]') then return {'schema'} end
  local s, lifecycle_node = unpack(redis.call('HMGET',
    prefix .. 'request:' .. c .. ':' .. q, 'state', 'node_digest'))
  if not s or not lifecycle_node or
     (s ~= 'reserved' and s ~= 'queued' and s ~= 'claimed') then return {'schema'} end
  if s == 'queued' or s == 'claimed' then
    queued = queued + 1
    if c == client then client_queued = client_queued + 1 end
  end
end
if queued >= max_queued or client_queued >= max_client then return {'capacity'} end
local sequence = redis.call('HINCRBY', cursor, '_queue_sequence', 1)
local entry = redis.call('XADD', queue, sequence .. '-0', 'client', client, 'request', request)
redis.call('HSET', request_key, 'state', 'queued', 'cancellation_digest', cancel_digest,
  'envelope', envelope_json, 'enqueued_at', now, 'sequence', sequence, 'queue_entry', entry,
  'client_public_key', ARGV[15], 'request_id', ARGV[16])
redis.call('DEL', reservation_key)
redis.call('ZREM', expiries, token_digest)
return {'created', 'queued', values[7], tostring(sequence)}
"""

ENQUEUE_SCRIPT = ReviewedScript(
    "enqueue_encrypted_request_v1",
    ENQUEUE_SOURCE,
    "62224003ccbab921f2ab7c1a74bf2564fb5eed5f28f198c25a374eb9986d9904",  # pragma: allowlist secret
    True,
)
CLAIM_SOURCE = """\
local leases, node, queue, expiries, cursor = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5]
local prefix, node_digest, node_id, owner, consumer = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local ttl, max_global, max_node, queue_bound, cleanup_bound = tonumber(ARGV[6]), tonumber(ARGV[7]), tonumber(ARGV[8]), tonumber(ARGV[9]), tonumber(ARGV[10])
local t = redis.call('TIME'); local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local function digest(value)
  return value and string.len(value) == 64 and not string.find(value, '[^0-9a-f]')
end
local function finite(value)
  local number = tonumber(value)
  return number and number == number and math.abs(number) ~= math.huge and number
end
local indexed_lease = redis.call('ZSCORE', leases, node_digest)
if not indexed_lease or tonumber(indexed_lease) <= now or redis.call('EXISTS', node) == 0 then return {'owner'} end
local nv = redis.call('HMGET', node, 'node_id', 'control_credential_digest', 'scheduler_draining', 'lease_expires_at_epoch')
for i=1,#nv do if not nv[i] then return {'schema'} end end
local node_lease = tonumber(nv[4])
if not node_lease or math.abs(node_lease - tonumber(indexed_lease)) > 0.000001 then return {'schema'} end
if nv[1] ~= node_id or nv[2] ~= owner or (nv[3] ~= '0' and nv[3] ~= '1') then return {'owner'} end
local entries = redis.call('XRANGE', queue, '-', '+', 'COUNT', queue_bound)
for _, entry in ipairs(entries) do
  local f = entry[2]; local client, request
  for i=1,#f,2 do if f[i]=='client' then client=f[i+1] elseif f[i]=='request' then request=f[i+1] end end
  if not client or not request or #f ~= 4 or string.len(client) ~= 64 or string.len(request) ~= 64 then return {'schema'} end
  local rk = prefix .. 'request:' .. client .. ':' .. request
  local rv = redis.call('HMGET', rk, 'state','client','request','client_public_key','request_id','node_id','node_digest','deadline','envelope','sequence','queue_entry','claim_generation')
  local present=0 for i=1,#rv do if rv[i] then present=present+1 end end
  if present == 0 then return {'schema'} elseif present < 11 then return {'schema'} end
  local deadline = tonumber(rv[8]); local sequence = tonumber(rv[10])
  if not deadline or not sequence or rv[2]~=client or rv[3]~=request or rv[6]~=node_id or rv[7]~=node_digest or rv[11]~=entry[1] then return {'schema'} end
  if deadline > now and (rv[1]=='queued' or rv[1]=='claimed') then
    local member=client .. ':' .. request; local ck=prefix .. 'claim:' .. client .. ':' .. request
    local fields={'client','request','node_digest','node_id','owner_digest','consumer_digest','deadline','sequence','generation','lease_expires'}
    local old=redis.call('HMGET',ck,unpack(fields)); local old_present=0
    for i=1,#old do if old[i] then old_present=old_present+1 end end
    if old_present ~= 0 and old_present ~= #old then return {'schema'} end
    local reclaim = false
    if old_present == 0 then
      if rv[1] ~= 'queued' or rv[12] then return {'schema'} end
    else
      local old_deadline=tonumber(old[7]); local old_sequence=tonumber(old[8]); local old_generation=tonumber(old[9]); local old_expiry=tonumber(old[10])
      local indexed_expiry=redis.call('ZSCORE',expiries,member)
      if not old_deadline or not old_sequence or not old_generation or old_generation < 1 or not old_expiry or
         old[1]~=client or old[2]~=request or old[3]~=node_digest or old[4]~=node_id or
         not digest(old[3]) or not digest(old[5]) or not digest(old[6]) or
         old_deadline~=deadline or old_sequence~=sequence or (indexed_expiry and tonumber(indexed_expiry)~=old_expiry) or
         (old_expiry > now and not indexed_expiry) or
         rv[1]~='claimed' or tonumber(rv[12])~=old_generation then return {'schema'} end
      if old_expiry <= now then reclaim = true else reclaim = nil end
    end
    if reclaim ~= nil then
    local live = redis.call('ZRANGEBYSCORE', expiries, '(' .. now, '+inf', 'LIMIT', 0, max_global + 1)
    local node_live = 0
    for _, live_member in ipairs(live) do
      local sep = string.find(live_member, ':', 1, true)
      local live_client = sep and string.sub(live_member,1,sep-1)
      local live_request = sep and string.sub(live_member,sep+1)
      if not digest(live_client) or not digest(live_request) then return {'schema'} end
      local live_claim = prefix .. 'claim:' .. live_client .. ':' .. live_request
      local lv = redis.call('HMGET',live_claim,unpack(fields)); local lv_present=0
      for i=1,#lv do if lv[i] then lv_present=lv_present+1 end end
      local live_deadline=finite(lv[7]); local live_sequence=finite(lv[8]); local live_generation=finite(lv[9]); local live_expiry=finite(lv[10])
      local live_score=finite(redis.call('ZSCORE',expiries,live_member))
      local live_request_key=prefix .. 'request:' .. live_client .. ':' .. live_request
      local lifecycle=redis.call('HMGET',live_request_key,'state','client','request','node_id','node_digest','deadline','sequence','claim_generation')
      local lifecycle_present=0
      for i=1,#lifecycle do if lifecycle[i] then lifecycle_present=lifecycle_present+1 end end
      if lv_present ~= #fields or lv[1]~=live_client or lv[2]~=live_request or
         not digest(lv[3]) or not lv[4] or string.len(lv[4]) == 0 or not digest(lv[5]) or not digest(lv[6]) or
         not live_deadline or not live_sequence or live_sequence < 1 or live_sequence % 1 ~= 0 or
         not live_generation or live_generation < 1 or live_generation % 1 ~= 0 or
         not live_expiry or not live_score or live_score ~= live_expiry or live_expiry <= now or
         live_deadline <= now or live_expiry > live_deadline or lifecycle_present ~= #lifecycle or
         lifecycle[1] ~= 'claimed' or lifecycle[2] ~= live_client or lifecycle[3] ~= live_request or
         lifecycle[4] ~= lv[4] or lifecycle[5] ~= lv[3] or finite(lifecycle[6]) ~= live_deadline or
         finite(lifecycle[7]) ~= live_sequence or finite(lifecycle[8]) ~= live_generation then return {'schema'} end
      if lv[3] == node_digest then node_live = node_live + 1 end
    end
    local expired = redis.call('ZRANGEBYSCORE', expiries, '-inf', now, 'LIMIT', 0, cleanup_bound)
    if #expired > 0 then redis.call('ZREM', expiries, unpack(expired)) end
    if #live >= max_global then return {'capacity'} end
    if node_live >= max_node then return {'capacity'} end
    local generation=redis.call('HINCRBY',cursor,'_claim_generation',1)
    local expires=math.min(now+ttl,deadline)
    if expires <= now then return {'empty'} end
    redis.call('HSET',ck,'client',client,'request',request,'node_digest',node_digest,'node_id',node_id,
      'owner_digest',owner,'consumer_digest',consumer,'deadline',rv[8],'sequence',rv[10],
      'generation',generation,'lease_expires',expires)
    redis.call('ZADD',expiries,expires,member); redis.call('HSET',rk,'state','claimed','claim_generation',generation)
    return {reclaim and 'reclaimed' or 'claimed',tostring(generation),tostring(expires),rv[8],rv[4],rv[5],rv[9]}
    end
  end
end
return {'empty'}
"""
CLAIM_SCRIPT = ReviewedScript(
    "claim_queued_request_v1",
    CLAIM_SOURCE,
    "70a4c142082c9192a39886ee6046a54c34bee48974f2d9cb97392e4836096695",  # pragma: allowlist secret
    True,
)

RENEW_CLAIM_SOURCE = """\
local leases,node,claim,request,expiries=KEYS[1],KEYS[2],KEYS[3],KEYS[4],KEYS[5]
local node_digest,node_id,owner,consumer,client,request_digest,generation,ttl=ARGV[1],ARGV[2],ARGV[3],ARGV[4],ARGV[5],ARGV[6],ARGV[7],tonumber(ARGV[8])
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local function finite(value)
  local number=tonumber(value)
  return number and number==number and math.abs(number)~=math.huge and number
end
local lease=redis.call('ZSCORE',leases,node_digest)
if not lease or tonumber(lease)<=now or redis.call('EXISTS',node)==0 then return {'owner_mismatch'} end
local nv=redis.call('HMGET',node,'node_id','control_credential_digest','scheduler_draining','lease_expires_at_epoch')
if not nv[1] or not nv[2] or not nv[3] then return {'schema'} end
local node_lease=tonumber(nv[4])
if not node_lease or math.abs(node_lease-tonumber(lease))>0.000001 then return {'schema'} end
if nv[1]~=node_id or nv[2]~=owner or (nv[3]~='0' and nv[3]~='1') then return {'owner_mismatch'} end
local cv=redis.call('HMGET',claim,'client','request','node_digest','node_id','owner_digest','consumer_digest','deadline','sequence','generation','lease_expires')
local present=0 for i=1,#cv do if cv[i] then present=present+1 end end
if present==0 then return {'missing_or_expired'} elseif present~=#cv then return {'schema'} end
local sequence=finite(cv[8]); local current=finite(cv[9]); local expires=finite(cv[10]); local deadline=finite(cv[7])
local indexed_expiry=finite(redis.call('ZSCORE',expiries,client .. ':' .. request_digest))
if not sequence or sequence<1 or sequence%1~=0 or not current or current<1 or current%1~=0 or not expires or not deadline or
   not indexed_expiry or indexed_expiry~=expires or expires>deadline then return {'schema'} end
if tostring(current)~=generation then return {'stale_generation',tostring(current)} end
if cv[1]~=client or cv[2]~=request_digest or cv[3]~=node_digest or cv[4]~=node_id or cv[5]~=owner or cv[6]~=consumer then return {'owner_mismatch'} end
if expires<=now or deadline<=now then return {'missing_or_expired'} end
local rv=redis.call('HMGET',request,'state','client','request','node_id','node_digest','deadline','sequence','claim_generation')
for i=1,#rv do if not rv[i] then return {'schema'} end end
local request_deadline=finite(rv[6]); local request_sequence=finite(rv[7]); local request_generation=finite(rv[8])
if not request_deadline or not request_sequence or request_sequence<1 or request_sequence%1~=0 or
   not request_generation or request_generation<1 or request_generation%1~=0 then return {'schema'} end
if rv[1]~='claimed' or rv[2]~=client or rv[3]~=request_digest or rv[4]~=node_id or
   rv[5]~=node_digest or request_deadline~=deadline or request_sequence~=sequence or request_generation~=current then return {'schema'} end
local renewed=math.min(now+ttl,deadline); if renewed<=now then return {'missing_or_expired'} end
redis.call('HSET',claim,'lease_expires',renewed); redis.call('ZADD',expiries,renewed,client .. ':' .. request_digest)
return {'continued',tostring(current),tostring(renewed)}
"""
RENEW_CLAIM_SCRIPT = ReviewedScript(
    "renew_claim_v1",
    RENEW_CLAIM_SOURCE,
    "1d49310715988bf6b1cf5040d870a49ca21a0760a4b498da2feba673fc44f95f",  # pragma: allowlist secret
    True,
)

SCRIPT_REGISTRY: Mapping[str, ReviewedScript] = MappingProxyType(
    {
        SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT,
        REGISTRATION_TRANSITION_SCRIPT.name: REGISTRATION_TRANSITION_SCRIPT,
        SCHEDULER_STATE_SCRIPT.name: SCHEDULER_STATE_SCRIPT,
        SELECT_AND_RESERVE_SCRIPT.name: SELECT_AND_RESERVE_SCRIPT,
        ENQUEUE_SCRIPT.name: ENQUEUE_SCRIPT,
        CLAIM_SCRIPT.name: CLAIM_SCRIPT,
        RENEW_CLAIM_SCRIPT.name: RENEW_CLAIM_SCRIPT,
    }
)
SCRIPT_DIGESTS: Mapping[str, str] = MappingProxyType(
    {name: script.sha256 for name, script in SCRIPT_REGISTRY.items()}
)


class ValkeyFoundation:
    """Owns an explicit pool and exposes only foundation-level operations."""

    def __init__(self, config: ValkeyConfig, expected_manifest: SchemaManifest):
        if not isinstance(config, ValkeyConfig):
            raise ValkeyConfigurationError("invalid Valkey configuration")
        if not isinstance(expected_manifest, SchemaManifest):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        self.config = config
        self.expected_manifest = expected_manifest
        self.check_read_compatible(expected_manifest)
        self.check_write_compatible(expected_manifest)
        self._client = self._create_client()

    def __repr__(self) -> str:
        return "ValkeyFoundation(<redacted>)"

    def _connection_kwargs(self) -> dict[str, Any]:
        cfg = self.config
        kwargs = {
            "username": cfg.username,
            "password": cfg.password,
            "socket_connect_timeout": cfg.connect_timeout_seconds,
            "socket_timeout": min(
                cfg.socket_timeout_seconds,
                cfg.command_timeout_seconds,
                cfg.retry_timeout_seconds,
            ),
            "retry_on_error": [],
            "decode_responses": False,
            "max_connections": _MAX_CONNECTIONS,
        }
        if cfg.tls:
            kwargs.update(
                ssl_ca_certs=cfg.tls_ca_cert,
                ssl_certfile=cfg.tls_client_cert,
                ssl_keyfile=cfg.tls_client_key,
            )
        return kwargs

    def _create_client(self) -> redis.Redis:
        kwargs = self._connection_kwargs()
        try:
            if self.config.direct:
                connection_class = (
                    redis.SSLConnection if self.config.tls else redis.Connection
                )
                pool = redis.ConnectionPool(
                    host=self.config.direct.host,
                    port=self.config.direct.port,
                    connection_class=connection_class,
                    **kwargs,
                )
                return redis.Redis(connection_pool=pool)
            assert self.config.sentinel
            sentinel_kwargs = {
                "username": self.config.sentinel.sentinel_username,
                "password": self.config.sentinel.sentinel_password,
                "socket_connect_timeout": self.config.connect_timeout_seconds,
                "socket_timeout": self.config.socket_timeout_seconds,
                "max_connections": _MAX_CONNECTIONS,
            }
            if self.config.tls:
                sentinel_kwargs.update(
                    ssl=True,
                    ssl_ca_certs=self.config.tls_ca_cert,
                    ssl_certfile=self.config.tls_client_cert,
                    ssl_keyfile=self.config.tls_client_key,
                )
                kwargs["ssl"] = True
            sentinel = Sentinel(
                self.config.sentinel.sentinels,
                sentinel_kwargs=sentinel_kwargs,
                **kwargs,
            )
            return sentinel.master_for(self.config.sentinel.service_name)
        except RedisError:
            raise ValkeyUnavailableError("state backend unavailable") from None
        except (TypeError, ValueError):
            raise ValkeyConfigurationError(
                "invalid Valkey connection configuration"
            ) from None

    def close(self) -> None:
        self._client.close()
        self._client.connection_pool.disconnect()

    def _call(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        deadline = time.monotonic() + self.config.retry_timeout_seconds
        for attempt in range(self.config.retry_attempts + 1):
            try:
                return operation(*args, **kwargs)
            except NoScriptError:
                raise
            except ResponseError as exc:
                if "READONLY" in str(exc).upper():
                    raise ValkeyReadOnlyError("state backend is not writable") from None
                raise ValkeyUnavailableError("state backend command failed") from None
            except RedisError:
                if attempt >= self.config.retry_attempts:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05 * (2**attempt), remaining))
        raise ValkeyUnavailableError("state backend unavailable") from None

    def _call_mutating_script(self, operation: Any, *args: Any) -> Any:
        # A lost reply may follow a committed mutation, so replay cannot be safe.
        try:
            return operation(*args)
        except NoScriptError:
            raise
        except ResponseError as exc:
            if "READONLY" in str(exc).upper():
                raise ValkeyReadOnlyError("state backend is not writable") from None
            raise ValkeyUnavailableError("state backend command failed") from None
        except RedisError:
            raise ValkeyUnavailableError("state backend unavailable") from None

    def initialize_manifest(self) -> SchemaManifest:
        # Never persist an expected value this process could not itself use.
        self.check_read_compatible(self.expected_manifest)
        self.check_write_compatible(self.expected_manifest)
        raw = self.expected_manifest.encode()
        key = self.config.key("schema")
        self.check_read_compatible(self.expected_manifest)
        self.check_write_compatible(self.expected_manifest)
        self._call(self._client.set, key, raw, nx=True)
        stored = self._call(self._client.get, key)
        if stored is None:
            raise ValkeyUnavailableError("state backend command failed")
        manifest = SchemaManifest.decode(stored)
        self.check_read_compatible(manifest)
        self.check_write_compatible(manifest)
        return manifest

    def read_manifest(self) -> SchemaManifest:
        raw = self._call(self._client.get, self.config.key("schema"))
        if raw is None:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return SchemaManifest.decode(raw)

    def check_read_compatible(self, manifest: SchemaManifest) -> None:
        c, e = self.config, self.expected_manifest
        if (
            manifest.schema_major != c.schema_major
            or not manifest.reader_min <= c.reader_revision <= manifest.reader_max
            or not c.supported_schema_read_min
            <= manifest.active_schema_revision
            <= c.supported_schema_read_max
            or dict(manifest.script_digests) != dict(SCRIPT_DIGESTS)
            or manifest.migration_epoch != e.migration_epoch
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")

    def check_write_compatible(self, manifest: SchemaManifest) -> None:
        self.check_read_compatible(manifest)
        c = self.config
        if (
            not manifest.writer_min <= c.writer_revision <= manifest.writer_max
            or not manifest.writer_min
            <= manifest.active_writer_revision
            <= manifest.writer_max
            or not c.supported_writer_min
            <= manifest.active_writer_revision
            <= c.supported_writer_max
            or not c.supported_writer_min <= c.writer_revision <= c.supported_writer_max
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")

    def execute(
        self,
        script_name: str,
        keys: tuple[str, ...] = (),
        args: tuple[bytes, ...] = (),
        *,
        max_result_bytes: int = _MAX_RESULT_BYTES,
    ) -> Any:
        script = SCRIPT_REGISTRY.get(script_name)
        if script is None:
            raise ValkeyScriptError("unknown reviewed script")
        manifest = self.read_manifest()
        self.check_read_compatible(manifest)
        if script.mutates:
            self.check_write_compatible(manifest)
        if manifest.script_digests.get(script.name) != script.sha256:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            if script.mutates:
                self.check_read_compatible(manifest)
                self.check_write_compatible(manifest)
            dispatch = self._call_mutating_script if script.mutates else self._call
            result = dispatch(
                self._client.evalsha, script.eval_sha1, len(keys), *keys, *args
            )
        except NoScriptError:
            try:
                loaded = self._call(self._client.script_load, script.source)
            except NoScriptError:
                raise ValkeyScriptError("reviewed script recovery failed") from None
            loaded = loaded.decode() if isinstance(loaded, bytes) else loaded
            if loaded != script.eval_sha1:
                raise ValkeyScriptError("reviewed script digest mismatch")
            manifest = self.read_manifest()
            self.check_read_compatible(manifest)
            if script.mutates:
                self.check_write_compatible(manifest)
            try:
                dispatch = self._call_mutating_script if script.mutates else self._call
                result = dispatch(
                    self._client.evalsha, script.eval_sha1, len(keys), *keys, *args
                )
            except NoScriptError:
                raise ValkeyScriptError("reviewed script recovery failed") from None
        _validate_script_result(result, max_result_bytes=max_result_bytes)
        return result

    def server_time(self) -> tuple[int, int]:
        result = self.execute(SERVER_TIME_SCRIPT.name)
        try:
            seconds, micros = (int(part) for part in result)
        except (TypeError, ValueError):
            raise ValkeyScriptError("invalid reviewed script result") from None
        if seconds < 0 or not 0 <= micros < 1_000_000:
            raise ValkeyScriptError("invalid reviewed script result")
        return seconds, micros

    def readiness(self) -> None:
        if self._call(self._client.ping) is not True:
            raise ValkeyUnavailableError("state backend unavailable")
        role = self._call(self._client.role)
        if (
            not isinstance(role, (list, tuple))
            or not role
            or role[0] not in (b"master", "master")
        ):
            raise ValkeyReadOnlyError("state backend is not writable")
        manifest = self.read_manifest()
        self.check_read_compatible(manifest)
        self.check_write_compatible(manifest)


class ValkeyRegistrationStore:
    """Internal Valkey implementation through reservation and encrypted enqueue.

    This deliberately does not implement or advertise ``RelayStateStore``: the
    remaining coordination transitions must exist before runtime selection is safe.
    """

    def __init__(
        self, foundation: ValkeyFoundation, config: RelayStateStoreConfig
    ) -> None:
        if not isinstance(foundation, ValkeyFoundation) or not isinstance(
            config, RelayStateStoreConfig
        ):
            raise RelayStateStoreError("invalid Valkey registration configuration")
        self._foundation = foundation
        self._config = config

    def __repr__(self) -> str:
        return "ValkeyRegistrationStore(<redacted>)"

    @property
    def config(self) -> RelayStateStoreConfig:
        return self._config

    def close(self) -> None:
        self._foundation.close()

    @staticmethod
    def _node_digest(node_id: str) -> str:
        return hashlib.sha256(b"node\0" + node_id.encode("utf-8")).hexdigest()

    def _validate_node_id(self, node_id: str) -> None:
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id.encode()) > self.config.max_node_id_bytes
        ):
            raise RelayStateStoreError(
                "node ID must be non-empty and within byte bound"
            )

    @staticmethod
    def _validate_digest(value: str) -> None:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise RelayStateStoreError(
                "control credential digest must be lowercase SHA-256"
            )

    @staticmethod
    def _capability_args(capabilities: ComputeNodeCapabilities) -> tuple[bytes, ...]:
        models = json.dumps(
            capabilities.supported_model_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(models) > _MAX_RESULT_BYTES:
            raise RelayStateStoreError("supported model IDs exceed byte bound")
        return (
            models,
            capabilities.active_context_tier.encode(),
            str(capabilities.maximum_total_context_tokens).encode(),
            str(capabilities.default_output_token_reservation).encode(),
            str(capabilities.maximum_output_tokens).encode(),
            str(capabilities.max_concurrency).encode(),
            capabilities.backend_class.encode(),
            capabilities.api_version.encode(),
        )

    def _keys(self, digest: str) -> tuple[str, ...]:
        cfg = self._foundation.config
        return (
            cfg.key("nodes:lease"),
            cfg.key("node", digest),
            cfg.key("cursor"),
        )

    def _transition(
        self,
        operation: str,
        node_id: str,
        owner: str,
        extra: tuple[bytes, ...],
    ) -> tuple[str, list[Any]]:
        digest = self._node_digest(node_id)
        ttl_arg = repr(float(self.config.lease_ttl_seconds)).encode("ascii")
        args = (
            self._foundation.config.key_prefix.encode(),
            operation.encode(),
            digest.encode(),
            owner.encode(),
            ttl_arg,
            str(self.config.max_compute_nodes).encode(),
            str(self.config.node_transition_batch_size).encode(),
            *extra,
        )
        result = self._foundation.execute(
            REGISTRATION_TRANSITION_SCRIPT.name, self._keys(digest), args
        )
        if not isinstance(result, (list, tuple)) or not result:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        status = result[0]
        if isinstance(status, bytes):
            try:
                code = status.decode("ascii")
            except UnicodeDecodeError:
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
        elif isinstance(status, str):
            code = status
        else:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        expected_lengths = {
            "capacity": 1,
            "credential_mismatch": 1,
            "deadline": 1,
            "not_found": 1,
            "schema": 1,
            "ok": 2 if operation in {"register", "renew", "reap"} else 1,
        }
        if code not in expected_lengths or len(result) != expected_lengths[code]:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if code == "capacity":
            raise RelayStateCapacityExceeded(
                "compute-node registration capacity reached"
            )
        if code == "credential_mismatch":
            raise RelayStateCredentialMismatch("control credential digest mismatch")
        if code == "deadline":
            raise RelayStateStoreError("registration deadline must be finite")
        if code == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return code, list(result[1:])

    @staticmethod
    def _record_from_script(raw: object) -> ComputeNodeRegistration:
        record = ValkeyRegistrationStore._fixed_record(raw)
        if record is None:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return ValkeyRegistrationStore._decode_record(record)

    @staticmethod
    def _fixed_record(raw: object) -> dict[bytes, bytes] | None:
        if not isinstance(raw, (list, tuple)) or len(raw) != len(_REGISTRATION_FIELDS):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if all(value is None for value in raw):
            return None
        if any(not isinstance(value, bytes) for value in raw):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if sum(len(value) for value in raw) > _MAX_RESULT_BYTES:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return dict(zip(_REGISTRATION_FIELDS, raw))

    @staticmethod
    def _decode_record(raw: Mapping[bytes, bytes]) -> ComputeNodeRegistration:
        try:
            model_ids = json.loads(raw[b"supported_model_ids"])
            if not isinstance(model_ids, list):
                raise ValueError
            capabilities = ComputeNodeCapabilities(
                supported_model_ids=tuple(model_ids),
                active_context_tier=raw[b"active_context_tier"].decode(),
                maximum_total_context_tokens=int(raw[b"maximum_total_context_tokens"]),
                default_output_token_reservation=int(
                    raw[b"default_output_token_reservation"]
                ),
                maximum_output_tokens=int(raw[b"maximum_output_tokens"]),
                max_concurrency=int(raw[b"max_concurrency"]),
                backend_class=raw[b"backend_class"].decode(),
                api_version=raw[b"api_version"].decode(),
            )
            return ComputeNodeRegistration(
                node_id=raw[b"node_id"].decode(),
                capabilities=capabilities,
                control_credential_digest=raw[b"control_credential_digest"].decode(),
                registered_at_epoch=float(raw[b"registered_at_epoch"]),
                lease_expires_at_epoch=float(raw[b"lease_expires_at_epoch"]),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None

    def _read(self, node_id: str, now: float) -> ComputeNodeRegistration | None:
        digest = self._node_digest(node_id)
        score = self._foundation._call(
            self._foundation._client.zscore,
            self._foundation.config.key("nodes:lease"),
            digest,
        )
        if score is None or not math.isfinite(score) or score <= now:
            return None
        raw = self._foundation._call(
            self._foundation._client.hmget,
            self._foundation.config.key("node", digest),
            _REGISTRATION_FIELDS,
        )
        record = self._fixed_record(raw)
        return self._decode_record(record) if record is not None else None

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
        _, returned = self._transition(
            "register",
            node_id,
            control_credential_digest,
            (node_id.encode(), *self._capability_args(capabilities)),
        )
        if len(returned) != 1:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return self._record_from_script(returned[0])

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
        extra = (
            (b"1", *self._capability_args(capabilities))
            if capabilities is not None
            else (b"0", *(b"" for _ in range(8)))
        )
        code, returned = self._transition(
            "renew", node_id, control_credential_digest, extra
        )
        if code == "not_found":
            return None
        if len(returned) != 1:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return self._record_from_script(returned[0])

    def get(self, node_id: str) -> ComputeNodeRegistration | None:
        self._validate_node_id(node_id)
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        seconds, micros = self._foundation.server_time()
        return self._read(node_id, seconds + micros / 1_000_000)

    def list(self) -> tuple[ComputeNodeRegistration, ...]:
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        seconds, micros = self._foundation.server_time()
        now = seconds + micros / 1_000_000
        digests = self._foundation._call(
            self._foundation._client.zrangebyscore,
            self._foundation.config.key("nodes:lease"),
            f"({now}",
            "+inf",
            start=0,
            num=self.config.max_compute_nodes,
        )
        records = []
        # Whole-list snapshots are intentionally non-transactional; each record is
        # bounded, and nodes removed between the index and hash reads are omitted.
        for digest in digests:
            if not isinstance(digest, bytes) or not re.fullmatch(
                rb"[0-9a-f]{64}", digest
            ):
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
            raw = self._foundation._call(
                self._foundation._client.hmget,
                self._foundation.config.key("node", digest.decode()),
                _REGISTRATION_FIELDS,
            )
            record = self._fixed_record(raw)
            if record is None:
                continue
            records.append(self._decode_record(record))
        return tuple(sorted(records, key=lambda record: record.node_id))

    def expire(self) -> tuple[ComputeNodeRegistration, ...]:
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        _, returned = self._transition("reap", "", "", ())
        if len(returned) != 1 or not isinstance(returned[0], (list, tuple)):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        expired = [self._record_from_script(raw) for raw in returned[0]]
        return tuple(sorted(expired, key=lambda record: record.node_id))

    def unregister(self, node_id: str, control_credential_digest: str) -> bool:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        return (
            self._transition("unregister", node_id, control_credential_digest, ())[0]
            == "ok"
        )

    @staticmethod
    def _ascii_status(result: object) -> tuple[str, list[object]]:
        if not isinstance(result, (list, tuple)) or not result:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        value = result[0]
        try:
            status = value.decode("ascii") if isinstance(value, bytes) else value
        except UnicodeDecodeError:
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None
        if not isinstance(status, str):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return status, list(result[1:])

    @staticmethod
    def _decode_text(value: object) -> str:
        if not isinstance(value, bytes) or len(value) > _MAX_RESULT_BYTES:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None

    def _identity(self, client_public_key: str, request_id: str) -> tuple[str, str]:
        def digest(value: str, domain: bytes) -> str:
            if not isinstance(value, str) or not value:
                raise RelayStateStoreError("request identity is invalid")
            encoded = value.encode("utf-8")
            if len(encoded) > self.config.max_identity_bytes:
                raise RelayStateStoreError("request identity is invalid")
            return hashlib.sha256(domain + encoded).hexdigest()

        return digest(client_public_key, b"client\0"), digest(request_id, b"request\0")

    def _model_tier_deadline(
        self, model: str, tier: str, deadline: float
    ) -> tuple[str, str, float]:
        normalized_model = model.strip().lower() if isinstance(model, str) else ""
        normalized_tier = tier.strip().lower() if isinstance(tier, str) else ""
        if (
            not normalized_model
            or len(normalized_model.encode()) > self.config.max_model_id_bytes
        ):
            raise RelayStateStoreError("requested model is invalid")
        if normalized_tier not in CONTEXT_TIER_TOKEN_BOUNDS:
            raise RelayStateStoreError("requested context tier is invalid")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise RelayStateStoreError("request deadline must be finite")
        return normalized_model, normalized_tier, float(deadline)

    def _cancellation_digest(self, token: object, *, optional: bool = False) -> str:
        if optional and token is None:
            return ""
        if not isinstance(token, str) or not token:
            raise RelayStateStoreError("cancellation proof is invalid")
        encoded = token.encode("utf-8")
        if len(encoded) > self.config.max_cancellation_token_bytes:
            raise RelayStateStoreError("cancellation proof is invalid")
        return hashlib.sha256(b"cancel\0" + encoded).hexdigest()

    def set_scheduler_state(
        self, node_id: str, control_credential_digest: str, state: SchedulerNodeState
    ) -> bool:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        if not isinstance(state, SchedulerNodeState):
            raise RelayStateStoreError("scheduler state must be SchedulerNodeState")
        digest = self._node_digest(node_id)
        result = self._foundation.execute(
            SCHEDULER_STATE_SCRIPT.name,
            self._keys(digest),
            (
                digest.encode(),
                control_credential_digest.encode(),
                (b"1" if state.healthy else b"0"),
                (b"1" if state.draining else b"0"),
                str(state.claimed_work).encode(),
            ),
        )
        status, values = self._ascii_status(result)
        if values or status not in {"ok", "not_found", "credential_mismatch", "schema"}:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if status == "credential_mismatch":
            raise RelayStateCredentialMismatch("control credential digest mismatch")
        if status == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return status == "ok"

    def select_and_reserve(
        self,
        client_public_key: str,
        request_id: str,
        requested_model_id: str,
        requested_context_tier: str,
        request_deadline_epoch: float,
        cancellation_token: str | None = None,
    ) -> SelectionResult:
        client, request = self._identity(client_public_key, request_id)
        model, tier, deadline = self._model_tier_deadline(
            requested_model_id, requested_context_tier, request_deadline_epoch
        )
        cancellation = self._cancellation_digest(cancellation_token, optional=True)
        fingerprint = hashlib.sha256(f"{model}\0{tier}".encode()).hexdigest()
        raw_token = secrets.token_hex(32)
        token_digest = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        cfg = self._foundation.config
        keys = (
            cfg.key("nodes:lease"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("cursor"),
            cfg.key("request", client, request),
            cfg.key("reservation", token_digest),
        )
        tier_bounds = tuple(
            str(CONTEXT_TIER_TOKEN_BOUNDS[name]).encode()
            for name in ("8k-fast", "64k-full")
        )
        args = (
            cfg.key_prefix.encode(),
            client.encode(),
            request.encode(),
            model.encode(),
            tier.encode(),
            repr(deadline).encode(),
            cancellation.encode(),
            fingerprint.encode(),
            token_digest.encode(),
            repr(float(self.config.reservation_ttl_seconds)).encode(),
            str(CONTEXT_TIER_TOKEN_BOUNDS[tier]).encode(),
            str(self.config.max_reservations).encode(),
            str(self.config.max_reservations_per_client).encode(),
            str(self.config.max_reservations_per_node).encode(),
            str(self.config.max_queue_depth_per_node).encode(),
            str(self.config.max_request_lifecycles).encode(),
            str(self.config.max_scheduler_fingerprints).encode(),
            str(self.config.max_compute_nodes).encode(),
            str(self.config.node_transition_batch_size).encode(),
            *tier_bounds,
            repr(float(self.config.max_request_ttl_seconds)).encode(),
        )
        status, values = self._ascii_status(
            self._foundation.execute(SELECT_AND_RESERVE_SCRIPT.name, keys, args)
        )
        if status == "capacity":
            raise RelayStateNoCapacity("no scheduler capacity")
        if status == "invalid":
            raise RelayStateInvalidReservation("request deadline expired")
        if status == "deadline_bound":
            raise RelayStateStoreError("request deadline exceeds its configured bound")
        if status == "conflict":
            raise RelayStateConflict("request identity conflict")
        if status == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if status not in {"created", "existing"} or len(values) != (
            2 if status == "created" else 3
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        node_id = self._decode_text(values[0])
        expires_text = self._decode_text(values[1])
        state = "reserved" if status == "created" else self._decode_text(values[2])
        expires = float(expires_text) if expires_text else None
        return SelectionResult(
            node_id,
            model,
            tier,
            deadline,
            expires,
            raw_token if status == "created" else None,
            status == "created",
            state,
        )

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
        client, request = self._identity(client_public_key, request_id)
        self._validate_node_id(selected_node_id)
        model, tier, deadline = self._model_tier_deadline(
            requested_model_id, requested_context_tier, request_deadline_epoch
        )
        if not isinstance(envelope, EncryptedRequestEnvelope):
            raise RelayStateStoreError("envelope must be EncryptedRequestEnvelope")
        encoded = json.dumps(
            {
                "protocol": envelope.protocol,
                "version": envelope.version,
                "ciphertext": envelope.ciphertext,
                "cipherkey": envelope.cipherkey,
                "iv": envelope.iv,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(encoded) > self.config.max_envelope_bytes:
            raise RelayStateStoreError(
                "encrypted envelope exceeds its configured byte bound"
            )
        if not isinstance(reservation_token, str) or not re.fullmatch(
            r"[0-9a-f]{64}", reservation_token
        ):
            raise RelayStateInvalidReservation("reservation invalid")
        token_digest = hashlib.sha256(reservation_token.encode("ascii")).hexdigest()
        cancellation = self._cancellation_digest(cancellation_token)
        node_digest = self._node_digest(selected_node_id)
        cfg = self._foundation.config
        keys = (
            cfg.key("nodes:lease"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("request", client, request),
            cfg.key("reservation", token_digest),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            cfg.key("cursor"),
        )
        args = (
            node_digest.encode(),
            client.encode(),
            request.encode(),
            model.encode(),
            tier.encode(),
            repr(deadline).encode(),
            token_digest.encode(),
            cancellation.encode(),
            encoded,
            str(self.config.max_queue_depth_per_node).encode(),
            str(self.config.max_queued_requests).encode(),
            str(self.config.max_queued_requests_per_client).encode(),
            cfg.key_prefix.encode(),
            str(self.config.max_request_lifecycles).encode(),
            client_public_key.encode(),
            request_id.encode(),
            str(self.config.node_transition_batch_size).encode(),
        )
        status, values = self._ascii_status(
            self._foundation.execute(ENQUEUE_SCRIPT.name, keys, args)
        )
        if status == "invalid":
            raise RelayStateInvalidReservation("reservation invalid")
        if status == "conflict":
            raise RelayStateConflict("request identity conflict")
        if status == "capacity":
            raise RelayStateNoCapacity("no scheduler capacity")
        if status == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if status not in {"created", "existing"} or len(values) != 3:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return EnqueueResult(
            self._decode_text(values[0]),
            self._decode_text(values[1]),
            deadline,
            int(self._decode_text(values[2])),
            status == "created",
        )

    def _consumer_digest(self, consumer_identity: str) -> str:
        if not isinstance(consumer_identity, str) or not consumer_identity:
            raise RelayStateStoreError("consumer identity is invalid")
        encoded = consumer_identity.encode("utf-8")
        if len(encoded) > self.config.max_consumer_identity_bytes:
            raise RelayStateStoreError("consumer identity is invalid")
        return hashlib.sha256(b"consumer\0" + encoded).hexdigest()

    @staticmethod
    def _decode_request_envelope(raw: bytes) -> EncryptedRequestEnvelope:
        if not isinstance(raw, bytes):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "protocol",
                "version",
                "ciphertext",
                "cipherkey",
                "iv",
            }:
                raise ValueError
            return EncryptedRequestEnvelope(**value)
        except (TypeError, ValueError, json.JSONDecodeError, RelayStateStoreError):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None

    def claim_queued_request(
        self, node_id: str, control_credential_digest: str, consumer_identity: str
    ) -> ClaimResult:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        consumer = self._consumer_digest(consumer_identity)
        node_digest = self._node_digest(node_id)
        cfg = self._foundation.config
        keys = (
            cfg.key("nodes:lease"),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            cfg.key("claims:expiry"),
            cfg.key("cursor"),
        )
        args = (
            cfg.key_prefix.encode(),
            node_digest.encode(),
            node_id.encode(),
            control_credential_digest.encode(),
            consumer.encode(),
            repr(float(self.config.claim_ttl_seconds)).encode(),
            str(self.config.max_claims).encode(),
            str(self.config.max_claims_per_node).encode(),
            str(self.config.max_queue_depth_per_node).encode(),
            str(self.config.node_transition_batch_size).encode(),
        )
        status, values = self._ascii_status(
            self._foundation.execute(
                CLAIM_SCRIPT.name,
                keys,
                args,
                max_result_bytes=(
                    self.config.max_envelope_bytes
                    + (2 * self.config.max_identity_bytes)
                    + _CLAIM_RESULT_METADATA_BYTES
                ),
            )
        )
        if status == "owner":
            raise RelayStateCredentialMismatch("claim owner is invalid")
        if status == "capacity":
            raise RelayStateCapacityExceeded("claim capacity reached")
        if status == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if status == "empty" and not values:
            return ClaimResult("empty")
        if status not in {"claimed", "reclaimed"} or len(values) != 6:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            generation = int(self._decode_text(values[0]))
            lease = float(self._decode_text(values[1]))
            deadline = float(self._decode_text(values[2]))
            if (
                generation < 1
                or not math.isfinite(lease)
                or not math.isfinite(deadline)
                or lease > deadline
            ):
                raise ValueError
        except ValueError:
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None
        client_public_key, request_id, envelope_raw = values[3:]
        if (
            not isinstance(client_public_key, bytes)
            or not client_public_key
            or len(client_public_key) > self.config.max_identity_bytes
            or not isinstance(request_id, bytes)
            or not request_id
            or len(request_id) > self.config.max_identity_bytes
            or not isinstance(envelope_raw, bytes)
            or len(envelope_raw) > self.config.max_envelope_bytes
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return ClaimResult(
            status,
            generation,
            lease,
            deadline,
            self._decode_text(client_public_key),
            self._decode_text(request_id),
            self._decode_request_envelope(envelope_raw),
        )

    def renew_claim(
        self,
        node_id: str,
        control_credential_digest: str,
        consumer_identity: str,
        client_public_key: str,
        request_id: str,
        generation: int,
    ) -> ClaimRenewalResult:
        self._validate_node_id(node_id)
        self._validate_digest(control_credential_digest)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise RelayStateStoreError("claim generation is invalid")
        consumer = self._consumer_digest(consumer_identity)
        client, request = self._identity(client_public_key, request_id)
        node_digest = self._node_digest(node_id)
        cfg = self._foundation.config
        keys = (
            cfg.key("nodes:lease"),
            cfg.key("node", node_digest),
            cfg.key("claim", client, request),
            cfg.key("request", client, request),
            cfg.key("claims:expiry"),
        )
        args = (
            node_digest.encode(),
            node_id.encode(),
            control_credential_digest.encode(),
            consumer.encode(),
            client.encode(),
            request.encode(),
            str(generation).encode(),
            repr(float(self.config.claim_ttl_seconds)).encode(),
        )
        status, values = self._ascii_status(
            self._foundation.execute(RENEW_CLAIM_SCRIPT.name, keys, args)
        )
        if status == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if status in {"missing_or_expired", "owner_mismatch"} and not values:
            return ClaimRenewalResult(status)
        try:
            if status == "stale_generation" and len(values) == 1:
                current = int(self._decode_text(values[0]))
                if current < 1:
                    raise ValueError
                return ClaimRenewalResult(status, current)
            if status == "continued" and len(values) == 2:
                current = int(self._decode_text(values[0]))
                lease = float(self._decode_text(values[1]))
                if current < 1 or not math.isfinite(lease):
                    raise ValueError
                return ClaimRenewalResult(status, current, lease)
        except (TypeError, ValueError, OverflowError):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None
        raise ValkeySchemaIncompatibleError("state schema incompatible")

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
        if acknowledge:
            raise RelayStateStoreError(
                "control tombstones are not implemented by Valkey state"
            )
        return self.renew_claim(
            node_id,
            control_credential_digest,
            consumer_identity,
            client_public_key,
            request_id,
            generation,
        )

    def _live_claims(
        self, node_id: str
    ) -> tuple[tuple[QueuedRequest, ClaimRecord], ...]:
        self._validate_node_id(node_id)
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        seconds, micros = self._foundation.server_time()
        now = seconds + micros / 1_000_000
        cfg = self._foundation.config
        node_digest = self._node_digest(node_id)
        node_fields = (
            b"node_id",
            b"control_credential_digest",
            b"lease_expires_at_epoch",
        )
        node_raw = self._foundation._call(
            self._foundation._client.hmget,
            cfg.key("node", node_digest),
            node_fields,
        )
        if isinstance(node_raw, list) and node_raw and all(x is None for x in node_raw):
            return ()
        if (
            not isinstance(node_raw, list)
            or len(node_raw) != len(node_fields)
            or any(not isinstance(x, bytes) for x in node_raw)
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            node_lease = float(node_raw[2])
            indexed_node_lease = self._foundation._call(
                self._foundation._client.zscore, cfg.key("nodes:lease"), node_digest
            )
            indexed_node_lease = float(indexed_node_lease)
        except (TypeError, ValueError, OverflowError):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None
        if (
            node_raw[0] != node_id.encode()
            or not 0 < len(node_raw[0]) <= self.config.max_node_id_bytes
            or not _SHA256_RE.fullmatch(self._decode_text(node_raw[1]))
            or not math.isfinite(node_lease)
            or not math.isfinite(indexed_node_lease)
            or abs(indexed_node_lease - node_lease) > 1e-6
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if node_lease <= now:
            return ()
        members = self._foundation._call(
            self._foundation._client.zrangebyscore,
            cfg.key("claims:expiry"),
            f"({now}",
            "+inf",
            start=0,
            num=self.config.max_claims,
        )
        if not isinstance(members, list) or len(members) > self.config.max_claims:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        result = []
        cf = (
            b"client",
            b"request",
            b"node_digest",
            b"node_id",
            b"owner_digest",
            b"consumer_digest",
            b"deadline",
            b"sequence",
            b"generation",
            b"lease_expires",
        )
        rf = (
            b"state",
            b"client",
            b"request",
            b"client_public_key",
            b"request_id",
            b"node_id",
            b"model",
            b"tier",
            b"deadline",
            b"envelope",
            b"enqueued_at",
            b"sequence",
            b"claim_generation",
            b"queue_entry",
        )
        for member_raw in members:
            member = self._decode_text(member_raw)
            parts = member.split(":")
            if len(parts) != 2 or any(not _SHA256_RE.fullmatch(x) for x in parts):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            cr = self._foundation._call(
                self._foundation._client.hmget, cfg.key("claim", *parts), cf
            )
            if isinstance(cr, list) and cr and all(x is None for x in cr):
                continue
            if (
                not isinstance(cr, list)
                or len(cr) != len(cf)
                or any(not isinstance(x, bytes) for x in cr)
                or sum(map(len, cr)) > _MAX_RESULT_BYTES
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            c = dict(zip(cf, cr))
            claim_node_digest = self._decode_text(c[b"node_digest"])
            if not _SHA256_RE.fullmatch(claim_node_digest):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            if claim_node_digest != node_digest:
                continue
            rr = self._foundation._call(
                self._foundation._client.hmget, cfg.key("request", *parts), rf
            )
            if isinstance(rr, list) and rr and all(x is None for x in rr):
                continue
            if (
                not isinstance(rr, list)
                or len(rr) != len(rf)
                or any(not isinstance(x, bytes) for x in rr)
                or sum(map(len, rr))
                > self.config.max_envelope_bytes + _MAX_RESULT_BYTES
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            r = dict(zip(rf, rr))
            try:
                deadline = float(c[b"deadline"])
                lease = float(c[b"lease_expires"])
                seq = int(c[b"sequence"])
                gen = int(c[b"generation"])
                request_deadline = float(r[b"deadline"])
                enqueued_at = float(r[b"enqueued_at"])
                request_sequence = int(r[b"sequence"])
                request_generation = int(r[b"claim_generation"])
                model = self._decode_text(r[b"model"])
                tier = self._decode_text(r[b"tier"])
                indexed_expiry = self._foundation._call(
                    self._foundation._client.zscore,
                    cfg.key("claims:expiry"),
                    member,
                )
                indexed_expiry = float(indexed_expiry)
                if (
                    c[b"client"] != parts[0].encode()
                    or c[b"request"] != parts[1].encode()
                    or c[b"node_id"] != node_id.encode()
                    or not 0 < len(c[b"node_id"]) <= self.config.max_node_id_bytes
                    or c[b"owner_digest"] != node_raw[1]
                    or not _SHA256_RE.fullmatch(self._decode_text(c[b"owner_digest"]))
                    or not _SHA256_RE.fullmatch(
                        self._decode_text(c[b"consumer_digest"])
                    )
                    or not math.isfinite(indexed_expiry)
                    or abs(indexed_expiry - lease) > 1e-6
                    or not math.isfinite(deadline)
                    or not math.isfinite(lease)
                    or not math.isfinite(request_deadline)
                    or not math.isfinite(enqueued_at)
                    or gen < 1
                    or seq < 1
                    or lease <= now
                    or deadline <= now
                    or lease > deadline
                    or r[b"state"] != b"claimed"
                    or r[b"client"] != c[b"client"]
                    or r[b"request"] != c[b"request"]
                    or r[b"node_id"] != c[b"node_id"]
                    or not 0 < len(r[b"node_id"]) <= self.config.max_node_id_bytes
                    or request_deadline != deadline
                    or request_sequence != seq
                    or request_generation != gen
                    or r[b"queue_entry"] != f"{seq}-0".encode("ascii")
                    or not 0
                    < len(r[b"client_public_key"])
                    <= self.config.max_identity_bytes
                    or not 0 < len(r[b"request_id"]) <= self.config.max_identity_bytes
                    or not 0 < len(r[b"model"]) <= self.config.max_model_id_bytes
                    or not 0 < len(r[b"tier"]) <= self.config.max_model_id_bytes
                    or model != model.strip().lower()
                    or tier not in CONTEXT_TIER_TOKEN_BOUNDS
                    or len(r[b"envelope"]) > self.config.max_envelope_bytes
                    or hashlib.sha256(b"client\0" + r[b"client_public_key"]).hexdigest()
                    != parts[0]
                    or hashlib.sha256(b"request\0" + r[b"request_id"]).hexdigest()
                    != parts[1]
                ):
                    raise ValueError
                stream = self._foundation._call(
                    self._foundation._client.xrange,
                    cfg.key("queue", node_digest),
                    min=r[b"queue_entry"],
                    max=r[b"queue_entry"],
                    count=1,
                )
                if stream != [
                    (
                        r[b"queue_entry"],
                        {b"client": c[b"client"], b"request": c[b"request"]},
                    )
                ]:
                    raise ValueError
                env = self._decode_request_envelope(r[b"envelope"])
                queued = QueuedRequest(
                    parts[0],
                    parts[1],
                    self._decode_text(r[b"client_public_key"]),
                    self._decode_text(r[b"request_id"]),
                    node_id,
                    model,
                    tier,
                    deadline,
                    env,
                    enqueued_at,
                    seq,
                )
                claim = ClaimRecord(
                    parts[0],
                    parts[1],
                    self._decode_text(c[b"consumer_digest"]),
                    node_id,
                    self._decode_text(c[b"owner_digest"]),
                    deadline,
                    env,
                    seq,
                    gen,
                    lease,
                )
            except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
            result.append((queued, claim))
        return tuple(sorted(result, key=lambda x: x[1].sequence))

    def active_claims(self, node_id: str) -> tuple[ClaimRecord, ...]:
        return tuple(pair[1] for pair in self._live_claims(node_id))

    def claimed_request(
        self, node_id: str, request_id: str
    ) -> tuple[QueuedRequest, ClaimRecord] | None:
        if not isinstance(request_id, str) or not request_id:
            raise RelayStateStoreError("request id is required")
        encoded = request_id.encode("utf-8")
        if len(encoded) > self.config.max_identity_bytes:
            raise RelayStateStoreError("request identity is invalid")
        digest = hashlib.sha256(b"request\0" + encoded).hexdigest()
        return next(
            (
                pair
                for pair in self._live_claims(node_id)
                if pair[0].request_identity_digest == digest
            ),
            None,
        )

    def list_reservations(self) -> tuple[ReservationRecord, ...]:
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        seconds, micros = self._foundation.server_time()
        now = seconds + micros / 1_000_000
        cfg = self._foundation.config
        tokens = self._foundation._call(
            self._foundation._client.zrangebyscore,
            cfg.key("reservations:expiry"),
            f"({now}",
            "+inf",
            start=0,
            num=self.config.max_reservations,
        )
        records: list[ReservationRecord] = []
        fields = (
            b"client",
            b"request",
            b"fingerprint",
            b"node_id",
            b"model",
            b"tier",
            b"deadline",
            b"reservation_expires",
            b"token_digest",
        )
        for raw_token in tokens:
            token = self._decode_text(raw_token)
            if not _SHA256_RE.fullmatch(token):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            raw = self._foundation._call(
                self._foundation._client.hmget,
                cfg.key("reservation", token),
                fields,
            )
            # A cleanup transition may legitimately remove the hash after the
            # bounded index snapshot. Treat an entirely absent record as gone,
            # while malformed or partially present authority fails closed.
            if isinstance(raw, list) and raw and all(value is None for value in raw):
                continue
            if (
                not isinstance(raw, list)
                or len(raw) != len(fields)
                or any(not isinstance(value, bytes) for value in raw)
                or sum(len(value) for value in raw) > _MAX_RESULT_BYTES
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            value = dict(zip(fields, raw))
            try:
                decoded = [self._decode_text(value[field]) for field in fields[:6]]
                deadline = float(value[b"deadline"])
                expires = float(value[b"reservation_expires"])
                stored_token = self._decode_text(value[b"token_digest"])
                if (
                    any(not _SHA256_RE.fullmatch(item) for item in decoded[:3])
                    or not _SHA256_RE.fullmatch(stored_token)
                    or stored_token != token
                    or not math.isfinite(deadline)
                    or not math.isfinite(expires)
                    or expires <= now
                    or deadline <= now
                    or decoded[5] not in CONTEXT_TIER_TOKEN_BOUNDS
                ):
                    raise ValueError
                records.append(
                    ReservationRecord(*decoded, deadline, expires, stored_token)
                )
            except ValueError:
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
        return tuple(records)

    def queued_requests(self, node_id: str) -> tuple[QueuedRequest, ...]:
        self._validate_node_id(node_id)
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        seconds, micros = self._foundation.server_time()
        now = seconds + micros / 1_000_000
        node_digest = self._node_digest(node_id)
        cfg = self._foundation.config
        entries = self._foundation._call(
            self._foundation._client.xrange,
            cfg.key("queue", node_digest),
            min="-",
            max="+",
            count=self.config.max_queue_depth_per_node,
        )
        records: list[QueuedRequest] = []
        for entry in entries:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) != 2
                or not isinstance(entry[1], dict)
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            client = entry[1].get(b"client")
            request = entry[1].get(b"request")
            if not isinstance(client, bytes) or not isinstance(request, bytes):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            fields = (
                b"state",
                b"client",
                b"request",
                b"client_public_key",
                b"request_id",
                b"node_id",
                b"model",
                b"tier",
                b"deadline",
                b"envelope",
                b"enqueued_at",
                b"sequence",
            )
            raw_values = self._foundation._call(
                self._foundation._client.hmget,
                cfg.key(
                    "request", self._decode_text(client), self._decode_text(request)
                ),
                fields,
            )
            if (
                isinstance(raw_values, list)
                and raw_values
                and all(value is None for value in raw_values)
            ):
                continue
            if (
                not isinstance(raw_values, list)
                or len(raw_values) != len(fields)
                or any(not isinstance(value, bytes) for value in raw_values)
                or sum(len(value) for value in raw_values)
                > (self.config.max_envelope_bytes + _MAX_RESULT_BYTES)
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            raw = dict(zip(fields, raw_values))
            try:
                if raw[b"state"] != b"queued":
                    continue
                decoded_client = self._decode_text(raw[b"client"])
                decoded_request = self._decode_text(raw[b"request"])
                if (
                    decoded_client != self._decode_text(client)
                    or decoded_request != self._decode_text(request)
                    or not _SHA256_RE.fullmatch(decoded_client)
                    or not _SHA256_RE.fullmatch(decoded_request)
                ):
                    raise ValueError
                deadline = float(raw[b"deadline"])
                enqueued_at = float(raw[b"enqueued_at"])
                sequence = int(raw[b"sequence"])
                if not math.isfinite(deadline) or not math.isfinite(enqueued_at):
                    raise ValueError
                if deadline <= now:
                    continue
                if sequence < 1:
                    raise ValueError
                envelope_value = json.loads(raw[b"envelope"])
                envelope = EncryptedRequestEnvelope(**envelope_value)
                records.append(
                    QueuedRequest(
                        decoded_client,
                        decoded_request,
                        self._decode_text(raw[b"client_public_key"]),
                        self._decode_text(raw[b"request_id"]),
                        self._decode_text(raw[b"node_id"]),
                        self._decode_text(raw[b"model"]),
                        self._decode_text(raw[b"tier"]),
                        deadline,
                        envelope,
                        enqueued_at,
                        sequence,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
        return tuple(records)
