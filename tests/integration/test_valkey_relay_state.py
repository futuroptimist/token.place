import concurrent.futures
import shutil
import socket
import subprocess
import time
import uuid
import hashlib

import pytest
import redis

from valkey_relay_state import (
    DirectPrimary,
    SCRIPT_DIGESTS,
    SERVER_TIME_SCRIPT,
    SchemaManifest,
    ValkeyConfig,
    ValkeyFoundation,
    ValkeyReadOnlyError,
    ValkeySchemaIncompatibleError,
    ValkeyUnavailableError,
    ValkeyRegistrationStore,
)
from relay_state_store import (
    ComputeNodeCapabilities,
    RelayStateCapacityExceeded,
    RelayStateCredentialMismatch,
    RelayStateStoreConfig,
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


def _registration_store(port, namespace, **changes):
    foundation = _foundation(port, namespace)
    foundation.initialize_manifest()
    return ValkeyRegistrationStore(
        foundation,
        RelayStateStoreConfig(namespace="testing.cluster-a", **changes),
    )


def _capabilities(max_concurrency=2):
    return ComputeNodeCapabilities(
        supported_model_ids=("qwen3-8b-instruct",),
        active_context_tier="8k-fast",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=1024,
        maximum_output_tokens=2048,
        max_concurrency=max_concurrency,
        backend_class="cuda",
    )


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _cleanup_registration(store, node_ids):
    client = store._foundation._client
    keys = [store._foundation.config.key("schema"), store._lease_key()]
    keys.extend(store._node_key(node_id) for node_id in node_ids)
    client.delete(*keys)
    store.close()


def test_registration_contract_shared_instances_and_capability_replacement(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    nodes = ["node-a"]
    try:
        registered = first.register("node-a", _capabilities(), _digest("owner"))
        assert second.get("node-a") == registered
        changed = _capabilities(max_concurrency=4)
        renewed = second.renew("node-a", _digest("owner"), capabilities=changed)
        assert renewed.capabilities == changed
        assert first.list() == (renewed,)
        with pytest.raises(RelayStateCredentialMismatch):
            first.register("node-a", changed, _digest("attacker"))
        with pytest.raises(RelayStateCredentialMismatch):
            second.unregister("node-a", _digest("attacker"))
        assert first.renew("missing", _digest("owner")) is None
        assert second.unregister("node-a", _digest("owner"))
        assert not first.unregister("node-a", _digest("owner"))
    finally:
        _cleanup_registration(first, nodes)
        second.close()


def test_registration_capacity_is_atomic_and_recovers_after_inclusive_expiry(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = [
        _registration_store(
            valkey_server, namespace, max_compute_nodes=1, lease_ttl_seconds=0.05
        )
        for _ in range(2)
    ]
    nodes = ["node-a", "node-b"]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(store.register, node, _capabilities(), _digest(node))
                for store, node in zip(stores, nodes)
            ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result().node_id)
            except RelayStateCapacityExceeded:
                outcomes.append("capacity")
        assert outcomes.count("capacity") == 1
        time.sleep(0.06)
        loser = next(node for node in nodes if node not in outcomes)
        assert (
            stores[0].register(loser, _capabilities(), _digest(loser)).node_id == loser
        )
    finally:
        _cleanup_registration(stores[0], nodes)
        stores[1].close()


def test_concurrent_different_owner_registration_has_one_winner(valkey_server):
    namespace = uuid.uuid4().hex
    stores = [_registration_store(valkey_server, namespace) for _ in range(2)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(store.register, "node-a", _capabilities(), _digest(owner))
                for store, owner in zip(stores, ("owner-a", "owner-b"))
            ]
        winners = 0
        for future in futures:
            try:
                future.result()
                winners += 1
            except RelayStateCredentialMismatch:
                pass
        assert winners == 1
        assert stores[0].get("node-a") == stores[1].get("node-a")
    finally:
        _cleanup_registration(stores[0], ["node-a"])
        stores[1].close()
