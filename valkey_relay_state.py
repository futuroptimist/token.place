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
    QueuedRequest,
)

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 5
_MAX_RESULT_BYTES = 65_536
_MAX_RESULT_ITEMS = 1_024
_MAX_RESULT_DEPTH = 8
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
    "registration:sequence": 0,
    "queue:sequence": 0,
    "reservations": 0,
    "requests": 0,
    "cursors": 0,
    "queues": 0,
    "node": 1,
    "reservation": 1,
    "queue": 1,
    "client:reservations": 1,
    "client:requests": 1,
    "client:queue": 1,
    "node:reservations": 1,
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


def _validate_script_result(result: object) -> None:
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
        if total_bytes > _MAX_RESULT_BYTES:
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
local leases, node, registration_sequence = KEYS[1], KEYS[2], KEYS[3]
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
    local registration_order = redis.call('INCR', registration_sequence)
    redis.call('HSET', node, 'node_id', ARGV[8],
      'control_credential_digest', owner, 'registered_at_epoch', now,
      'supported_model_ids', ARGV[9], 'active_context_tier', ARGV[10],
      'maximum_total_context_tokens', ARGV[11],
      'default_output_token_reservation', ARGV[12],
      'maximum_output_tokens', ARGV[13], 'max_concurrency', ARGV[14],
      'backend_class', ARGV[15], 'api_version', ARGV[16],
      'scheduler_healthy', '1', 'scheduler_draining', '0',
      'scheduler_claimed_work', '0', 'registration_order', registration_order)
  elseif operation == 'register' or ARGV[8] == '1' then
    redis.call('HSET', node, 'supported_model_ids', ARGV[9],
      'active_context_tier', ARGV[10], 'maximum_total_context_tokens', ARGV[11],
      'default_output_token_reservation', ARGV[12],
      'maximum_output_tokens', ARGV[13], 'max_concurrency', ARGV[14],
      'backend_class', ARGV[15], 'api_version', ARGV[16])
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
    "8a97c3ec1459b176314d9faf89f8659c7ac22a544862c8dd8f67c5446b88fd57",  # pragma: allowlist secret
    True,
)

SCHEDULER_STATE_SOURCE = """\
local leases, node = KEYS[1], KEYS[2]
local owner = ARGV[1]
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local expiry = redis.call('ZSCORE', leases, ARGV[2])
if not expiry or tonumber(expiry) <= now then
  if expiry then redis.call('DEL', node); redis.call('ZREM', leases, ARGV[2]) end
  return {'not_found'}
end
if redis.call('HGET', node, 'control_credential_digest') ~= owner then
  return {'credential_mismatch'}
end
if not redis.call('HGET', node, 'registration_order') then return {'schema'} end
redis.call('HSET', node, 'scheduler_healthy', ARGV[3],
  'scheduler_draining', ARGV[4], 'scheduler_claimed_work', ARGV[5])
return {'ok'}
"""

SCHEDULER_STATE_SCRIPT = ReviewedScript(
    "scheduler_state_v1",
    SCHEDULER_STATE_SOURCE,
    "e7e52d01570ee07ed8bb71626b1d124e5c02d4f729a10f882c985f66639aa697",  # pragma: allowlist secret
    True,
)

