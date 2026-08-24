import concurrent.futures
import logging
import socket
import subprocess
import time
import uuid

import pytest

from valkey_relay_state import (
    DirectPrimaryDiscovery,
    SCRIPT_REGISTRY,
    SchemaManifest,
    SentinelDiscovery,
    ValkeyConfig,
    ValkeyConfigurationError,
    ValkeyFoundation,
    ValkeySchemaIncompatibleError,
    ValkeyScriptError,
    ValkeySecurity,
    ValkeyUnavailableError,
)


def config(*, port=6379, direct=True, sentinel=False, **changes):
    values = dict(environment="test", cluster="c-" + uuid.uuid4().hex, schema_major=1,
        reader_revision=2, writer_revision=2, supported_schema_read_min=1,
        supported_schema_read_max=3, supported_writer_min=1, supported_writer_max=3,
        direct=DirectPrimaryDiscovery("127.0.0.1", port) if direct else None,
        sentinel=SentinelDiscovery((("sentinel.invalid", 26379),), "relay-primary") if sentinel else None)
    values.update(changes)
    return ValkeyConfig(**values)


def manifest(**changes):
    values = dict(schema_major=1, active_schema_revision=2, active_writer_revision=2,
        reader_min=1, reader_max=3, writer_min=1, writer_max=3,
        script_digests={name: script.digest for name, script in SCRIPT_REGISTRY.items()}, migration_epoch=1)
    values.update(changes)
    return SchemaManifest(**values)


def test_exact_key_hash_tag_and_schema_prefix():
    c = config(cluster="relay-a")
    assert c.key_prefix == "tokenplace:{test:relay-a}:relay:v1:"
    assert c.key("schema") == "tokenplace:{test:relay-a}:relay:v1:schema"


@pytest.mark.parametrize("kwargs", [{}, {"direct": True, "sentinel": True}])
def test_requires_exactly_one_discovery_mode(kwargs):
    with pytest.raises(ValkeyConfigurationError):
        config(direct=kwargs.get("direct", False), sentinel=kwargs.get("sentinel", False))


def test_sentinel_and_security_validation_and_redaction():
    c = config(direct=False, sentinel=True, security=ValkeySecurity(username="user", password="secret", tls=True))
    text = repr(c) + repr(c.sentinel) + repr(c.security)
    assert "secret" not in text and "sentinel.invalid" not in text and "user" not in text
    with pytest.raises(ValkeyConfigurationError):
        SentinelDiscovery((), "master")
    with pytest.raises(ValkeyConfigurationError):
        ValkeySecurity(client_cert_path="cert")


@pytest.mark.parametrize("changes", [{"connect_timeout_seconds": float("inf")}, {"socket_timeout_seconds": 0},
    {"command_timeout_seconds": 31}, {"retry_timeout_seconds": -1}, {"max_retries": 6}])
def test_timeouts_and_retries_are_bounded(changes):
    with pytest.raises(ValkeyConfigurationError):
        config(**changes)


@pytest.fixture(scope="module")
def valkey_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(["valkey-server", "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.1):
                break
        except OSError:
            time.sleep(.02)
    else:
        process.terminate(); raise RuntimeError("Valkey did not start")
    yield port
    process.terminate(); process.wait(timeout=5)


@pytest.fixture
def foundation(valkey_port):
    f = ValkeyFoundation(config(port=valkey_port), manifest())
    yield f
    # Delete only this test's exact manifest key; no scan/flush is used.
    f._client.delete(f.config.key("schema"))
    f.close()


def test_atomic_manifest_creation_and_compatibility_gates(foundation):
    assert foundation.initialize_manifest() == foundation.expected_manifest
    assert foundation.require_read_compatible().active_schema_revision == 2
    assert foundation.require_write_compatible().active_writer_revision == 2
    incompatible = manifest(schema_major=2)
    with pytest.raises(ValkeySchemaIncompatibleError):
        ValkeyFoundation(foundation.config, incompatible)
    assert foundation._manifest() == foundation.expected_manifest


def test_concurrent_manifest_initialization(valkey_port):
    c = config(port=valkey_port)
    expected = manifest()
    stores = [ValkeyFoundation(c, expected) for _ in range(8)]
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(lambda store: store.initialize_manifest(), stores))
        assert results == [expected] * 8
    finally:
        stores[0]._client.delete(c.key("schema"))
        for store in stores: store.close()


@pytest.mark.parametrize("change", [dict(schema_major=2), dict(active_schema_revision=4),
    dict(reader_min=3), dict(active_writer_revision=4), dict(writer_min=3),
    dict(script_digests={"server_time_v1": "0" * 64})])
def test_incompatible_manifest_rejected(valkey_port, change):
    f = ValkeyFoundation(config(port=valkey_port), manifest())
    try:
        f._client.set(f.config.key("schema"), manifest(**change).encode())
        with pytest.raises(ValkeySchemaIncompatibleError):
            f.require_write_compatible()
    finally:
        f._client.delete(f.config.key("schema")); f.close()


def test_server_time_and_exact_noscript_recovery(foundation):
    foundation.initialize_manifest()
    seconds, micros = foundation.server_time()  # empty server forces one SCRIPT LOAD recovery
    assert abs(seconds - time.time()) < 5 and 0 <= micros < 1_000_000
    assert foundation._client.script_exists("bad", foundation._client.script_load(SCRIPT_REGISTRY["server_time_v1"].source)) == [False, True]


def test_arbitrary_and_digest_mismatched_scripts_are_rejected_before_dispatch(foundation):
    foundation.initialize_manifest()
    with pytest.raises(ValkeyScriptError):
        foundation.execute_script("return redis.call('SET','x','y')", mutating=True)
    bad = ValkeyFoundation(foundation.config, manifest(script_digests={"server_time_v1": "0" * 64}))
    with pytest.raises(ValkeyScriptError):
        bad.execute_script("server_time_v1", mutating=True)
    assert foundation._client.dbsize() == 1  # schema only: no lifecycle state was mutated
    bad.close()


def test_readiness_and_unavailable_error_are_redacted(foundation, caplog):
    foundation.initialize_manifest(); foundation.readiness()
    unavailable = ValkeyFoundation(config(port=1, security=ValkeySecurity(password="very-secret")), manifest())
    with caplog.at_level(logging.DEBUG), pytest.raises(ValkeyUnavailableError) as caught:
        unavailable.readiness()
    public = str(caught.value) + repr(unavailable) + caplog.text
    assert "very-secret" not in public and "127.0.0.1" not in public and foundation.config.key("schema") not in public
    unavailable.close()
