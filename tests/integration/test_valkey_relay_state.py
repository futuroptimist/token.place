import concurrent.futures
import shutil
import socket
import subprocess
import time
import uuid
import hashlib
from threading import Barrier

import pytest
import redis

from relay_state_store import (
    ComputeNodeCapabilities,
    RelayStateCapacityExceeded,
    RelayStateCredentialMismatch,
    RelayStateStoreConfig,
)

from valkey_relay_state import (
    DirectPrimary,
    SCRIPT_DIGESTS,
    SERVER_TIME_SCRIPT,
    SchemaManifest,
    ValkeyConfig,
    ValkeyFoundation,
    ValkeyRegistrationStore,
    ValkeyReadOnlyError,
    ValkeySchemaIncompatibleError,
    ValkeyUnavailableError,
)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def valkey_server(tmp_path_factory):
    executable = shutil.which("valkey-server")
    if executable is None:
        pytest.fail("valkey-server is required for the real-backend integration tests")
    port = _free_port()
    work = tmp_path_factory.mktemp("valkey-foundation")
    process = subprocess.Popen(
        [
            executable,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(work),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    probe = None
    try:
        probe = redis.Redis(host="127.0.0.1", port=port, socket_timeout=0.2)
        for _ in range(100):
            try:
                if probe.ping():
                    break
            except redis.RedisError:
                time.sleep(0.02)
        else:
            raise RuntimeError("isolated Valkey did not start")
        yield port
    finally:
        if probe is not None:
            probe.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _manifest(**changes):
    values = dict(
        schema_major=1,
        active_schema_revision=1,
        active_writer_revision=1,
        reader_min=1,
        reader_max=1,
        writer_min=1,
        writer_max=1,
        script_digests=SCRIPT_DIGESTS,
        migration_epoch=0,
    )
    values.update(changes)
    return SchemaManifest(**values)


def _foundation(port, namespace=None, expected=None):
    cfg = ValkeyConfig(
        environment="test",
        cluster=namespace or uuid.uuid4().hex,
        schema_major=1,
        reader_revision=1,
        writer_revision=1,
        supported_schema_read_min=1,
        supported_schema_read_max=1,
        supported_writer_min=1,
        supported_writer_max=1,
        direct=DirectPrimary("127.0.0.1", port),
        connect_timeout_seconds=0.2,
        socket_timeout_seconds=0.4,
        command_timeout_seconds=0.4,
        retry_timeout_seconds=0.05,
        retry_attempts=1,
    )
    return ValkeyFoundation(cfg, expected or _manifest())


def test_atomic_initialization_compatibility_readiness_and_exact_cleanup(valkey_server):
    foundation = _foundation(valkey_server)
    try:
        assert foundation.initialize_manifest() == _manifest()
        assert foundation.initialize_manifest() == _manifest()
        foundation.readiness()
    finally:
        foundation._client.delete(foundation.config.key("schema"))
        foundation.close()


def test_incompatible_existing_manifest_is_not_repaired(valkey_server):
    foundation = _foundation(valkey_server)
    incompatible = _manifest(schema_major=2)
    try:
        foundation._client.set(foundation.config.key("schema"), incompatible.encode())
        with pytest.raises(ValkeySchemaIncompatibleError):
            foundation.initialize_manifest()
        assert (
            foundation._client.get(foundation.config.key("schema"))
            == incompatible.encode()
        )
    finally:
        foundation._client.delete(foundation.config.key("schema"))
        foundation.close()


def test_concurrent_manifest_initialization_is_atomic(valkey_server):
    namespace = uuid.uuid4().hex
    stores = [_foundation(valkey_server, namespace) for _ in range(12)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda store: store.initialize_manifest(), stores))
        assert results == [_manifest()] * 12
        assert (
            stores[0]._client.get(stores[0].config.key("schema"))
            == _manifest().encode()
        )
    finally:
        stores[0]._client.delete(stores[0].config.key("schema"))
        for store in stores:
            store.close()


def test_server_time_and_exact_noscript_recovery_without_lifecycle_mutation(
    valkey_server,
):
    foundation = _foundation(valkey_server)
    try:
        foundation.initialize_manifest()
        assert foundation._client.script_exists(SERVER_TIME_SCRIPT.eval_sha1) == [False]
        before = foundation._client.dbsize()
        seconds, micros = foundation.server_time()
        assert abs(seconds - time.time()) < 5 and 0 <= micros < 1_000_000
        assert foundation._client.script_exists(SERVER_TIME_SCRIPT.eval_sha1) == [True]
        assert foundation._client.dbsize() == before  # only the schema manifest exists
    finally:
        foundation._client.delete(foundation.config.key("schema"))
        foundation.close()


def test_conflicting_concurrent_initializers_preserve_one_manifest(valkey_server):
    namespace = uuid.uuid4().hex
    first = _foundation(valkey_server, namespace)
    other_manifest = _manifest(migration_epoch=1)
    second = _foundation(valkey_server, namespace, other_manifest)
    assert first.expected_manifest.script_digests == SCRIPT_DIGESTS
    assert second.expected_manifest.script_digests == SCRIPT_DIGESTS
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        futures = [pool.submit(store.initialize_manifest) for store in (first, second)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ValkeySchemaIncompatibleError:
                outcomes.append("rejected")
        stored = first._client.get(first.config.key("schema"))
        assert outcomes.count("rejected") == 1
        assert len([outcome for outcome in outcomes if outcome != "rejected"]) == 1
        assert stored in {_manifest().encode(), other_manifest.encode()}
    finally:
        pool.shutdown()
        first._client.delete(first.config.key("schema"))
        first.close()
        second.close()


def test_unavailable_backend_is_bounded_and_redacted():
    foundation = _foundation(_free_port())
    started = time.monotonic()
    try:
        with pytest.raises(
            ValkeyUnavailableError, match="state backend unavailable"
        ) as caught:
            foundation.readiness()
        assert time.monotonic() - started < 3
        assert "127.0.0.1" not in str(caught.value)
    finally:
        foundation.close()


def test_read_only_role_is_classified_without_details(valkey_server):
    foundation = _foundation(valkey_server)
    original = foundation._client.role
    try:
        foundation._client.role = lambda: [b"slave"]
        with pytest.raises(ValkeyReadOnlyError, match="not writable"):
            foundation.readiness()
    finally:
        foundation._client.role = original
        foundation.close()


def _registration_store(port, namespace, **overrides):
    foundation = _foundation(port, namespace)
    foundation.initialize_manifest()
    return ValkeyRegistrationStore(
        foundation,
        RelayStateStoreConfig(namespace="testing.valkey", **overrides),
    )


def _capabilities(concurrency=2):
    return ComputeNodeCapabilities(
        supported_model_ids=("qwen3-8b-instruct",),
        active_context_tier="8k-fast",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=1024,
        maximum_output_tokens=2048,
        max_concurrency=concurrency,
        backend_class="cuda",
    )


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_registration_contract_is_atomic_across_independent_clients(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, max_compute_nodes=2)
    second = _registration_store(valkey_server, namespace, max_compute_nodes=2)
    owner = _digest("owner")
    try:
        created = first.register("node-b", _capabilities(), owner)
        assert created == second.get("node-b")
        assert second.register("node-b", _capabilities(3), owner).capabilities.max_concurrency == 3
        with pytest.raises(RelayStateCredentialMismatch):
            second.register("node-b", _capabilities(), _digest("other"))
        assert first.renew("unknown", owner) is None
        first.register("node-a", _capabilities(), owner)
        assert [record.node_id for record in second.list()] == ["node-a", "node-b"]
        with pytest.raises(RelayStateCapacityExceeded):
            second.register("node-c", _capabilities(), owner)
        assert first.unregister("node-a", owner) is True
        assert second.unregister("node-a", owner) is False
        second.register("node-c", _capabilities(), owner)

        barrier = Barrier(2)
        def compete(store, credential):
            barrier.wait()
            try:
                return store.register("node-b", _capabilities(), credential).control_credential_digest
            except RelayStateCredentialMismatch:
                return "rejected"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, (first, second), (owner, _digest("rival"))))
        assert sorted(results) == sorted(["rejected", owner])
    finally:
        keys = [
            first._foundation.config.key("schema"),
            first._foundation.config.key("nodes:lease"),
        ]
        for node_id in ("node-a", "node-b", "node-c", "unknown"):
            keys.append(first._foundation.config.key("node", first._node_digest(node_id)))
        first._foundation._client.delete(*keys)
        first.close()
        second.close()


def test_registration_expiry_is_server_timed_and_recovers_capacity(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server, namespace, max_compute_nodes=1, lease_ttl_seconds=0.02
    )
    owner = _digest("owner")
    try:
        record = store.register("node-a", _capabilities(), owner)
        server_seconds, server_micros = store._foundation.server_time()
        assert abs(record.registered_at_epoch - (server_seconds + server_micros / 1_000_000)) < 1
        time.sleep(0.03)
        assert store.get("node-a") is None
        assert store.register("node-b", _capabilities(), owner).node_id == "node-b"
    finally:
        keys = [
            store._foundation.config.key("schema"),
            store._foundation.config.key("nodes:lease"),
            store._foundation.config.key("node", store._node_digest("node-a")),
            store._foundation.config.key("node", store._node_digest("node-b")),
        ]
        store._foundation._client.delete(*keys)
        store.close()
