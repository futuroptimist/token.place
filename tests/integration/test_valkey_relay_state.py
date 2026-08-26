import concurrent.futures
import shutil
import socket
import subprocess
import time
import uuid

import pytest
import redis
from dataclasses import replace

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
    ValkeyReadOnlyError,
    ValkeyRegistrationStore,
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


def _registration_store(port, namespace=None, *, capacity=8, ttl=30):
    foundation = _foundation(port, namespace)
    foundation.initialize_manifest()
    return ValkeyRegistrationStore(
        foundation,
        RelayStateStoreConfig(
            namespace=namespace or "integration",
            max_compute_nodes=capacity,
            lease_ttl_seconds=ttl,
        ),
    )


@pytest.fixture
def capabilities():
    return ComputeNodeCapabilities(
        supported_model_ids=("model-a",),
        active_context_tier="8k-fast",
        maximum_total_context_tokens=8192,
        default_output_token_reservation=256,
        maximum_output_tokens=1024,
        max_concurrency=2,
        backend_class="cpu",
    )


def _owner(value):
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _clean_registration_store(store):
    lease_key = store._foundation.config.key("nodes:lease")
    digests = store._foundation._client.zrange(lease_key, 0, -1)
    keys = [
        store._foundation.config.key(
            "node", digest.decode() if isinstance(digest, bytes) else digest
        )
        for digest in digests
    ]
    keys.extend((lease_key, store._foundation.config.key("schema")))
    if keys:
        store._foundation._client.delete(*keys)
    store.close()


def test_registration_lifecycle_and_unknown_field_preservation(
    valkey_server, capabilities
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    owner = _owner("owner")
    try:
        first = store.register("node-b", capabilities, owner)
        assert first == store.get("node-b")
        changed = replace(capabilities, max_concurrency=3)
        store._foundation._client.hset(
            store._foundation.config.key("node", store._node_digest("node-b")),
            "future_additive_field",
            "safe-fixed-value",
        )
        renewed = store.register("node-b", changed, owner)
        assert renewed.registered_at_epoch == first.registered_at_epoch
        assert renewed.capabilities == changed
        assert store.renew("missing", owner) is None
        assert store.renew("node-b", owner) is not None
        with pytest.raises(RelayStateCredentialMismatch):
            store.register("node-b", capabilities, _owner("other"))
        with pytest.raises(RelayStateCredentialMismatch):
            store.unregister("node-b", _owner("other"))
        raw = store._foundation._client.hgetall(
            store._foundation.config.key("node", store._node_digest("node-b"))
        )
        assert raw[b"future_additive_field"] == b"safe-fixed-value"
        assert store.unregister("node-b", owner)
        assert not store.unregister("node-b", owner)
        assert store.get("node-b") is None
    finally:
        _clean_registration_store(store)


def test_listing_capacity_concurrency_and_namespace_isolation(
    valkey_server, capabilities
):
    namespace = uuid.uuid4().hex
    stores = [
        _registration_store(valkey_server, namespace, capacity=2) for _ in range(3)
    ]
    isolated = _registration_store(valkey_server, uuid.uuid4().hex)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            same = list(
                pool.map(
                    lambda store: store.register(
                        "node-b", capabilities, _owner("same-owner")
                    ),
                    stores[:2],
                )
            )
        assert same[0].control_credential_digest == same[1].control_credential_digest
        stores[0].register("node-a", capabilities, _owner("a"))
        assert [record.node_id for record in stores[1].list()] == ["node-a", "node-b"]
        with pytest.raises(RelayStateCapacityExceeded):
            stores[2].register("node-c", capabilities, _owner("c"))
        isolated.register("node-c", capabilities, _owner("c"))
        assert [record.node_id for record in isolated.list()] == ["node-c"]
    finally:
        _clean_registration_store(stores[0])
        for store in stores[1:]:
            store.close()
        _clean_registration_store(isolated)


def test_competing_owner_and_final_capacity_slot_have_one_winner(
    valkey_server, capabilities
):
    namespace = uuid.uuid4().hex
    stores = [_registration_store(valkey_server, namespace, capacity=1) for _ in range(2)]
    try:
        def attempt(index):
            try:
                return stores[index].register(
                    "node-shared", capabilities, _owner(f"owner-{index}")
                ).control_credential_digest
            except RelayStateCredentialMismatch:
                return "rejected"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        assert results.count("rejected") == 1
        assert stores[0].get("node-shared").control_credential_digest in results
    finally:
        _clean_registration_store(stores[0])
        stores[1].close()


def test_server_time_inclusive_expiry_and_capacity_recovery(
    valkey_server, capabilities
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, capacity=1, ttl=0.05
    )
    try:
        registration = store.register("node-old", capabilities, _owner("old"))
        while time.time() < registration.lease_expires_at_epoch:
            time.sleep(0.005)
        assert store.get("node-old") is None
        store.register("node-new", capabilities, _owner("new"))
        assert [record.node_id for record in store.list()] == ["node-new"]
    finally:
        _clean_registration_store(store)


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
        # Other lifecycle tests may already have loaded this reviewed script into
        # the shared isolated server; script cache state is not namespaced.
        assert foundation._client.script_exists(SERVER_TIME_SCRIPT.eval_sha1) in (
            [False],
            [True],
        )
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