SELECT_RESERVE_SOURCE = """\
local leases, expiry_index, requests, reservations, cursors = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5]
local prefix, client, request, identity, model, tier, tier_tokens, deadline =
  ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6], tonumber(ARGV[7]), tonumber(ARGV[8])
local fingerprint, token_digest, cancellation = ARGV[9], ARGV[10], ARGV[11]
local ttl, max_ttl, max_nodes, batch = tonumber(ARGV[12]), tonumber(ARGV[13]), tonumber(ARGV[14]), tonumber(ARGV[15])
local max_res, max_client, max_node, max_depth, max_lifecycle, max_cursors =
  tonumber(ARGV[16]), tonumber(ARGV[17]), tonumber(ARGV[18]), tonumber(ARGV[19]), tonumber(ARGV[20]), tonumber(ARGV[21])
local t = redis.call('TIME'); local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local lifecycle = prefix .. 'request:' .. client .. ':' .. request
local function reap()
  local due = redis.call('ZRANGEBYSCORE', expiry_index, '-inf', now, 'LIMIT', 0, batch)
  for _, token in ipairs(due) do
    local rkey = prefix .. 'reservation:' .. token
    local vals = redis.call('HMGET', rkey, 'client', 'request', 'identity')
    if vals[1] and vals[2] and vals[3] then
      local lkey = prefix .. 'request:' .. vals[1] .. ':' .. vals[2]
      if redis.call('HGET', lkey, 'state') == 'reserved' and redis.call('HGET', lkey, 'token_digest') == token then
        redis.call('DEL', lkey); redis.call('ZREM', requests, vals[3])
        redis.call('ZREM', prefix .. 'client:requests:' .. vals[1], vals[3])
        local node = redis.call('HGET', rkey, 'node_digest')
        if node then redis.call('ZREM', prefix .. 'node:reservations:' .. node, vals[3]) end
        redis.call('ZREM', prefix .. 'client:reservations:' .. vals[1], vals[3])
      end
    end
    redis.call('DEL', rkey); redis.call('ZREM', expiry_index, token); redis.call('ZREM', reservations, token)
  end
end
reap()
if deadline <= now then return {'invalid'} end
if deadline > now + max_ttl then return {'deadline'} end
local existing = redis.call('HMGET', lifecycle, 'state','node_id','model','tier','deadline','reservation_expiry','cancellation_digest')
if existing[1] then
  if existing[3] ~= model or existing[4] ~= tier or tonumber(existing[5]) ~= deadline then return {'conflict'} end
  if existing[7] and existing[7] ~= '' and (cancellation == '' or existing[7] ~= cancellation) then return {'conflict'} end
  if existing[1] == 'reserved' then return {'existing',existing[2],existing[6],'reserved'} end
  if existing[1] == 'queued' or existing[1] == 'claimed' then return {'existing',existing[2],'',existing[1]} end
  return {'conflict'}
end
if redis.call('ZCARD', reservations) >= max_res or redis.call('ZCARD', requests) >= max_lifecycle then return {'capacity'} end
if redis.call('ZCARD', prefix .. 'client:requests:' .. client) >= max_client then return {'capacity'} end
local live = redis.call('ZRANGEBYSCORE', leases, '(' .. now, '+inf', 'LIMIT', 0, max_nodes)
local candidates = {}
for _, node_digest in ipairs(live) do
  local nk = prefix .. 'node:' .. node_digest
  local v = redis.call('HMGET', nk, 'node_id','supported_model_ids','active_context_tier','maximum_total_context_tokens','max_concurrency','scheduler_healthy','scheduler_draining','scheduler_claimed_work','registration_order')
  if not v[9] then return {'schema'} end
  local ok, models = pcall(cjson.decode, v[2]); if not ok or type(models) ~= 'table' then return {'schema'} end
  local supports = false; for _, m in ipairs(models) do if m == model then supports = true end end
  local sizes = {['2k']=2048,['4k']=4096,['8k-fast']=8192,['16k']=16384,['32k']=32768,['64k']=65536,['128k']=131072}
  local size = sizes[v[3]]
  local nr = redis.call('ZCARD', prefix .. 'node:reservations:' .. node_digest)
  local nq = redis.call('ZCARD', prefix .. 'queue:' .. node_digest)
  local load = nr + nq + tonumber(v[8])
  if supports and size and size >= tier_tokens and tonumber(v[4]) >= tier_tokens and v[6] == '1' and v[7] == '0'
    and load < tonumber(v[5]) and nr < max_node and nr + nq < max_depth then
    table.insert(candidates, {size,load,tonumber(v[9]),v[1],node_digest})
  end
end
if #candidates == 0 then return {'capacity'} end
table.sort(candidates, function(a,b) if a[1]~=b[1] then return a[1]<b[1] elseif a[2]~=b[2] then return a[2]<b[2] else return a[3]<b[3] end end)
local best_tier, best_load = candidates[1][1], candidates[1][2]
local tied = {}; for _, c in ipairs(candidates) do if c[1]==best_tier and c[2]==best_load then table.insert(tied,c) end end
local cursor = redis.call('HGET', cursors, fingerprint); local chosen = tied[1]
if cursor then
  local separator = string.find(cursor, ':'); local cursor_order = separator and tonumber(string.sub(cursor, 1, separator - 1))
  if not cursor_order then return {'schema'} end
  for _, c in ipairs(tied) do if c[3] > cursor_order then chosen=c; break end end
end
local expires = math.min(now + ttl, deadline)
local rkey = prefix .. 'reservation:' .. token_digest
redis.call('HSET', lifecycle, 'state','reserved','client_digest',client,'request_digest',request,
 'node_id',chosen[4],'node_digest',chosen[5],'model',model,'tier',tier,'deadline',deadline,
 'reservation_expiry',expires,'token_digest',token_digest,'cancellation_digest',cancellation)
redis.call('HSET', rkey, 'client',client,'request',request,'identity',identity,'node_digest',chosen[5])
redis.call('ZADD', requests, deadline, identity); redis.call('ZADD', reservations, expires, token_digest)
redis.call('ZADD', expiry_index, expires, token_digest); redis.call('ZADD', prefix .. 'client:requests:' .. client, deadline, identity)
redis.call('ZADD', prefix .. 'client:reservations:' .. client, expires, identity); redis.call('ZADD', prefix .. 'node:reservations:' .. chosen[5], expires, identity)
if redis.call('HLEN', cursors) - (redis.call('HEXISTS', cursors, '_activity') == 1 and 1 or 0) >= max_cursors and not redis.call('HEXISTS', cursors, fingerprint) then
  local all=redis.call('HGETALL', cursors); local victim=nil; local activity=math.huge
  for i=1,#all,2 do if all[i] ~= '_activity' then local sep=string.find(all[i+1],':'); local a=sep and tonumber(string.sub(all[i+1],sep+1)); if a and a<activity then victim=all[i];activity=a end end end
  if victim then redis.call('HDEL',cursors,victim) end
end
local sequence=redis.call('HINCRBY',cursors,'_activity',1); redis.call('HSET',cursors,fingerprint,chosen[3]..':'..sequence)
return {'created',chosen[4],tostring(expires),'reserved'}
"""
SELECT_RESERVE_SCRIPT = ReviewedScript(
    "select_reserve_v1",
    SELECT_RESERVE_SOURCE,
    "36a0f73801d1de4d4de6112ae2ce578e800d2bb7e8b2475df959e36f94727f39",  # pragma: allowlist secret
    True,
)

