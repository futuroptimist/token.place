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

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 5
_MAX_RESULT_BYTES = 65_536
_MAX_RESULT_ITEMS = 1_024
_MAX_RESULT_DEPTH = 8
_MAX_MANIFEST_SCRIPTS = 64
_MAX_CONNECTIONS = 32
_KEY_FAMILIES = frozenset({"schema"})
_DIGEST_KEY_FAMILIES = frozenset({"request", "response", "lease", "worker"})


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
        if not self.sentinels or len(self.sentinels) > 32:
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

    def key(self, family: str, digest: str | None = None) -> str:
        if family in _KEY_FAMILIES and digest is None:
            return self.key_prefix + family
        if (
            family not in _DIGEST_KEY_FAMILIES
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise ValkeyConfigurationError("invalid key suffix")
        return f"{self.key_prefix}{family}:{digest}"

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
            or not 0 < len(self.script_digests) <= _MAX_MANIFEST_SCRIPTS
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
    hashlib.sha256(SERVER_TIME_SOURCE.encode()).hexdigest(),
    False,
)
SCRIPT_REGISTRY: Mapping[str, ReviewedScript] = MappingProxyType(
    {SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT}
)


class ValkeyFoundation:
    """Owns an explicit pool and exposes only foundation-level operations."""

    def __init__(self, config: ValkeyConfig, expected_manifest: SchemaManifest):
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
            or dict(manifest.script_digests) != dict(e.script_digests)
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
            result = self._call(
                self._client.evalsha, script.eval_sha1, len(keys), *keys, *args
            )
        except NoScriptError:
            loaded = self._call(self._client.script_load, script.source)
            loaded = loaded.decode() if isinstance(loaded, bytes) else loaded
            if loaded != script.eval_sha1:
                raise ValkeyScriptError("reviewed script digest mismatch")
            manifest = self.read_manifest()
            self.check_read_compatible(manifest)
            if script.mutates:
                self.check_write_compatible(manifest)
            result = self._call(
                self._client.evalsha, script.eval_sha1, len(keys), *keys, *args
            )
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
