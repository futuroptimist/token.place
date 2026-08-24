from __future__ import annotations

import logging
import socket
import subprocess
import threading
import uuid
from dataclasses import replace

import pytest
import redis

from relay_valkey import (
    DirectPrimary, Keyspace, SCRIPT_REGISTRY, SERVER_TIME_SCRIPT,
    SchemaCompatibilityError, SchemaManifest, ScriptExecutionError,
    SentinelDiscovery, ValkeyConfig, ValkeyConfigurationError, ValkeyFoundation,
    ValkeyReadOnlyError, ValkeyUnavailableError, VersionedScript,
)


def config(port: int = 6379, namespace: str = "unit") -> ValkeyConfig:
    return ValkeyConfig(
        environment="test", cluster=namespace, schema_major=1,
        reader_revision=2, writer_revision=2,
        supported_schema_read_min=1, supported_schema_read_max=3,
        supported_writer_min=1, supported_writer_max=3,
        connect_timeout=.25, socket_timeout=.25, command_timeout=.25,
        retry_timeout=.1, retry_attempts=1,
        direct=DirectPrimary("127.0.0.1", port),
    )


def manifest(**changes) -> SchemaManifest:
    base = dict(
        schema_major=1, active_schema_revision=2, active_writer_revision=2,
        supported_reader_min=1, supported_reader_max=3,
        supported_writer_min=1, supported_writer_max=3,
        script_digests={name: script.sha1 for name, script in SCRIPT_REGISTRY.items()},
        migration_epoch=0,
    )
    base.update(changes)
    return SchemaManifest(**base)


def test_exact_key_prefix_and_hash_tag():
    keys = Keyspace("staging", "relay-a", 4)
    assert keys.prefix == "tokenplace:{staging:relay-a}:relay:v4:"
    assert keys.key("schema") == "tokenplace:{staging:relay-a}:relay:v4:schema"
    assert keys.prefix.count("{") == keys.prefix.count("}") == 1


def test_discovery_and_bounded_configuration_validation():
    assert config().direct == DirectPrimary("127.0.0.1", 6379)
    sentinel = SentinelDiscovery((("sentinel.internal", 26379),), "relay-primary", "user", "secret")
    assert "secret" not in repr(sentinel) and "sentinel.internal" not in repr(sentinel)
    assert replace(config(), direct=None, sentinel=sentinel).sentinel is sentinel
    for change in ({"direct": None}, {"sentinel": sentinel}, {"connect_timeout": 0},
                   {"command_timeout": 31}, {"retry_attempts": 4}, {"retry_timeout": 31}):
        with pytest.raises(ValkeyConfigurationError):
            replace(config(), **change)
    with pytest.raises(ValkeyConfigurationError):
        SentinelDiscovery((), "primary")


def test_redacted_representations_errors_and_logs(caplog):
    secret = "never-print-this"
    cfg = replace(config(), password=secret, username="private-user",
                  direct=DirectPrimary("private.example", 6380))
    foundation = ValkeyFoundation(cfg)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValkeyUnavailableError) as caught:
            foundation.read_manifest()
    rendered = repr(cfg) + repr(foundation) + repr(caught.value) + caplog.text
    assert secret not in rendered
    assert "private-user" not in rendered
    assert "private.example" not in rendered


def test_compatibility_gates_fail_before_protocol_dispatch():
    class NeverClient:
        def __getattr__(self, name):
            raise AssertionError(f"protocol call: {name}")
    f = ValkeyFoundation(config(), client=NeverClient())
    bad_digest = manifest(script_digests={"server_time_v1": "0" * 40})
    for bad in (manifest(schema_major=2), manifest(supported_reader_min=3),
                manifest(active_schema_revision=4), bad_digest):
        with pytest.raises(SchemaCompatibilityError):
            f.require_read_compatible(bad)
    for bad in (manifest(supported_writer_min=3), manifest(active_writer_revision=4)):
        with pytest.raises(SchemaCompatibilityError):
            f.require_write_compatible(bad)