ENQUEUE_SOURCE = """\
local leases, requests, reservations, expiry_index, queue, queue_sequence, queues = KEYS[1],KEYS[2],KEYS[3],KEYS[4],KEYS[5],KEYS[6],KEYS[7]
local prefix,client,request,identity,token,node_id,node_digest,model,tier,deadline,envelope,cancel =
 ARGV[1],ARGV[2],ARGV[3],ARGV[4],ARGV[5],ARGV[6],ARGV[7],ARGV[8],ARGV[9],tonumber(ARGV[10]),ARGV[11],ARGV[12]
local max_ttl,max_queue,max_client,max_node = tonumber(ARGV[13]),tonumber(ARGV[14]),tonumber(ARGV[15]),tonumber(ARGV[16])
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local lifecycle=prefix..'request:'..client..':'..request
local v=redis.call('HMGET',lifecycle,'state','node_id','node_digest','model','tier','deadline','token_digest','cancellation_digest','envelope','sequence')
if v[1]=='queued' or v[1]=='claimed' then
 if v[2]~=node_id or v[4]~=model or v[5]~=tier or tonumber(v[6])~=deadline or v[9]~=envelope or v[8]~=cancel then return {'conflict'} end
 if v[7]~=token then return {'invalid'} end
 return {'existing',v[1],v[10]}
end
if v[1]~='reserved' or v[2]~=node_id or v[3]~=node_digest or v[4]~=model or v[5]~=tier or tonumber(v[6])~=deadline or v[7]~=token then return {'invalid'} end
if v[8]~='' and v[8]~=cancel then return {'conflict'} end
local lease=redis.call('ZSCORE',leases,node_digest); local rexp=redis.call('ZSCORE',expiry_index,token)
if deadline<=now or not lease or tonumber(lease)<=now or not rexp or tonumber(rexp)<=now then return {'invalid'} end
if deadline>now+max_ttl then return {'deadline'} end
if redis.call('ZCARD',queue)>=max_node or redis.call('ZCARD',queues)>=max_queue or redis.call('ZCARD',prefix..'client:queue:'..client)>=max_client then return {'capacity'} end
local seq=redis.call('INCR',queue_sequence)
redis.call('HSET',lifecycle,'state','queued','envelope',envelope,'cancellation_digest',cancel,'enqueued_at',now,'sequence',seq)
redis.call('ZADD',queue,seq,client..':'..request); redis.call('ZADD',prefix..'client:queue:'..client,seq,identity)
redis.call('ZADD',queues,seq,identity)
redis.call('DEL',prefix..'reservation:'..token); redis.call('ZREM',reservations,token); redis.call('ZREM',expiry_index,token)
redis.call('ZREM',prefix..'client:reservations:'..client,identity); redis.call('ZREM',prefix..'node:reservations:'..node_digest,identity)
return {'created','queued',tostring(seq)}
"""
ENQUEUE_SCRIPT = ReviewedScript(
    "enqueue_request_v1",
    ENQUEUE_SOURCE,
    "fdd05d42207b0d39a89f835de4a7afec90079575e563c1880440c08eed71e1ba",  # pragma: allowlist secret
    True,
)
SCRIPT_REGISTRY: Mapping[str, ReviewedScript] = MappingProxyType(
    {
        SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT,
        REGISTRATION_TRANSITION_SCRIPT.name: REGISTRATION_TRANSITION_SCRIPT,
        SCHEDULER_STATE_SCRIPT.name: SCHEDULER_STATE_SCRIPT,
        SELECT_RESERVE_SCRIPT.name: SELECT_RESERVE_SCRIPT,
        ENQUEUE_SCRIPT.name: ENQUEUE_SCRIPT,
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
        self, script_name: str, keys: tuple[str, ...] = (), args: tuple[bytes, ...] = ()
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
        _validate_script_result(result)
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
    """Internal Valkey implementation of only registration and lease transitions.

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
            cfg.key("registration:sequence"),
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
            (
                self._foundation.config.key("nodes:lease"),
                self._foundation.config.key("node", digest),
            ),
            (
                control_credential_digest.encode(),
                digest.encode(),
                b"1" if state.healthy else b"0",
                b"1" if state.draining else b"0",
                str(state.claimed_work).encode(),
            ),
        )
        code = self._status(result)
        if code == "credential_mismatch":
            raise RelayStateCredentialMismatch("control credential digest mismatch")
        if code == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return code == "ok"

    @staticmethod
    def _status(result: object) -> str:
        if (
            not isinstance(result, (list, tuple))
            or not result
            or not isinstance(result[0], bytes)
        ):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            return result[0].decode("ascii")
        except UnicodeDecodeError:
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None

    def _identity(self, client_public_key: str, request_id: str) -> tuple[str, str]:
        def digest(value: str, domain: bytes) -> str:
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode()) > self.config.max_identity_bytes
            ):
                raise RelayStateStoreError("request identity is invalid")
            return hashlib.sha256(domain + value.encode()).hexdigest()

        return digest(client_public_key, b"client\0"), digest(request_id, b"request\0")

    def _model(self, value: str) -> str:
        normalized = value.strip().lower() if isinstance(value, str) else ""
        if not normalized or len(normalized.encode()) > self.config.max_model_id_bytes:
            raise RelayStateStoreError("requested model is invalid")
        return normalized

    @staticmethod
    def _tier(value: str) -> str:
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
        model, tier, deadline = (
            self._model(requested_model_id),
            self._tier(requested_context_tier),
            self._deadline(request_deadline_epoch),
        )
        cancellation = ""
        if cancellation_token is not None:
            if (
                not isinstance(cancellation_token, str)
                or not cancellation_token
                or len(cancellation_token.encode())
                > self.config.max_cancellation_token_bytes
            ):
                raise RelayStateStoreError("cancellation proof is invalid")
            cancellation = hashlib.sha256(
                b"cancel\0" + cancellation_token.encode()
            ).hexdigest()
        fingerprint = hashlib.sha256(f"{model}\0{tier}".encode()).hexdigest()
        identity = hashlib.sha256(f"{client}\0{request}".encode()).hexdigest()
        raw_token = secrets.token_hex(32)
        token_digest = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        cfg = self._foundation.config
        keys = (
            cfg.key("nodes:lease"),
            cfg.key("reservations:expiry"),
            cfg.key("requests"),
            cfg.key("reservations"),
            cfg.key("cursors"),
        )
        args = tuple(
            str(v).encode()
            for v in (
                cfg.key_prefix,
                client,
                request,
                identity,
                model,
                tier,
                CONTEXT_TIER_TOKEN_BOUNDS[tier],
                repr(deadline),
                fingerprint,
                token_digest,
                cancellation,
                repr(float(self.config.reservation_ttl_seconds)),
                repr(float(self.config.max_request_ttl_seconds)),
                self.config.max_compute_nodes,
                self.config.node_transition_batch_size,
                self.config.max_reservations,
                self.config.max_reservations_per_client,
                self.config.max_reservations_per_node,
                self.config.max_queue_depth_per_node,
                self.config.max_request_lifecycles,
                self.config.max_scheduler_fingerprints,
            )
        )
        result = self._foundation.execute(SELECT_RESERVE_SCRIPT.name, keys, args)
        code = self._status(result)
        if code == "capacity":
            raise RelayStateNoCapacity("no scheduler capacity")
        if code == "invalid":
            raise RelayStateInvalidReservation("request deadline expired")
        if code == "deadline":
            raise RelayStateStoreError("request deadline exceeds its configured bound")
        if code == "conflict":
            raise RelayStateConflict("request identity conflict")
        if code == "schema":
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if code not in {"created", "existing"} or len(result) != 4:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            node = result[1].decode()
            expiry = float(result[2]) if result[2] else None
            state = result[3].decode()
        except (UnicodeDecodeError, ValueError, TypeError):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None
        return SelectionResult(
            node,
            model,
            tier,
            deadline,
            expiry,
            raw_token if code == "created" else None,
            code == "created",
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
        model, tier, deadline = (
            self._model(requested_model_id),
            self._tier(requested_context_tier),
            self._deadline(request_deadline_epoch),
        )
        if not isinstance(envelope, EncryptedRequestEnvelope):
            raise RelayStateStoreError("envelope must be EncryptedRequestEnvelope")
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
        ).encode()
        if len(serialized) > self.config.max_envelope_bytes:
            raise RelayStateStoreError(
                "encrypted envelope exceeds its configured byte bound"
            )
        if not isinstance(reservation_token, str) or not re.fullmatch(
            r"[0-9a-f]{64}", reservation_token
        ):
            raise RelayStateInvalidReservation("reservation invalid")
        if (
            not isinstance(cancellation_token, str)
            or not cancellation_token
            or len(cancellation_token.encode())
            > self.config.max_cancellation_token_bytes
        ):
            raise RelayStateStoreError("cancellation proof is invalid")
        token = hashlib.sha256(reservation_token.encode("ascii")).hexdigest()
        cancel = hashlib.sha256(b"cancel\0" + cancellation_token.encode()).hexdigest()
        identity = hashlib.sha256(f"{client}\0{request}".encode()).hexdigest()
        selected_node_digest = self._node_digest(selected_node_id)
        cfg = self._foundation.config
        keys = (
            cfg.key("nodes:lease"),
            cfg.key("requests"),
            cfg.key("reservations"),
            cfg.key("reservations:expiry"),
            cfg.key("queue", selected_node_digest),
            cfg.key("queue:sequence"),
            cfg.key("queues"),
        )
        args = tuple(
            str(v).encode()
            for v in (
                cfg.key_prefix,
                client,
                request,
                identity,
                token,
                selected_node_id,
                selected_node_digest,
                model,
                tier,
                repr(deadline),
                serialized.decode(),
                cancel,
                repr(float(self.config.max_request_ttl_seconds)),
                self.config.max_queued_requests,
                self.config.max_queued_requests_per_client,
                self.config.max_queue_depth_per_node,
            )
        )
        result = self._foundation.execute(ENQUEUE_SCRIPT.name, keys, args)
        code = self._status(result)
        if code == "invalid":
            raise RelayStateInvalidReservation("reservation invalid")
        if code == "conflict":
            raise RelayStateConflict("request identity conflict")
        if code == "capacity":
            raise RelayStateNoCapacity("no scheduler capacity")
        if code == "deadline":
            raise RelayStateStoreError("request deadline exceeds its configured bound")
        if code not in {"created", "existing"} or len(result) != 3:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        try:
            state = result[1].decode()
            sequence = int(result[2])
        except (UnicodeDecodeError, ValueError, TypeError):
            raise ValkeySchemaIncompatibleError("state schema incompatible") from None
        return EnqueueResult(
            state, selected_node_id, deadline, sequence, code == "created"
        )

    def list_reservations(self) -> tuple[ReservationRecord, ...]:
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        seconds, micros = self._foundation.server_time()
        now = seconds + micros / 1_000_000
        cfg = self._foundation.config
        tokens = self._foundation._call(
            self._foundation._client.zrangebyscore,
            cfg.key("reservations"),
            f"({now}",
            "+inf",
            start=0,
            num=self.config.max_reservations,
        )
        records = []
        for token in tokens:
            if not isinstance(token, bytes) or not re.fullmatch(
                rb"[0-9a-f]{64}", token
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            reservation = self._foundation._call(
                self._foundation._client.hmget,
                cfg.key("reservation", token.decode()),
                ("client", "request"),
            )
            if (
                not isinstance(reservation, list)
                or len(reservation) != 2
                or any(not isinstance(value, bytes) for value in reservation)
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            client, request = (value.decode("ascii") for value in reservation)
            raw = self._foundation._call(
                self._foundation._client.hmget,
                cfg.key("request", client, request),
                ("node_id", "model", "tier", "deadline", "reservation_expiry"),
            )
            try:
                node, model, tier = (raw[index].decode() for index in range(3))
                deadline, expires = float(raw[3]), float(raw[4])
            except (
                AttributeError,
                IndexError,
                TypeError,
                ValueError,
                UnicodeDecodeError,
            ):
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
            records.append(
                ReservationRecord(
                    client,
                    request,
                    hashlib.sha256(f"{model}\0{tier}".encode()).hexdigest(),
                    node,
                    model,
                    tier,
                    deadline,
                    expires,
                    token.decode(),
                )
            )
        return tuple(records)

    def queued_requests(self, node_id: str) -> tuple[QueuedRequest, ...]:
        self._validate_node_id(node_id)
        manifest = self._foundation.read_manifest()
        self._foundation.check_read_compatible(manifest)
        cfg = self._foundation.config
        node_digest = self._node_digest(node_id)
        identities = self._foundation._call(
            self._foundation._client.zrange,
            cfg.key("queue", node_digest),
            0,
            self.config.max_queue_depth_per_node - 1,
        )
        records = []
        for member in identities:
            if not isinstance(member, bytes) or not re.fullmatch(
                rb"[0-9a-f]{64}:[0-9a-f]{64}", member
            ):
                raise ValkeySchemaIncompatibleError("state schema incompatible")
            client, request = member.decode("ascii").split(":")
            raw = self._foundation._call(
                self._foundation._client.hmget,
                cfg.key("request", client, request),
                (
                    "node_id",
                    "model",
                    "tier",
                    "deadline",
                    "envelope",
                    "enqueued_at",
                    "sequence",
                ),
            )
            try:
                envelope_value = json.loads(raw[4])
                envelope = EncryptedRequestEnvelope(**envelope_value)
                records.append(
                    QueuedRequest(
                        client,
                        request,
                        client,
                        request,
                        raw[0].decode(),
                        raw[1].decode(),
                        raw[2].decode(),
                        float(raw[3]),
                        envelope,
                        float(raw[5]),
                        int(raw[6]),
                    )
                )
            except (
                IndexError,
                TypeError,
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                RelayStateStoreError,
            ):
                raise ValkeySchemaIncompatibleError(
                    "state schema incompatible"
                ) from None
        return tuple(records)
