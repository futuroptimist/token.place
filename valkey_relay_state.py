"""Internal Valkey connection, schema, key, and reviewed-script primitives.

This foundation intentionally implements no relay lifecycle operation and is not
imported by :mod:`relay`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import redis
from redis.exceptions import NoScriptError, RedisError, ResponseError
from redis.sentinel import Sentinel

from relay_state_store import (
    ComputeNodeCapabilities,
    ComputeNodeRegistration,
    RelayStateCapacityExceeded,
    RelayStateCredentialMismatch,
    RelayStateStoreConfig,
    RelayStateStoreError,
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
local leases, node = KEYS[1], KEYS[2]
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
      'backend_class', ARGV[15], 'api_version', ARGV[16])
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
    "6b2a4873c89176ec44f08f71f778b263bdfb55c9b3bd383883058292a548ed26",  # pragma: allowlist secret
    True,
)
SCRIPT_REGISTRY: Mapping[str, ReviewedScript] = MappingProxyType(
    {
        SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT,
        REGISTRATION_TRANSITION_SCRIPT.name: REGISTRATION_TRANSITION_SCRIPT,
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
