"""Internal Valkey primitives for a future RelayStateStore implementation.

This module deliberately exposes no relay lifecycle operation and is not imported by
``relay.py``.  Values in errors and representations are intentionally non-diagnostic:
connection coordinates and datastore contents must never escape this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError, NoScriptError, RedisError, TimeoutError
from redis.retry import Retry
from redis.sentinel import Sentinel

_NAMESPACE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HOST = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_MAX_TIMEOUT = 30.0
_MAX_RETRIES = 3
_MAX_RESULT_ITEMS = 32
_MAX_RESULT_BYTES = 16_384


class ValkeyError(RuntimeError):
    """A bounded, redacted Valkey foundation failure."""

    code = "state_backend_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class ValkeyConfigurationError(ValkeyError):
    code = "state_configuration_invalid"


class ValkeyUnavailableError(ValkeyError):
    code = "state_backend_unavailable"


class ValkeyReadOnlyError(ValkeyError):
    code = "state_backend_read_only"


class SchemaCompatibilityError(ValkeyError):
    code = "state_schema_incompatible"


class ScriptExecutionError(ValkeyError):
    code = "state_script_error"


@dataclass(frozen=True, repr=False)
class DirectPrimary:
    host: str
    port: int

    def __post_init__(self) -> None:
        _validate_address(self.host, self.port)

    def __repr__(self) -> str:
        return "DirectPrimary(<redacted>)"


@dataclass(frozen=True, repr=False)
class SentinelDiscovery:
    sentinels: tuple[tuple[str, int], ...]
    master_name: str
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if not self.sentinels or not _NAMESPACE.fullmatch(self.master_name):
            raise ValkeyConfigurationError()
        for host, port in self.sentinels:
            _validate_address(host, port)

    def __repr__(self) -> str:
        return "SentinelDiscovery(<redacted>)"


@dataclass(frozen=True, repr=False)
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
    connect_timeout: float
    socket_timeout: float
    command_timeout: float
    retry_timeout: float
    retry_attempts: int
    direct: DirectPrimary | None = None
    sentinel: SentinelDiscovery | None = None
    tls: bool = False
    username: str | None = None
    password: str | None = None
    ssl_ca_certs: str | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    def __post_init__(self) -> None:
        if not _NAMESPACE.fullmatch(self.environment) or not _NAMESPACE.fullmatch(self.cluster):
            raise ValkeyConfigurationError()
        revisions = (
            self.schema_major, self.reader_revision, self.writer_revision,
            self.supported_schema_read_min, self.supported_schema_read_max,
            self.supported_writer_min, self.supported_writer_max,
        )
        if any(type(value) is not int or value < 1 for value in revisions):
            raise ValkeyConfigurationError()
        if self.supported_schema_read_min > self.supported_schema_read_max or self.supported_writer_min > self.supported_writer_max:
            raise ValkeyConfigurationError()
        if (self.direct is None) == (self.sentinel is None):
            raise ValkeyConfigurationError()
        for value in (self.connect_timeout, self.socket_timeout, self.command_timeout, self.retry_timeout):
            if not isinstance(value, (int, float)) or not 0 < value <= _MAX_TIMEOUT:
                raise ValkeyConfigurationError()
        if type(self.retry_attempts) is not int or not 0 <= self.retry_attempts <= _MAX_RETRIES:
            raise ValkeyConfigurationError()
        if self.retry_timeout * max(1, self.retry_attempts) > _MAX_TIMEOUT:
            raise ValkeyConfigurationError()
        if not self.tls and any((self.ssl_ca_certs, self.ssl_certfile, self.ssl_keyfile)):
            raise ValkeyConfigurationError()

    def __repr__(self) -> str:
        return "ValkeyConfig(<redacted>)"


def _validate_address(host: str, port: int) -> None:
    if not isinstance(host, str) or not _HOST.fullmatch(host) or type(port) is not int or not 1 <= port <= 65535:
        raise ValkeyConfigurationError()


@dataclass(frozen=True)
class Keyspace:
    environment: str
    cluster: str
    schema_major: int

    def __post_init__(self) -> None:
        if not _NAMESPACE.fullmatch(self.environment) or not _NAMESPACE.fullmatch(self.cluster) or type(self.schema_major) is not int or self.schema_major < 1:
            raise ValkeyConfigurationError()

    @property
    def prefix(self) -> str:
        return f"tokenplace:{{{self.environment}:{self.cluster}}}:relay:v{self.schema_major}:"

    def key(self, suffix: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_:]{0,127}", suffix):
            raise ValkeyConfigurationError()
        return self.prefix + suffix


@dataclass(frozen=True)
class SchemaManifest:
    schema_major: int
    active_schema_revision: int
    active_writer_revision: int
    supported_reader_min: int
    supported_reader_max: int
    supported_writer_min: int
    supported_writer_max: int
    script_digests: Mapping[str, str]
    migration_epoch: int

    def __post_init__(self) -> None:
        values = (self.schema_major, self.active_schema_revision, self.active_writer_revision,
                  self.supported_reader_min, self.supported_reader_max,
                  self.supported_writer_min, self.supported_writer_max, self.migration_epoch)
        if any(type(v) is not int or v < 0 for v in values) or self.schema_major < 1:
            raise SchemaCompatibilityError()
        if self.supported_reader_min > self.supported_reader_max or self.supported_writer_min > self.supported_writer_max:
            raise SchemaCompatibilityError()
        clean = dict(self.script_digests)
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", k) or not re.fullmatch(r"[0-9a-f]{40}", v) for k, v in clean.items()):
            raise SchemaCompatibilityError()
        object.__setattr__(self, "script_digests", MappingProxyType(clean))

    def encode(self) -> bytes:
        body = {name: getattr(self, name) for name in (
            "schema_major", "active_schema_revision", "active_writer_revision",
            "supported_reader_min", "supported_reader_max", "supported_writer_min",
            "supported_writer_max", "migration_epoch")}
        body["script_digests"] = dict(self.script_digests)
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def decode(cls, value: bytes) -> "SchemaManifest":
        if not isinstance(value, bytes) or len(value) > _MAX_RESULT_BYTES:
            raise SchemaCompatibilityError()
        try:
            body = json.loads(value)
            if not isinstance(body, dict) or set(body) != {
                "schema_major", "active_schema_revision", "active_writer_revision",
                "supported_reader_min", "supported_reader_max", "supported_writer_min",
                "supported_writer_max", "script_digests", "migration_epoch"}:
                raise ValueError
            return cls(**body)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SchemaCompatibilityError() from None


@dataclass(frozen=True)
class VersionedScript:
    name: str
    source: str
    sha1: str

    @classmethod
    def trusted(cls, name: str, source: str) -> "VersionedScript":
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or not source or len(source.encode()) > _MAX_RESULT_BYTES:
            raise ScriptExecutionError()
        return cls(name, source, hashlib.sha1(source.encode(), usedforsecurity=False).hexdigest())


# Foundation-only script: proves reviewed registry/load behavior without protocol mutation.
SERVER_TIME_SCRIPT = VersionedScript.trusted("server_time_v1", "local t=redis.call('TIME'); return {t[1],t[2]}")
SCRIPT_REGISTRY: Mapping[str, VersionedScript] = MappingProxyType({SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT})


class ValkeyFoundation:
    def __init__(self, config: ValkeyConfig, *, client: redis.Redis | None = None) -> None:
        self._config = config
        self.keys = Keyspace(config.environment, config.cluster, config.schema_major)
        self._client = client or self._connect(config)

    def __repr__(self) -> str:
        return "ValkeyFoundation(<redacted>)"

    @staticmethod
    def _connect(config: ValkeyConfig) -> redis.Redis:
        options: dict[str, Any] = dict(
            socket_connect_timeout=config.connect_timeout,
            socket_timeout=min(config.socket_timeout, config.command_timeout),
            retry=Retry(NoBackoff(), config.retry_attempts), retry_on_timeout=True,
            health_check_interval=0, decode_responses=False, username=config.username,
            password=config.password,
            max_connections=8,
        )
        if config.tls:
            options.update(
                ssl_ca_certs=config.ssl_ca_certs,
                ssl_certfile=config.ssl_certfile,
                ssl_keyfile=config.ssl_keyfile,
            )
        try:
            if config.direct:
                pool_class = redis.SSLConnection if config.tls else redis.Connection
                pool = redis.ConnectionPool(
                    host=config.direct.host, port=config.direct.port,
                    connection_class=pool_class, **options,
                )
                return redis.Redis(connection_pool=pool)
            discovery = config.sentinel
            if discovery is None:  # Already rejected by ValkeyConfig; retain fail-closed typing.
                raise ValkeyConfigurationError()
            sentinel = Sentinel(discovery.sentinels, sentinel_kwargs={
                "socket_connect_timeout": config.connect_timeout,
                "socket_timeout": config.socket_timeout,
                "username": discovery.username, "password": discovery.password,
            }, **options)
            return sentinel.master_for(discovery.master_name)
        except (RedisError, ValueError, TypeError):
            raise ValkeyUnavailableError() from None

    def _manifest_key(self) -> str:
        return self.keys.key("schema")

    def initialize_manifest(self, expected: SchemaManifest) -> SchemaManifest:
        if expected.schema_major != self._config.schema_major:
            raise SchemaCompatibilityError()
        try:
            self._client.set(self._manifest_key(), expected.encode(), nx=True)
            raw = self._client.get(self._manifest_key())
        except (ConnectionError, TimeoutError, RedisError):
            raise ValkeyUnavailableError() from None
        actual = SchemaManifest.decode(raw)
        if actual != expected:
            raise SchemaCompatibilityError()
        self.require_write_compatible(actual)
        return actual

    def read_manifest(self) -> SchemaManifest:
        try:
            raw = self._client.get(self._manifest_key())
        except (ConnectionError, TimeoutError, RedisError):
            raise ValkeyUnavailableError() from None
        if raw is None:
            raise SchemaCompatibilityError()
        return SchemaManifest.decode(raw)

    def require_read_compatible(self, manifest: SchemaManifest) -> None:
        c = self._config
        if (manifest.schema_major != c.schema_major or
                not manifest.supported_reader_min <= c.reader_revision <= manifest.supported_reader_max or
                not c.supported_schema_read_min <= manifest.active_schema_revision <= c.supported_schema_read_max or
                dict(manifest.script_digests) != {n: s.sha1 for n, s in SCRIPT_REGISTRY.items()}):
            raise SchemaCompatibilityError()

    def require_write_compatible(self, manifest: SchemaManifest) -> None:
        self.require_read_compatible(manifest)
        c = self._config
        if (not manifest.supported_writer_min <= c.writer_revision <= manifest.supported_writer_max or
                not c.supported_writer_min <= manifest.active_writer_revision <= c.supported_writer_max):
            raise SchemaCompatibilityError()

    def server_time(self) -> tuple[int, int]:
        manifest = self.read_manifest()
        self.require_read_compatible(manifest)
        try:
            seconds, micros = self._client.time()
            return int(seconds), int(micros)
        except (ConnectionError, TimeoutError, RedisError, TypeError, ValueError):
            raise ValkeyUnavailableError() from None

    def execute(self, script_name: str, *, keys: tuple[str, ...] = (), args: tuple[bytes, ...] = ()) -> tuple[Any, ...]:
        script = SCRIPT_REGISTRY.get(script_name)
        if script is None:
            raise ScriptExecutionError()
        manifest = self.read_manifest()
        # Mutating scripts must always pass both gates immediately before dispatch.
        self.require_write_compatible(manifest)
        try:
            try:
                result = self._client.evalsha(script.sha1, len(keys), *keys, *args)
            except NoScriptError:
                loaded = self._client.script_load(script.source)
                if loaded != script.sha1:
                    raise ScriptExecutionError()
                result = self._client.evalsha(script.sha1, len(keys), *keys, *args)
            return self._decode_result(result)
        except ScriptExecutionError:
            raise
        except (ConnectionError, TimeoutError, RedisError):
            raise ValkeyUnavailableError() from None

    @staticmethod
    def _decode_result(result: Any) -> tuple[Any, ...]:
        if not isinstance(result, (list, tuple)) or len(result) > _MAX_RESULT_ITEMS:
            raise ScriptExecutionError()
        if sum(len(x) if isinstance(x, bytes) else 16 for x in result) > _MAX_RESULT_BYTES:
            raise ScriptExecutionError()
        if any(not isinstance(x, (bytes, int)) for x in result):
            raise ScriptExecutionError()
        return tuple(result)

    def readiness(self) -> None:
        try:
            if not self._client.ping():
                raise ValkeyUnavailableError()
            role = self._client.role()
        except (ConnectionError, TimeoutError, RedisError):
            raise ValkeyUnavailableError() from None
        if not isinstance(role, (list, tuple)) or not role or role[0] not in (b"master", "master"):
            raise ValkeyReadOnlyError()
        manifest = self.read_manifest()
        self.require_write_compatible(manifest)

    def close(self) -> None:
        self._client.close()
        self._client.connection_pool.disconnect()
