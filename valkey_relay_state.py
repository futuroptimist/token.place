"""Internal Valkey connection, schema, key, and reviewed-script primitives.

This foundation deliberately implements no relay lifecycle operation and is not
wired into :mod:`relay`.  Public errors and representations are intentionally
bounded and contain no connection details, credentials, keys, or server replies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

_COORDINATE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 5
_MAX_MANIFEST_BYTES = 16_384
_MAX_SCRIPT_RESULT_BYTES = 65_536


class ValkeyFoundationError(RuntimeError):
    """A bounded, redacted Valkey foundation failure."""


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


@dataclass(frozen=True, slots=True)
class DirectPrimaryDiscovery:
    host: str = field(repr=False)
    port: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host or len(self.host) > 253:
            raise ValkeyConfigurationError("invalid direct-primary discovery")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValkeyConfigurationError("invalid direct-primary discovery")

    def __repr__(self) -> str:
        return "DirectPrimaryDiscovery(<redacted>)"


@dataclass(frozen=True, slots=True)
class SentinelDiscovery:
    sentinels: tuple[tuple[str, int], ...] = field(repr=False)
    master_name: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.sentinels or len(self.sentinels) > 32 or not self.master_name or len(self.master_name) > 128:
            raise ValkeyConfigurationError("invalid Sentinel discovery")
        for host, port in self.sentinels:
            if not host or len(host) > 253 or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValkeyConfigurationError("invalid Sentinel discovery")

    def __repr__(self) -> str:
        return "SentinelDiscovery(<redacted>)"


@dataclass(frozen=True, slots=True)
class ValkeySecurity:
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    ca_cert_path: str | None = field(default=None, repr=False)
    client_cert_path: str | None = field(default=None, repr=False)
    client_key_path: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        values = (self.username, self.password, self.ca_cert_path, self.client_cert_path, self.client_key_path)
        if any(value is not None and (not isinstance(value, str) or not value) for value in values):
            raise ValkeyConfigurationError("invalid authentication or TLS configuration")
        if bool(self.client_cert_path) != bool(self.client_key_path):
            raise ValkeyConfigurationError("client certificate and key must be configured together")
        if not self.tls and any((self.ca_cert_path, self.client_cert_path, self.client_key_path)):
            raise ValkeyConfigurationError("certificate inputs require TLS")

    def __repr__(self) -> str:
        return f"ValkeySecurity(tls={self.tls}, credentials=<redacted>)"


@dataclass(frozen=True, slots=True)
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
    direct: DirectPrimaryDiscovery | None = field(default=None, repr=False)
    sentinel: SentinelDiscovery | None = field(default=None, repr=False)
    security: ValkeySecurity = field(default_factory=ValkeySecurity, repr=False)
    connect_timeout_seconds: float = 1.0
    socket_timeout_seconds: float = 1.0
    command_timeout_seconds: float = 2.0
    retry_timeout_seconds: float = 1.0
    max_retries: int = 1

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and _COORDINATE.fullmatch(v) for v in (self.environment, self.cluster)):
            raise ValkeyConfigurationError("invalid namespace coordinates")
        if (self.direct is None) == (self.sentinel is None):
            raise ValkeyConfigurationError("exactly one discovery mode is required")
        revisions = (self.schema_major, self.reader_revision, self.writer_revision, self.supported_schema_read_min,
                     self.supported_schema_read_max, self.supported_writer_min, self.supported_writer_max)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in revisions):
            raise ValkeyConfigurationError("schema revisions must be positive integers")
        if self.supported_schema_read_min > self.supported_schema_read_max or self.supported_writer_min > self.supported_writer_max:
            raise ValkeyConfigurationError("invalid compatibility range")
        for value in (self.connect_timeout_seconds, self.socket_timeout_seconds, self.command_timeout_seconds, self.retry_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= _MAX_TIMEOUT_SECONDS:
                raise ValkeyConfigurationError("timeouts must be finite, positive, and bounded")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= _MAX_RETRIES:
            raise ValkeyConfigurationError("retry count must be bounded")

    @property
    def key_prefix(self) -> str:
        return f"tokenplace:{{{self.environment}:{self.cluster}}}:relay:v{self.schema_major}:"

    def key(self, suffix: str) -> str:
        if not isinstance(suffix, str) or not suffix or len(suffix) > 512 or any(c in suffix for c in "{}\r\n"):
            raise ValkeyConfigurationError("invalid key suffix")
        return self.key_prefix + suffix

    def __repr__(self) -> str:
        mode = "direct" if self.direct else "sentinel"
        return f"ValkeyConfig(environment={self.environment!r}, cluster={self.cluster!r}, schema_major={self.schema_major}, discovery={mode}, connection=<redacted>)"


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
        ints = (self.schema_major, self.active_schema_revision, self.active_writer_revision,
                self.reader_min, self.reader_max, self.writer_min, self.writer_max, self.migration_epoch)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in ints):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        if self.schema_major < 1 or self.reader_min > self.reader_max or self.writer_min > self.writer_max:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        digests = dict(self.script_digests)
        if len(digests) > 64 or any(not isinstance(n, str) or not n or len(n) > 128 or not _DIGEST.fullmatch(d) for n, d in digests.items()):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        object.__setattr__(self, "script_digests", MappingProxyType(digests))

    def encode(self) -> bytes:
        value = json.dumps({"schema_major": self.schema_major, "active_schema_revision": self.active_schema_revision,
            "active_writer_revision": self.active_writer_revision, "reader_min": self.reader_min, "reader_max": self.reader_max,
            "writer_min": self.writer_min, "writer_max": self.writer_max, "script_digests": dict(self.script_digests),
            "migration_epoch": self.migration_epoch}, sort_keys=True, separators=(",", ":")).encode()
        if len(value) > _MAX_MANIFEST_BYTES:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return value

    @classmethod
    def decode(cls, value: bytes) -> "SchemaManifest":
        try:
            if not isinstance(value, bytes) or len(value) > _MAX_MANIFEST_BYTES:
                raise ValueError
            data = json.loads(value)
            if set(data) != {"schema_major", "active_schema_revision", "active_writer_revision", "reader_min", "reader_max", "writer_min", "writer_max", "script_digests", "migration_epoch"}:
                raise ValueError
            return cls(**data)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValkeySchemaIncompatibleError("state schema incompatible") from exc


@dataclass(frozen=True, slots=True)
class ReviewedScript:
    name: str
    source: str = field(repr=False)
    digest: str

    @classmethod
    def from_source(cls, name: str, source: str) -> "ReviewedScript":
        if not name or not source or len(source.encode()) > 65_536:
            raise ValkeyScriptError("invalid reviewed script")
        return cls(name, source, hashlib.sha256(source.encode()).hexdigest())


SERVER_TIME_SCRIPT = ReviewedScript.from_source("server_time_v1", "return redis.call('TIME')\n")
SCRIPT_REGISTRY: Mapping[str, ReviewedScript] = MappingProxyType({SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT})


class ValkeyFoundation:
    """Explicit pooled client exposing only schema and reviewed-script primitives."""

    def __init__(self, config: ValkeyConfig, expected_manifest: SchemaManifest):
        if expected_manifest.schema_major != config.schema_major:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        self.config = config
        self.expected_manifest = expected_manifest
        self._client = self._connect(config)

    @staticmethod
    def _connect(config: ValkeyConfig) -> redis.Redis:
        common = dict(username=config.security.username, password=config.security.password,
            socket_connect_timeout=config.connect_timeout_seconds, socket_timeout=min(config.socket_timeout_seconds, config.command_timeout_seconds),
            retry=Retry(NoBackoff(), config.max_retries), retry_on_timeout=True, decode_responses=False,
            max_connections=16)
        if config.security.tls:
            common.update(connection_class=redis.SSLConnection, ssl_ca_certs=config.security.ca_cert_path,
                ssl_certfile=config.security.client_cert_path, ssl_keyfile=config.security.client_key_path)
        if config.direct:
            pool = redis.ConnectionPool(host=config.direct.host, port=config.direct.port, **common)
            return redis.Redis(connection_pool=pool)
        assert config.sentinel
        sentinel = redis.Sentinel(config.sentinel.sentinels, socket_timeout=config.socket_timeout_seconds,
            socket_connect_timeout=config.connect_timeout_seconds, sentinel_kwargs={"username": config.security.username, "password": config.security.password})
        return sentinel.master_for(config.sentinel.master_name, **common)

    def __repr__(self) -> str:
        return "ValkeyFoundation(connection=<redacted>)"

    def _manifest(self) -> SchemaManifest:
        try:
            raw = self._client.get(self.config.key("schema"))
        except redis.RedisError as exc:
            raise ValkeyUnavailableError("state backend unavailable") from exc
        if raw is None:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return SchemaManifest.decode(raw)

    def initialize_manifest(self) -> SchemaManifest:
        try:
            created = self._client.set(self.config.key("schema"), self.expected_manifest.encode(), nx=True)
        except redis.RedisError as exc:
            raise ValkeyUnavailableError("state backend unavailable") from exc
        manifest = self.expected_manifest if created else self._manifest()
        self.require_write_compatible(manifest)
        return manifest

    def require_read_compatible(self, manifest: SchemaManifest | None = None) -> SchemaManifest:
        manifest = manifest or self._manifest()
        c = self.config
        expected = self.expected_manifest
        ok = (manifest.schema_major == c.schema_major and manifest.reader_min <= c.reader_revision <= manifest.reader_max
              and c.supported_schema_read_min <= manifest.active_schema_revision <= c.supported_schema_read_max
              and manifest.script_digests == expected.script_digests and manifest.migration_epoch == expected.migration_epoch)
        if not ok:
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return manifest

    def require_write_compatible(self, manifest: SchemaManifest | None = None) -> SchemaManifest:
        manifest = self.require_read_compatible(manifest)
        c = self.config
        if not (manifest.writer_min <= c.writer_revision <= manifest.writer_max
                and c.supported_writer_min <= manifest.active_writer_revision <= c.supported_writer_max):
            raise ValkeySchemaIncompatibleError("state schema incompatible")
        return manifest

    def server_time(self) -> tuple[int, int]:
        self.require_read_compatible()
        result = self.execute_script("server_time_v1", mutating=False)
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ValkeyScriptError("invalid script result")
        try:
            return int(result[0]), int(result[1])
        except (TypeError, ValueError) as exc:
            raise ValkeyScriptError("invalid script result") from exc

    def execute_script(self, name: str, *, mutating: bool, keys: tuple[str, ...] = (), args: tuple[bytes, ...] = ()) -> object:
        script = SCRIPT_REGISTRY.get(name)
        if script is None or self.expected_manifest.script_digests.get(name) != script.digest:
            raise ValkeyScriptError("unapproved script")
        (self.require_write_compatible() if mutating else self.require_read_compatible())
        sha1 = hashlib.sha1(script.source.encode(), usedforsecurity=False).hexdigest()
        try:
            try:
                result = self._client.evalsha(sha1, len(keys), *keys, *args)
            except redis.exceptions.NoScriptError:
                loaded = self._client.script_load(script.source)
                loaded_sha = loaded.decode() if isinstance(loaded, bytes) else loaded
                if loaded_sha != sha1:
                    raise ValkeyScriptError("reviewed script digest mismatch")
                result = self._client.evalsha(sha1, len(keys), *keys, *args)
        except ValkeyScriptError:
            raise
        except redis.exceptions.ReadOnlyError as exc:
            raise ValkeyReadOnlyError("state backend is not writable primary") from exc
        except redis.RedisError as exc:
            raise ValkeyUnavailableError("state backend unavailable") from exc
        if len(repr(result).encode()) > _MAX_SCRIPT_RESULT_BYTES:
            raise ValkeyScriptError("script result exceeds bound")
        return result

    def readiness(self) -> None:
        try:
            if not self._client.ping():
                raise ValkeyUnavailableError("state backend unavailable")
            role = self._client.role()
            if not isinstance(role, (list, tuple)) or not role or role[0] not in (b"master", "master"):
                raise ValkeyReadOnlyError("state backend is not writable primary")
            self.require_write_compatible()
        except (ValkeyFoundationError,):
            raise
        except redis.exceptions.ReadOnlyError as exc:
            raise ValkeyReadOnlyError("state backend is not writable primary") from exc
        except redis.RedisError as exc:
            raise ValkeyUnavailableError("state backend unavailable") from exc

    def close(self) -> None:
        self._client.close()
        self._client.connection_pool.disconnect()