def test_registry_rejects_arbitrary_and_digest_mismatched_scripts():
    assert SERVER_TIME_SCRIPT.sha1 == VersionedScript.trusted(
        SERVER_TIME_SCRIPT.name, SERVER_TIME_SCRIPT.source).sha1
    f = ValkeyFoundation(config(), client=object())
    with pytest.raises(ScriptExecutionError):
        f.execute("caller_supplied")
    assert not hasattr(f, "eval")


class RoleClient:
    def __init__(self, role): self._role = role
    def ping(self): return True
    def role(self): return self._role


def test_readiness_classifies_read_only_without_reading_manifest():
    f = ValkeyFoundation(config(), client=RoleClient([b"slave"]))
    with pytest.raises(ValkeyReadOnlyError):
        f.readiness()


def test_unavailable_is_typed_and_bounded():
    f = ValkeyFoundation(config(port=1))
    with pytest.raises(ValkeyUnavailableError) as caught:
        f.readiness()
    assert str(caught.value) == "state_backend_unavailable"


@pytest.fixture(scope="module")
def valkey_server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("valkey")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    proc = subprocess.Popen([
        "valkey-server", "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--dir", str(directory),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = redis.Redis(host="127.0.0.1", port=port, socket_timeout=.2)
    for _ in range(50):
        try:
            if probe.ping(): break
        except redis.RedisError:
            threading.Event().wait(.02)
    else:
        proc.terminate(); pytest.fail("isolated Valkey did not start")
    yield port
    probe.close(); proc.terminate(); proc.wait(timeout=5)


@pytest.fixture
def live_foundation(valkey_server):
    namespace = "t" + uuid.uuid4().hex
    f = ValkeyFoundation(config(valkey_server, namespace))
    yield f
    # This fixture owns exactly one known foundation key. Never scan or flush.
    f._client.delete(f.keys.key("schema"))
    f.close()


def test_atomic_manifest_creation_and_readiness(live_foundation):
    expected = manifest()
    assert live_foundation.initialize_manifest(expected) == expected
    assert live_foundation.initialize_manifest(expected) == expected
    live_foundation.readiness()
    with pytest.raises(SchemaCompatibilityError):
        live_foundation.initialize_manifest(manifest(migration_epoch=1))
    assert live_foundation.read_manifest() == expected


def test_concurrent_manifest_initialization_has_one_compatible_winner(live_foundation):
    barrier = threading.Barrier(2)
    results = []
    def initialize(epoch):
        barrier.wait()
        try: results.append(live_foundation.initialize_manifest(manifest(migration_epoch=epoch)))
        except SchemaCompatibilityError: results.append("incompatible")
    threads = [threading.Thread(target=initialize, args=(epoch,)) for epoch in (1, 2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len([value for value in results if value != "incompatible"]) == 1
    assert results.count("incompatible") == 1


def test_server_time_and_exact_noscript_recovery(live_foundation):
    live_foundation.initialize_manifest(manifest())
    seconds, micros = live_foundation.server_time()
    assert seconds > 1_700_000_000 and 0 <= micros < 1_000_000
    live_foundation._client.script_flush()
    result = live_foundation.execute("server_time_v1")
    assert len(result) == 2
    assert live_foundation._client.script_exists(SERVER_TIME_SCRIPT.sha1) == [True]


def test_foundation_does_not_create_lifecycle_state(live_foundation):
    live_foundation.initialize_manifest(manifest())
    live_foundation.execute("server_time_v1")
    # Exact, finite key assertions only; no broad scan is used.
    for suffix in ("nodes:lease", "cursor", "requests:deadline", "claims:expiry",
                   "responses:expiry", "terminals:expiry"):
        assert live_foundation._client.exists(live_foundation.keys.key(suffix)) == 0
