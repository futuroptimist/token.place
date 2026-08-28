import concurrent.futures
import dataclasses
import hashlib
import logging
import math
import shutil
import socket
import subprocess
import time
import traceback
import uuid
from threading import Barrier

import pytest
import redis

from relay_state_store import (
    ComputeNodeCapabilities,
    EncryptedRequestEnvelope,
    InMemoryRelayStateStore,
    RelayStateCapacityExceeded,
    RelayStateCredentialMismatch,
    RelayStateInvalidReservation,
    RelayStateNoCapacity,
    RelayStateStoreConfig,
    SchedulerNodeState,
)
from tests.registration_store_contract import assert_registration_contract
from valkey_relay_state import (
    SCRIPT_DIGESTS,
    SERVER_TIME_SCRIPT,
    DirectPrimary,
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


def _scheduler_policy_capabilities(
    *, models=("qwen3-8b-instruct",), tier="8k-fast", concurrency=4
):
    tokens = 65536 if tier == "64k-full" else 8192
    return dataclasses.replace(
        _capabilities(concurrency=concurrency),
        supported_model_ids=models,
        active_context_tier=tier,
        maximum_total_context_tokens=tokens,
    )


def _delete_scheduler_policy_state(store, node_ids, selections=()):
    cfg = store._foundation.config
    keys = [
        cfg.key("schema"),
        cfg.key("nodes:lease"),
        cfg.key("cursor"),
        cfg.key("reservations:expiry"),
        cfg.key("requests:deadline"),
    ]
    keys.extend(cfg.key("node", store._node_digest(node)) for node in node_ids)
    for client, request, selection in selections:
        keys.append(
            cfg.key(
                "request",
                hashlib.sha256(f"client\0{client}".encode()).hexdigest(),
                hashlib.sha256(f"request\0{request}".encode()).hexdigest(),
            )
        )
        if selection is not None and selection.reservation_token is not None:
            keys.append(cfg.key("reservation", _digest(selection.reservation_token)))
    store._foundation._client.delete(*keys)


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("bounded polling condition was not reached")


def _registration_keys(store, *node_ids):
    return [
        store._foundation.config.key("schema"),
        store._foundation.config.key("nodes:lease"),
        store._foundation.config.key("cursor"),
        *(
            store._foundation.config.key("node", store._node_digest(node_id))
            for node_id in node_ids
        ),
    ]


def test_scheduler_reservation_and_enqueue_are_shared_and_idempotent(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    owner = _digest("owner")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    deadline = time.time() + 30
    selected = None
    try:
        registered = first.register("node", _capabilities(concurrency=1), owner)
        node_key = first._foundation.config.key("node", first._node_digest("node"))
        assert first._foundation._client.hmget(
            node_key,
            "scheduler_healthy",
            "scheduler_draining",
            "scheduler_claimed_work",
        ) == [b"1", b"0", b"0"]
        assert second.set_scheduler_state(
            "node", owner, SchedulerNodeState(healthy=True, claimed_work=0)
        )

        selected = first.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        assert selected.created and selected.selected_node_id == registered.node_id
        assert selected.reservation_token is not None
        retry = second.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        assert not retry.created and retry.reservation_token is None
        assert len(second.list_reservations()) == 1

        queued = second.enqueue_encrypted_request(
            "client",
            "request",
            selected.reservation_token,
            "node",
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            envelope,
            "cancel",
        )
        assert queued.created and queued.sequence == 1
        repeated = first.enqueue_encrypted_request(
            "client",
            "request",
            selected.reservation_token,
            "node",
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            envelope,
            "cancel",
        )
        assert not repeated.created and repeated.sequence == queued.sequence
        selection_after_enqueue = second.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
        )
        assert not selection_after_enqueue.created
        assert selection_after_enqueue.state == "queued"
        assert first.list_reservations() == ()
        assert first.queued_requests("node")[0].envelope == envelope
    finally:
        cfg = first._foundation.config
        client = hashlib.sha256(b"client\0client").hexdigest()
        request = hashlib.sha256(b"request\0request").hexdigest()
        node = first._node_digest("node")
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", client, request),
        ]
        if selected is not None and selected.reservation_token is not None:
            token = hashlib.sha256(
                selected.reservation_token.encode("ascii")
            ).hexdigest()
            keys.append(cfg.key("reservation", token))
        first._foundation._client.delete(*keys)
        first.close()
        second.close()


def test_scheduler_state_contract_authentication_and_preservation(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    owner = _digest("scheduler-owner")
    wrong_owner = _digest("wrong-scheduler-owner")
    node_key = first._foundation.config.key(
        "node", first._node_digest("scheduler-node")
    )
    unknown_key = first._foundation.config.key(
        "node", first._node_digest("unknown-scheduler-node")
    )
    try:
        first.register("scheduler-node", _capabilities(), owner)
        assert first._foundation._client.hmget(
            node_key,
            "scheduler_healthy",
            "scheduler_draining",
            "scheduler_claimed_work",
        ) == [b"1", b"0", b"0"]

        updated = SchedulerNodeState(healthy=False, draining=True, claimed_work=1)
        assert first.set_scheduler_state("scheduler-node", owner, updated)
        assert second._foundation._client.hmget(
            node_key,
            "scheduler_healthy",
            "scheduler_draining",
            "scheduler_claimed_work",
        ) == [b"0", b"1", b"1"]

        unchanged = second._foundation._client.hgetall(node_key)
        with pytest.raises(RelayStateCredentialMismatch):
            second.set_scheduler_state(
                "scheduler-node", wrong_owner, SchedulerNodeState()
            )
        assert second._foundation._client.hgetall(node_key) == unchanged

        assert not second.set_scheduler_state(
            "unknown-scheduler-node", owner, SchedulerNodeState()
        )
        assert not second._foundation._client.exists(unknown_key)

        assert first.renew("scheduler-node", owner) is not None
        assert (
            second.renew(
                "scheduler-node",
                owner,
                capabilities=dataclasses.replace(_capabilities(), max_concurrency=3),
            )
            is not None
        )
        first.register(
            "scheduler-node",
            dataclasses.replace(_capabilities(), max_concurrency=4),
            owner,
        )
        assert second._foundation._client.hmget(
            node_key,
            "scheduler_healthy",
            "scheduler_draining",
            "scheduler_claimed_work",
        ) == [b"0", b"1", b"1"]
    finally:
        first._foundation._client.delete(
            *_registration_keys(first, "scheduler-node", "unknown-scheduler-node")
        )
        first.close()
        second.close()


def test_scheduler_state_lifecycle_unregister_and_inclusive_expiry(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    owner = _digest("scheduler-lifecycle-owner")
    cfg = first._foundation.config
    leases = cfg.key("nodes:lease")
    cursor = cfg.key("cursor")
    node_digest = first._node_digest("scheduler-node")
    node_key = cfg.key("node", node_digest)
    try:
        first.register("scheduler-node", _capabilities(), owner)
        assert second.unregister("scheduler-node", owner)
        assert second.get("scheduler-node") is None
        assert not first.set_scheduler_state(
            "scheduler-node", owner, SchedulerNodeState()
        )
        with pytest.raises(RelayStateNoCapacity):
            first.select_and_reserve(
                "client",
                "after-unregister",
                "qwen3-8b-instruct",
                "8k-fast",
                time.time() + 30,
            )
        assert not first._foundation._client.exists(node_key)
        assert first._foundation._client.zscore(leases, node_digest) is None

        first.register("scheduler-node", _capabilities(), owner)
        seconds, micros = first._foundation.server_time()
        cutoff = seconds + micros / 1_000_000
        first._foundation._client.zadd(leases, {node_digest: cutoff})
        assert not second.set_scheduler_state(
            "scheduler-node", owner, SchedulerNodeState(draining=True)
        )
        with pytest.raises(RelayStateNoCapacity):
            second.select_and_reserve(
                "client",
                "after-expiry",
                "qwen3-8b-instruct",
                "8k-fast",
                cutoff + 30,
            )
        assert second.get("scheduler-node") is None
        assert not second._foundation._client.exists(node_key)
        assert second._foundation._client.zscore(leases, node_digest) is None
    finally:
        first._foundation._client.delete(leases, cursor, node_key, cfg.key("schema"))
        first.close()
        second.close()


def test_scheduler_selection_policy_matches_in_memory_reference(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    memory = InMemoryRelayStateStore(
        RelayStateStoreConfig(namespace="policy-memory"), acknowledgement_key=b"k" * 32
    )
    stores = (memory, first)
    nodes = (
        ("unsupported", _scheduler_policy_capabilities(models=("other",))),
        ("too-small", _scheduler_policy_capabilities(models=("other",))),
        ("unhealthy", _scheduler_policy_capabilities(tier="64k-full")),
        ("draining", _scheduler_policy_capabilities(tier="64k-full")),
        ("large", _scheduler_policy_capabilities(tier="64k-full")),
        ("node-b", _scheduler_policy_capabilities()),
        ("node-a", _scheduler_policy_capabilities()),
    )
    selections = []
    try:
        for store in stores:
            for node, capabilities in nodes:
                store.register(node, capabilities, _digest(node))
            store.set_scheduler_state(
                "unhealthy", _digest("unhealthy"), SchedulerNodeState(healthy=False)
            )
            store.set_scheduler_state(
                "draining", _digest("draining"), SchedulerNodeState(draining=True)
            )

        deadline = time.time() + 60
        # A 64k request excludes unsupported, insufficient, unhealthy, and draining
        # nodes, leaving only the capable full-tier registration.
        assert [
            store.select_and_reserve(
                "client", "full", "qwen3-8b-instruct", "64k-full", deadline
            ).selected_node_id
            for store in stores
        ] == ["large", "large"]

        # The smallest capable tier wins; equal least-loaded candidates then rotate
        # in registration order (node-b before lexically earlier node-a).
        memory_results = [
            memory.select_and_reserve(
                "memory", f"round-{index}", "qwen3-8b-instruct", "8k-fast", deadline
            )
            for index in range(2)
        ]
        valkey_results = [
            (first if index == 0 else second).select_and_reserve(
                "valkey", f"round-{index}", "qwen3-8b-instruct", "8k-fast", deadline
            )
            for index in range(2)
        ]
        assert [result.selected_node_id for result in memory_results] == [
            "node-b", "node-a"
        ]
        assert [result.selected_node_id for result in valkey_results] == [
            result.selected_node_id for result in memory_results
        ]
        selections.extend(
            [("client", "full", None), *(
                ("valkey", f"round-{index}", result)
                for index, result in enumerate(valkey_results)
            )]
        )
    finally:
        _delete_scheduler_policy_state(first, [node for node, _ in nodes], selections)
        first.close()
        second.close()


def test_scheduler_fairness_cursor_changes_only_for_new_reservations(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, reservation_ttl_seconds=30)
    second = _registration_store(valkey_server, namespace, reservation_ttl_seconds=30)
    cfg = first._foundation.config
    cursor = cfg.key("cursor")
    deadline = first._foundation.server_time()[0] + 300
    selections = []
    try:
        for node in ("node-b", "node-a"):
            first.register(node, _scheduler_policy_capabilities(), _digest(node))

        initial = first._foundation._client.hgetall(cursor)
        with pytest.raises(RelayStateNoCapacity):
            second.select_and_reserve("client", "failed", "unsupported", "8k-fast", deadline)
        assert first._foundation._client.hgetall(cursor) == initial
        assert first._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert first._foundation._client.zcard(cfg.key("requests:deadline")) == 0

        created = first.select_and_reserve(
            "client", "first", "qwen3-8b-instruct", "8k-fast", deadline
        )
        selections.append(("client", "first", created))
        after_creation = first._foundation._client.hgetall(cursor)
        retry = second.select_and_reserve(
            "client", "first", "qwen3-8b-instruct", "8k-fast", deadline
        )
        assert not retry.created
        assert first._foundation._client.hgetall(cursor) == after_creation

        token = _digest(created.reservation_token)
        reservation_key = cfg.key("reservation", token)
        seconds, _ = first._foundation.server_time()
        expired = seconds - 1
        client_digest = hashlib.sha256(b"client\0client").hexdigest()
        request_digest = hashlib.sha256(b"request\0first").hexdigest()
        request_key = cfg.key("request", client_digest, request_digest)
        first._foundation._client.hset(request_key, "deadline", repr(expired))
        first._foundation._client.hset(reservation_key, "deadline", repr(expired))
        first._foundation._client.zadd(
            cfg.key("requests:deadline"),
            {f"{client_digest}:{request_digest}": expired},
        )

        successor = second.select_and_reserve(
            "client", "second", "qwen3-8b-instruct", "8k-fast", deadline
        )
        selections.append(("client", "second", successor))
        assert created.selected_node_id == "node-b"
        assert successor.selected_node_id == "node-a"
        assert first._foundation._client.hget(cursor, "_activity") == b"2"
        assert not first._foundation._client.exists(reservation_key)
    finally:
        _delete_scheduler_policy_state(
            first, ("node-b", "node-a"),
            [("client", "failed", None), *selections],
        )
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("activities", "expected_evicted"),
    [((1, 2), "alpha"), ((1, 1), "alpha")],
)
def test_cursor_fingerprint_index_preserves_additive_fields_and_evicts_deterministically(
    valkey_server, activities, expected_evicted
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=2
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    fingerprints = {name: _digest(name) for name in ("alpha", "beta")}
    deadline = time.time() + 30
    selected = None
    try:
        store.register("node", _capabilities(), _digest("owner"))
        store._foundation._client.hset(
            cfg.key("node", node_digest),
            "supported_model_ids",
            '["qwen3-8b-instruct","new-model"]',
        )
        additive = {f"additive:{index}": f"value-{index}" for index in range(1_000)}
        metadata = {"_count": 2, "_activity": 2, "_fp:1": fingerprints["alpha"],
                    "_fp:2": fingerprints["beta"], **additive}
        for index, name in enumerate(("alpha", "beta")):
            metadata[fingerprints[name]] = node_digest
            metadata["a:" + fingerprints[name]] = activities[index]
        store._foundation._client.hset(cursor, mapping=metadata)

        selected = store.select_and_reserve(
            "client", "request", "new-model", "8k-fast", deadline
        )
        new_fingerprint = _digest("new-model\x008k-fast")
        evicted = fingerprints[expected_evicted]
        assert store._foundation._client.hmget(cursor, *additive) == [
            value.encode() for value in additive.values()
        ]
        assert store._foundation._client.hget(cursor, evicted) is None
        assert store._foundation._client.hget(cursor, "a:" + evicted) is None
        assert new_fingerprint.encode() in store._foundation._client.hmget(
            cursor, "_fp:1", "_fp:2"
        )
    finally:
        token = (_digest(selected.reservation_token) if selected and
                 selected.reservation_token else _digest("unused"))
        client, request = store._identity("client", "request")
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
            cfg.key("reservation", token),
        )
        store.close()


def test_cursor_fingerprint_index_protects_active_fingerprints(valkey_server):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=2
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    active, inactive = _digest("active"), _digest("inactive")
    active_client, active_request = _digest("active-client"), _digest("active-request")
    active_key = cfg.key("request", active_client, active_request)
    deadline = time.time() + 30
    selected = None
    try:
        store.register("node", _capabilities(), _digest("owner"))
        store._foundation._client.hset(
            cfg.key("node", node_digest), "supported_model_ids",
            '["qwen3-8b-instruct","new-model"]',
        )
        store._foundation._client.hset(cursor, mapping={
            "_count": 2, "_activity": 2, "_fp:1": active, "_fp:2": inactive,
            active: node_digest, "a:" + active: 1,
            inactive: node_digest, "a:" + inactive: 2,
        })
        store._foundation._client.hset(
            active_key,
            mapping={"state": "reserved", "node_digest": node_digest,
                     "fingerprint": active},
        )
        store._foundation._client.zadd(
            cfg.key("requests:deadline"),
            {active_client + ":" + active_request: deadline},
        )

        selected = store.select_and_reserve(
            "client", "request", "new-model", "8k-fast", deadline
        )
        assert store._foundation._client.hget(cursor, active) == node_digest.encode()
        assert store._foundation._client.hget(cursor, inactive) is None
    finally:
        token = (_digest(selected.reservation_token) if selected and
                 selected.reservation_token else _digest("unused"))
        client, request = store._identity("client", "request")
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), active_key,
            cfg.key("request", client, request), cfg.key("reservation", token),
        )
        store.close()


@pytest.mark.parametrize("corruption", ["count", "duplicate", "mapping", "activity"])
def test_cursor_fingerprint_index_malformed_metadata_fails_without_mutation(
    valkey_server, corruption
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=2
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    first, second = _digest("first"), _digest("second")
    deadline = time.time() + 30
    try:
        store.register("node", _capabilities(), _digest("owner"))
        metadata = {"_count": 2, "_activity": 2, "_fp:1": first,
                    "_fp:2": second, first: node_digest, second: node_digest,
                    "a:" + first: 1, "a:" + second: 2, "additive": "kept"}
        if corruption == "count": metadata["_count"] = 1
        if corruption == "duplicate": metadata["_fp:2"] = first
        if corruption == "mapping": del metadata[first]
        if corruption == "activity": metadata["a:" + first] = "bad"
        store._foundation._client.hset(cursor, mapping=metadata)
        before = store._foundation._client.hgetall(cursor)
        client, request = store._identity("client", "request")

        with pytest.raises(ValkeySchemaIncompatibleError):
            store.select_and_reserve(
                "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
            )
        assert store._foundation._client.hgetall(cursor) == before
        assert not store._foundation._client.exists(cfg.key("request", client, request))
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert store._foundation._client.zcard(cfg.key("requests:deadline")) == 0
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest),
        )
        store.close()


@pytest.mark.parametrize(
    "activity",
    [None, "1", "02", "9223372036854775807"],
    ids=["missing", "regressed", "noncanonical", "nonincrementable"],
)
def test_cursor_fingerprint_index_rejects_invalid_global_activity_without_mutation(
    valkey_server, monkeypatch, activity
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=3
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    first, second = _digest("first"), _digest("second")
    client, request = store._identity("client", "request")
    raw_token = "a" * 64
    token = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    deadline = time.time() + 30
    try:
        store.register("node", _capabilities(), _digest("owner"))
        metadata = {
            "_count": 2,
            "_fp:1": first,
            "_fp:2": second,
            first: node_digest,
            second: node_digest,
            "a:" + first: 1,
            "a:" + second: 2,
            "additive": "kept",
        }
        if activity is not None:
            metadata["_activity"] = activity
        store._foundation._client.hset(cursor, mapping=metadata)
        before = store._foundation._client.hgetall(cursor)
        monkeypatch.setattr("valkey_relay_state.secrets.token_hex", lambda _: raw_token)

        with pytest.raises(ValkeySchemaIncompatibleError):
            store.select_and_reserve(
                "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
            )

        assert store._foundation._client.hgetall(cursor) == before
        assert not store._foundation._client.exists(
            cfg.key("request", client, request), cfg.key("reservation", token)
        )
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert store._foundation._client.zcard(cfg.key("requests:deadline")) == 0
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
            cfg.key("reservation", token),
        )
        store.close()


def test_cursor_fingerprint_index_activity_advances_monotonically(valkey_server):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=3
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    first, second = _digest("first"), _digest("second")
    new_fingerprint = _digest("qwen3-8b-instruct\x008k-fast")
    selected = None
    try:
        store.register("node", _capabilities(), _digest("owner"))
        store._foundation._client.hset(cursor, mapping={
            "_count": 2, "_activity": 9, "_fp:1": first, "_fp:2": second,
            first: node_digest, second: node_digest,
            "a:" + first: 4, "a:" + second: 9,
        })
        selected = store.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", time.time() + 30
        )
        assert store._foundation._client.hmget(
            cursor, "_activity", "a:" + new_fingerprint
        ) == [b"10", b"10"]
    finally:
        client, request = store._identity("client", "request")
        token = _digest(selected.reservation_token) if selected else _digest("unused")
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
            cfg.key("reservation", token),
        )
        store.close()


def test_cursor_fingerprint_index_empty_cursor_initializes_activity(valkey_server):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=2
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    selected = None
    try:
        store.register("node", _capabilities(), _digest("owner"))
        selected = store.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", time.time() + 30
        )
        assert store._foundation._client.hmget(cursor, "_count", "_activity") == [
            b"1", b"1"
        ]
    finally:
        client, request = store._identity("client", "request")
        token = _digest(selected.reservation_token) if selected else _digest("unused")
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
            cfg.key("reservation", token),
        )
        store.close()


@pytest.mark.parametrize("count", ["00", "01", "02"])
def test_cursor_fingerprint_index_rejects_noncanonical_count_without_mutation(
    valkey_server, monkeypatch, count
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=3
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    client, request = store._identity("client", "request")
    raw_token = "a" * 64
    token = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    try:
        store.register("node", _capabilities(), _digest("owner"))
        metadata = {"_count": count, "_activity": "2", "additive": "kept"}
        if count != "00":
            first = _digest("first")
            metadata.update({"_fp:1": first, first: node_digest, "a:" + first: "1"})
        if count == "02":
            second = _digest("second")
            metadata.update({"_fp:2": second, second: node_digest, "a:" + second: "2"})
        store._foundation._client.hset(cursor, mapping=metadata)
        before = store._foundation._client.hgetall(cursor)
        monkeypatch.setattr("valkey_relay_state.secrets.token_hex", lambda _: raw_token)

        with pytest.raises(ValkeySchemaIncompatibleError):
            store.select_and_reserve(
                "client", "request", "qwen3-8b-instruct", "8k-fast", time.time() + 30
            )

        assert store._foundation._client.hgetall(cursor) == before
        assert not store._foundation._client.exists(
            cfg.key("request", client, request), cfg.key("reservation", token)
        )
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert store._foundation._client.zcard(cfg.key("requests:deadline")) == 0
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
            cfg.key("reservation", token),
        )
        store.close()


@pytest.mark.parametrize("count", [None, "0"], ids=["absent", "zero"])
def test_cursor_fingerprint_index_rejects_empty_count_with_positive_activity(
    valkey_server, count
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=2
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    client, request = store._identity("client", "request")
    try:
        store.register("node", _capabilities(), _digest("owner"))
        metadata = {"_activity": "1", "additive": "kept"}
        if count is not None:
            metadata["_count"] = count
        store._foundation._client.hset(cursor, mapping=metadata)
        before = store._foundation._client.hgetall(cursor)

        with pytest.raises(ValkeySchemaIncompatibleError):
            store.select_and_reserve(
                "client", "request", "qwen3-8b-instruct", "8k-fast", time.time() + 30
            )

        assert store._foundation._client.hgetall(cursor) == before
        assert not store._foundation._client.exists(cfg.key("request", client, request))
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert store._foundation._client.zcard(cfg.key("requests:deadline")) == 0
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
        )
        store.close()


def test_cursor_fingerprint_index_explicit_zero_count_initializes(valkey_server):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_scheduler_fingerprints=2
    )
    cfg = store._foundation.config
    cursor = cfg.key("cursor")
    node_digest = store._node_digest("node")
    selected = None
    try:
        store.register("node", _capabilities(), _digest("owner"))
        store._foundation._client.hset(
            cursor, mapping={"_count": "0", "_activity": "0", "additive": "kept"}
        )
        selected = store.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", time.time() + 30
        )
        assert store._foundation._client.hmget(
            cursor, "_count", "_activity", "additive"
        ) == [b"1", b"1", b"kept"]
    finally:
        client, request = store._identity("client", "request")
        token = _digest(selected.reservation_token) if selected else _digest("unused")
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cursor,
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node_digest), cfg.key("request", client, request),
            cfg.key("reservation", token),
        )
        store.close()


@pytest.mark.parametrize("state", ["queued", "claimed"])
@pytest.mark.parametrize("corruption", ["missing", "wrong_identity"])
def test_idempotent_retries_require_exact_stream_authority(
    valkey_server, state, corruption
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    owner = _digest("owner")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    deadline = time.time() + 30
    selected = None
    cfg = first._foundation.config
    client, request = first._identity("client", "request")
    node = first._node_digest("node")
    request_key = cfg.key("request", client, request)
    queue_key = cfg.key("queue", node)
    try:
        first.register("node", _capabilities(), owner)
        selected = first.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
        )
        queued = first.enqueue_encrypted_request(
            "client", "request", selected.reservation_token, "node",
            "qwen3-8b-instruct", "8k-fast", deadline, envelope, "cancel",
        )
        entry = first._foundation._client.hget(request_key, "queue_entry")
        assert entry is not None
        if state == "claimed":
            first._foundation._client.hset(request_key, "state", "claimed")

        cursor_before = first._foundation._client.hgetall(cfg.key("cursor"))
        node_before = first._foundation._client.hgetall(cfg.key("node", node))
        assert not second.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
        ).created
        assert not second.enqueue_encrypted_request(
            "client", "request", selected.reservation_token, "node",
            "qwen3-8b-instruct", "8k-fast", deadline, envelope, "cancel",
        ).created
        assert first._foundation._client.xlen(queue_key) == 1
        assert queued.sequence == 1

        if corruption == "missing":
            assert first._foundation._client.xdel(queue_key, entry) == 1
        else:
            wrong = first._foundation._client.xadd(
                queue_key, {"client": _digest("wrong"), "request": request}
            )
            first._foundation._client.hset(request_key, "queue_entry", wrong)
        lifecycle_before = first._foundation._client.hgetall(request_key)

        with pytest.raises(ValkeySchemaIncompatibleError):
            second.select_and_reserve(
                "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
            )
        with pytest.raises(ValkeySchemaIncompatibleError):
            second.enqueue_encrypted_request(
                "client", "request", selected.reservation_token, "node",
                "qwen3-8b-instruct", "8k-fast", deadline, envelope, "cancel",
            )
        assert first._foundation._client.hgetall(request_key) == lifecycle_before
        assert first._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
        assert first._foundation._client.hgetall(cfg.key("node", node)) == node_before
    finally:
        token = (
            hashlib.sha256(selected.reservation_token.encode("ascii")).hexdigest()
            if selected is not None and selected.reservation_token is not None
            else "unused"
        )
        first._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node), queue_key, request_key,
            cfg.key("reservation", token),
        )
        first.close()
        second.close()


def test_registration_renew_backfills_additive_scheduler_fields(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    owner = _digest("owner")
    node_key = store._foundation.config.key("node", store._node_digest("node"))
    try:
        store.register("node", _capabilities(), owner)
        store._foundation._client.hdel(
            node_key,
            "scheduler_healthy",
            "scheduler_draining",
            "scheduler_claimed_work",
        )

        store.renew("node", owner)

        assert store._foundation._client.hmget(
            node_key,
            "scheduler_healthy",
            "scheduler_draining",
            "scheduler_claimed_work",
        ) == [b"1", b"0", b"0"]
        assert store.set_scheduler_state(
            "node", owner, SchedulerNodeState(healthy=True, claimed_work=0)
        )
    finally:
        store._foundation._client.delete(
            store._foundation.config.key("schema"),
            store._foundation.config.key("nodes:lease"),
            store._foundation.config.key("cursor"),
            node_key,
        )
        store.close()


def test_enqueue_counts_queued_requests_hidden_by_earlier_reservations(valkey_server):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        max_queued_requests=1,
        max_queued_requests_per_client=2,
    )
    owner = _digest("owner")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    queued = None
    reserved = None
    cfg = store._foundation.config
    node = store._node_digest("node")
    queued_client, queued_request = store._identity("client-a", "queued")
    reserved_client, reserved_request = store._identity("client-b", "reserved")
    try:
        store.register("node", _capabilities(concurrency=3), owner)
        late_deadline = time.time() + 60
        queued = store.select_and_reserve(
            "client-a", "queued", "qwen3-8b-instruct", "8k-fast", late_deadline
        )
        store.enqueue_encrypted_request(
            "client-a",
            "queued",
            queued.reservation_token,
            "node",
            "qwen3-8b-instruct",
            "8k-fast",
            late_deadline,
            envelope,
            "cancel-a",
        )
        early_deadline = time.time() + 30
        reserved = store.select_and_reserve(
            "client-b", "reserved", "qwen3-8b-instruct", "8k-fast", early_deadline
        )

        with pytest.raises(RelayStateNoCapacity, match="^no scheduler capacity$"):
            store.enqueue_encrypted_request(
                "client-b",
                "reserved",
                reserved.reservation_token,
                "node",
                "qwen3-8b-instruct",
                "8k-fast",
                early_deadline,
                envelope,
                "cancel-b",
            )
    finally:
        reservation_keys = []
        for selection in (queued, reserved):
            if selection is not None and selection.reservation_token is not None:
                token = hashlib.sha256(
                    selection.reservation_token.encode("ascii")
                ).hexdigest()
                reservation_keys.append(cfg.key("reservation", token))
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", queued_client, queued_request),
            cfg.key("request", reserved_client, reserved_request),
            *reservation_keys,
        )
        store.close()


def test_claimed_lifecycle_counts_toward_all_queue_capacity(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        max_queue_depth_per_node=1,
        max_queued_requests=1,
        max_queued_requests_per_client=1,
        max_reservations_per_client=1,
    )
    owner = _digest("owner")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    deadline = time.time() + 60
    first = None
    second = None
    cfg = store._foundation.config
    client = hashlib.sha256(b"client\0client").hexdigest()
    request = hashlib.sha256(b"request\0request").hexdigest()
    other_client = hashlib.sha256(b"other\0other").hexdigest()
    other_request = hashlib.sha256(b"request\0second").hexdigest()
    node = store._node_digest("node")
    other_node = store._node_digest("other-node")
    try:
        store.register("node", _capabilities(concurrency=2), owner)
        store.register("other-node", _capabilities(concurrency=2), owner)
        first = store.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
        )
        store.enqueue_encrypted_request(
            "client",
            "request",
            first.reservation_token,
            "node",
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            envelope,
            "cancel",
        )
        request_key = cfg.key("request", client, request)
        store._foundation._client.hset(request_key, "state", "claimed")
        cursor_before = store._foundation._client.hgetall(cfg.key("cursor"))

        retry = store.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
        )
        assert not retry.created and retry.state == "claimed"
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
        with pytest.raises(RelayStateNoCapacity, match="^no scheduler capacity$"):
            store.select_and_reserve(
                "client", "second", "qwen3-8b-instruct", "8k-fast", deadline
            )

        second = store.select_and_reserve(
            "other", "second", "qwen3-8b-instruct", "8k-fast", deadline
        )
        assert second.selected_node_id == "other-node"
        with pytest.raises(RelayStateNoCapacity, match="^no scheduler capacity$"):
            store.enqueue_encrypted_request(
                "other",
                "second",
                second.reservation_token,
                "other-node",
                "qwen3-8b-instruct",
                "8k-fast",
                deadline,
                envelope,
                "other-cancel",
            )
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("node", node),
            cfg.key("node", other_node),
            cfg.key("queue", node),
            cfg.key("queue", other_node),
            cfg.key("request", client, request),
            cfg.key("request", client, other_request),
            cfg.key("request", other_client, other_request),
        ]
        if first is not None and first.reservation_token:
            keys.append(cfg.key("reservation", _digest(first.reservation_token)))
        if second is not None and second.reservation_token:
            keys.append(cfg.key("reservation", _digest(second.reservation_token)))
        store._foundation._client.delete(*keys)
        store.close()


def test_queued_requests_filters_claimed_and_expired_but_rejects_malformed_live(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    owner = _digest("owner")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    deadline = time.time() + 60
    selected = None
    cfg = store._foundation.config
    client = hashlib.sha256(b"client\0client").hexdigest()
    request = hashlib.sha256(b"request\0request").hexdigest()
    node = store._node_digest("node")
    request_key = cfg.key("request", client, request)
    try:
        store.register("node", _capabilities(), owner)
        selected = store.select_and_reserve(
            "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
        )
        store.enqueue_encrypted_request(
            "client", "request", selected.reservation_token, "node",
            "qwen3-8b-instruct", "8k-fast", deadline, envelope, "cancel"
        )
        store._foundation._client.hset(request_key, "state", "claimed")
        assert store.queued_requests("node") == ()
        store._foundation._client.hset(
            request_key, mapping={"state": "queued", "deadline": time.time() - 1}
        )
        assert store.queued_requests("node") == ()
        store._foundation._client.hset(
            request_key, mapping={"deadline": time.time() + 60, "sequence": "bad"}
        )
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.queued_requests("node")
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node), cfg.key("queue", node), request_key,
        )
        store.close()


def test_missing_indexed_lifecycle_fails_closed_without_mutation(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    owner = _digest("owner")
    cfg = store._foundation.config
    node = store._node_digest("node")
    missing = "a" * 64 + ":" + "b" * 64
    deadline = time.time() + 60
    try:
        store.register("node", _capabilities(), owner)
        store._foundation._client.zadd(cfg.key("requests:deadline"), {missing: deadline})
        before = store._foundation._client.hgetall(cfg.key("cursor"))
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.select_and_reserve(
                "client", "request", "qwen3-8b-instruct", "8k-fast", deadline
            )
        assert store._foundation._client.hgetall(cfg.key("cursor")) == before
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node),
        )
        store.close()


@pytest.mark.parametrize("state", ["queued", "claimed"])
def test_expired_queue_reclaims_exact_stream_and_lifecycle_capacity(
    valkey_server, state
):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        reservation_ttl_seconds=0.03,
        node_transition_batch_size=1,
        max_queued_requests=1,
        max_queued_requests_per_client=1,
        max_queue_depth_per_node=4,
        max_reservations=4,
        max_reservations_per_client=4,
        max_reservations_per_node=4,
    )
    cfg = store._foundation.config
    owner = _digest("owner")
    node = store._node_digest("node")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    identities = []
    try:
        store.register("node", _capabilities(concurrency=4), owner)
        seconds, micros = store._foundation.server_time()
        queued_deadline = seconds + micros / 1_000_000 + 30
        queued = store.select_and_reserve(
            "queued-client", "queued-request", "qwen3-8b-instruct", "8k-fast",
            queued_deadline,
        )
        store.enqueue_encrypted_request(
            "queued-client", "queued-request", queued.reservation_token, "node",
            "qwen3-8b-instruct", "8k-fast", queued_deadline, envelope, "cancel",
        )
        identities.append(("queued-client", "queued-request"))
        client = hashlib.sha256(b"client\0queued-client").hexdigest()
        request = hashlib.sha256(b"request\0queued-request").hexdigest()
        request_key = cfg.key("request", client, request)
        expired_deadline = seconds + micros / 1_000_000 - 1
        store._foundation._client.hset(
            request_key, mapping={"state": state, "deadline": expired_deadline}
        )
        store._foundation._client.zadd(
            cfg.key("requests:deadline"), {client + ":" + request: expired_deadline}
        )
        cursor_before = store._foundation._client.hgetall(cfg.key("cursor"))

        with pytest.raises(RelayStateInvalidReservation):
            store.select_and_reserve(
                "queued-client", "queued-request", "qwen3-8b-instruct",
                "8k-fast", expired_deadline,
            )

        assert store._foundation._client.xlen(cfg.key("queue", node)) == 0
        assert not store._foundation._client.exists(request_key)
        assert store._foundation._client.zscore(
            cfg.key("requests:deadline"), client + ":" + request
        ) is None
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0

    finally:
        keys = [
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node), cfg.key("queue", node),
        ]
        for client_public_key, request_id in identities:
            keys.append(
                cfg.key(
                    "request",
                    hashlib.sha256(f"client\0{client_public_key}".encode()).hexdigest(),
                    hashlib.sha256(f"request\0{request_id}".encode()).hexdigest(),
                )
            )
        store._foundation._client.delete(*keys)
        store.close()


def test_malformed_expired_queue_authority_blocks_admission_without_mutation(
    valkey_server,
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner = _digest("owner")
    node = store._node_digest("node")
    client = hashlib.sha256(b"client\0expired").hexdigest()
    request = hashlib.sha256(b"request\0malformed").hexdigest()
    request_key = cfg.key("request", client, request)
    member = client + ":" + request
    try:
        store.register("node", _capabilities(), owner)
        store._foundation._client.hset(
            request_key,
            mapping={
                "state": "queued", "client": client, "request": request,
                "node_digest": node, "deadline": time.time() - 1,
                "queue_entry": "missing-0",
            },
        )
        store._foundation._client.zadd(
            cfg.key("requests:deadline"), {member: time.time() - 1}
        )
        cursor_before = store._foundation._client.hgetall(cfg.key("cursor"))
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.select_and_reserve(
                "new", "request", "qwen3-8b-instruct", "8k-fast",
                time.time() + 30,
            )
        assert store._foundation._client.exists(request_key)
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
    finally:
        store._foundation._client.delete(
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node), cfg.key("queue", node), request_key,
        )
        store.close()


def test_expired_addressed_reservation_is_reclaimed_beyond_cleanup_batch(
    valkey_server,
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, node_transition_batch_size=1,
        max_reservations=4, max_reservations_per_client=4,
        max_reservations_per_node=4,
    )
    cfg = store._foundation.config
    owner = _digest("owner")
    node = store._node_digest("node")
    records = []
    try:
        store.register("node", _capabilities(concurrency=4), owner)
        seconds, micros = store._foundation.server_time()
        future = seconds + micros / 1_000_000 + 30
        for index in range(3):
            client_name, request_name = f"client-{index}", f"request-{index}"
            result = store.select_and_reserve(
                client_name, request_name, "qwen3-8b-instruct", "8k-fast", future
            )
            client = hashlib.sha256(f"client\0{client_name}".encode()).hexdigest()
            request = hashlib.sha256(f"request\0{request_name}".encode()).hexdigest()
            token = _digest(result.reservation_token)
            records.append((client_name, request_name, client, request, token))

        expired = seconds + micros / 1_000_000 - 1
        for offset, (_, _, client, request, token) in enumerate(records):
            score = expired - (len(records) - offset)
            store._foundation._client.hset(
                cfg.key("request", client, request),
                mapping={"deadline": score, "reservation_expires": score},
            )
            store._foundation._client.hset(
                cfg.key("reservation", token),
                mapping={"deadline": score, "reservation_expires": score},
            )
            store._foundation._client.zadd(
                cfg.key("requests:deadline"), {client + ":" + request: score}
            )
            store._foundation._client.zadd(
                cfg.key("reservations:expiry"), {token: score}
            )

        cursor_before = store._foundation._client.hgetall(cfg.key("cursor"))
        target = records[-1]
        target_deadline = expired - 1
        with pytest.raises(RelayStateInvalidReservation):
            store.select_and_reserve(
                target[0], target[1], "qwen3-8b-instruct", "8k-fast",
                target_deadline,
            )

        assert not store._foundation._client.exists(
            cfg.key("request", target[2], target[3]),
            cfg.key("reservation", target[4]),
        )
        assert store._foundation._client.zcard(cfg.key("requests:deadline")) == 1
        assert store._foundation._client.zcard(cfg.key("reservations:expiry")) == 1
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
    finally:
        keys = [
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("node", node), cfg.key("queue", node),
        ]
        for _, _, client, request, token in records:
            keys.extend(
                [cfg.key("request", client, request), cfg.key("reservation", token)]
            )
        store._foundation._client.delete(*keys)
        store.close()


def _mark_registrations_due(store, node_ids):
    seconds, micros = store._foundation.server_time()
    cutoff = seconds + micros / 1_000_000
    lease_key = store._foundation.config.key("nodes:lease")
    with store._foundation._client.pipeline(transaction=True) as pipeline:
        for node_id in node_ids:
            digest = store._node_digest(node_id)
            node_key = store._foundation.config.key("node", digest)
            pipeline.hset(node_key, "lease_expires_at_epoch", cutoff)
            pipeline.zadd(lease_key, {digest: cutoff})
        pipeline.execute()


def test_shared_registration_only_backend_contract(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex, max_compute_nodes=1)
    try:
        assert_registration_contract(store, _capabilities(), _digest)
    finally:
        store._foundation._client.delete(*_registration_keys(store, "node-a", "node-b"))
        store.close()


@pytest.mark.parametrize("same_owner", [True, False])
def test_simultaneous_absent_node_registration_has_authoritative_result(
    valkey_server, same_owner
):
    namespace = uuid.uuid4().hex
    stores = [_registration_store(valkey_server, namespace) for _ in range(2)]
    owners = [_digest("owner"), _digest("owner" if same_owner else "rival")]
    barrier = Barrier(2)

    def register(index):
        barrier.wait()
        try:
            return stores[index].register("node-a", _capabilities(), owners[index])
        except RelayStateCredentialMismatch:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, range(2)))
        assert sum(result is not None for result in results) == (2 if same_owner else 1)
        committed = stores[0].get("node-a")
        assert committed is not None
        if same_owner:
            assert all(
                result is not None
                and result.registered_at_epoch == committed.registered_at_epoch
                and result.control_credential_digest
                == committed.control_credential_digest
                for result in results
            )
        else:
            assert next(result for result in results if result is not None) == committed
    finally:
        stores[0]._foundation._client.delete(*_registration_keys(stores[0], "node-a"))
        for store in stores:
            store.close()


def test_distinct_nodes_race_for_final_capacity_slot(valkey_server):
    namespace = uuid.uuid4().hex
    stores = [
        _registration_store(valkey_server, namespace, max_compute_nodes=1)
        for _ in range(2)
    ]
    barrier = Barrier(2)

    def register(index):
        barrier.wait()
        try:
            return stores[index].register(
                f"node-{index}", _capabilities(), _digest(f"owner-{index}")
            )
        except RelayStateCapacityExceeded:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, range(2)))
        assert sum(result is not None for result in results) == 1
        assert len(stores[0].list()) == 1
    finally:
        stores[0]._foundation._client.delete(
            *_registration_keys(stores[0], "node-0", "node-1")
        )
        for store in stores:
            store.close()


def test_reads_are_read_only_and_unknown_fields_survive_updates(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    owner = _digest("owner")
    node_key = store._foundation.config.key("node", store._node_digest("node-a"))
    try:
        store.register("node-a", _capabilities(), owner)
        future_value = b"future-marker:" + b"x" * 100_000
        store._foundation._client.hset(node_key, "future_field", future_value)
        assert store.get("node-a") is not None
        assert len(store.list()) == 1
        assert store.renew("node-a", owner) is not None
        assert store._foundation._client.hget(node_key, "future_field") == future_value
        incompatible = _manifest(writer_min=2, writer_max=2, active_writer_revision=2)
        store._foundation._client.set(
            store._foundation.config.key("schema"), incompatible.encode()
        )
        assert store.get("node-a") is not None
        assert len(store.list()) == 1
        for mutation in (
            lambda: store.register("node-b", _capabilities(), _digest("other")),
            lambda: store.renew("node-a", owner),
            lambda: store.unregister("node-a", owner),
            store.expire,
        ):
            with pytest.raises(ValkeySchemaIncompatibleError):
                mutation()
        assert store._foundation._client.hget(node_key, "future_field") == future_value
    finally:
        store._foundation._client.delete(*_registration_keys(store, "node-a", "node-b"))
        store.close()


def test_registration_namespace_isolation(valkey_server):
    stores = [_registration_store(valkey_server, uuid.uuid4().hex) for _ in range(2)]
    node_id = "shared-node"
    owners = (_digest("owner-a"), _digest("owner-b"))
    capabilities = (_capabilities(2), _capabilities(7))
    try:
        records = [
            store.register(node_id, capability, owner)
            for store, capability, owner in zip(stores, capabilities, owners)
        ]
        assert [store.get(node_id) for store in stores] == records
        assert [store.list() for store in stores] == [(records[0],), (records[1],)]

        renewed = stores[0].renew(node_id, owners[0], capabilities=_capabilities(3))
        assert renewed is not None and renewed.capabilities.max_concurrency == 3
        assert stores[1].get(node_id) == records[1]
        assert stores[0].unregister(node_id, owners[0])
        assert stores[0].get(node_id) is None
        assert stores[1].get(node_id) == records[1]
        assert stores[1].renew(node_id, owners[1]) is not None
        assert stores[1].unregister(node_id, owners[1])
        assert stores[0].list() == () and stores[1].list() == ()
    finally:
        for store in stores:
            store._foundation._client.delete(*_registration_keys(store, node_id))
            store.close()


@pytest.mark.parametrize("operation", ["get", "list"])
def test_reader_incompatible_registration_reads_stop_before_state_access(
    valkey_server, operation
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    schema_key, lease_key, cursor_key, node_key = _registration_keys(store, "node-a")
    incompatible = _manifest(reader_min=2, reader_max=2)
    malformed_hash = {b"node_id": b"malformed-application-payload"}
    malformed_member = b"malformed-index-member"
    try:
        store._foundation._client.hset(node_key, mapping=malformed_hash)
        store._foundation._client.zadd(lease_key, {malformed_member: 123.0})
        store._foundation._client.set(schema_key, incompatible.encode())
        before = (
            store._foundation._client.get(schema_key),
            store._foundation._client.hgetall(node_key),
            store._foundation._client.zrange(lease_key, 0, -1, withscores=True),
        )
        original_call = store._foundation._call
        commands = []

        def record_command(command, *args, **kwargs):
            commands.append(command.__name__.lower())
            return original_call(command, *args, **kwargs)

        store._foundation._call = record_command
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ) as caught:
            getattr(store, operation)("node-a") if operation == "get" else store.list()
        assert str(caught.value) == "state schema incompatible"
        assert not {"zscore", "zrangebyscore", "hmget", "evalsha"}.intersection(
            commands
        )
        assert before == (
            store._foundation._client.get(schema_key),
            store._foundation._client.hgetall(node_key),
            store._foundation._client.zrange(lease_key, 0, -1, withscores=True),
        )
    finally:
        store._foundation._client.delete(schema_key, lease_key, cursor_key, node_key)
        store.close()


def test_registration_persistence_uses_only_approved_redacted_values(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node_id = "node-persistence"
    raw_credential = "raw-control-credential-marker"
    owner = _digest(raw_credential)
    node_digest = store._node_digest(node_id)
    schema_key, lease_key, cursor_key, node_key = _registration_keys(store, node_id)
    expected_fields = {
        b"node_id",
        b"supported_model_ids",
        b"active_context_tier",
        b"maximum_total_context_tokens",
        b"default_output_token_reservation",
        b"maximum_output_tokens",
        b"max_concurrency",
        b"backend_class",
        b"api_version",
        b"control_credential_digest",
        b"registered_at_epoch",
        b"lease_expires_at_epoch",
        b"scheduler_healthy",
        b"scheduler_draining",
        b"scheduler_claimed_work",
        b"registration_order",
    }
    forbidden = (
        raw_credential.encode(),
        b"application-payload-marker",
        b"private-endpoint-marker",
        schema_key.encode(),
        lease_key.encode(),
        node_key.encode(),
    )
    try:
        store.register(node_id, _capabilities(), owner)
        assert store._foundation._client.zrange(lease_key, 0, -1) == [
            node_digest.encode()
        ]
        persisted = store._foundation._client.hgetall(node_key)
        assert set(persisted) == expected_fields
        assert persisted[b"control_credential_digest"] == owner.encode()
        assert persisted[b"supported_model_ids"] == b'["qwen3-8b-instruct"]'
        assert persisted[b"max_concurrency"] == b"2"
        assert persisted[b"maximum_total_context_tokens"] == b"8192"
        assert all(
            marker not in value for marker in forbidden for value in persisted.values()
        )
    finally:
        store._foundation._client.delete(schema_key, lease_key, cursor_key, node_key)
        store.close()


def test_list_tolerates_concurrent_unregister(valkey_server):
    namespace = uuid.uuid4().hex
    stores = [_registration_store(valkey_server, namespace) for _ in range(2)]
    owner = _digest("owner")
    original_call = stores[0]._foundation._call
    intercepted = False
    try:
        stores[0].register("node-a", _capabilities(), owner)

        def unregister_before_hash_read(command, *args, **kwargs):
            nonlocal intercepted
            if command.__name__ == "hmget":
                intercepted = True
                assert stores[1].unregister("node-a", owner)
            return original_call(command, *args, **kwargs)

        stores[0]._foundation._call = unregister_before_hash_read
        assert stores[0].list() == ()
        assert intercepted
        assert stores[1].get("node-a") is None
    finally:
        stores[0]._foundation._client.delete(*_registration_keys(stores[0], "node-a"))
        for store in stores:
            store.close()


def test_large_finite_ttl_uses_a_finite_epoch_seconds_deadline(valkey_server):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, lease_ttl_seconds=1e308
    )
    try:
        record = store.register("node-a", _capabilities(), _digest("owner"))
        assert math.isfinite(record.lease_expires_at_epoch)
        assert record.lease_expires_at_epoch == 1e308
    finally:
        store._foundation._client.delete(*_registration_keys(store, "node-a"))
        store.close()


def test_maximum_utf8_capabilities_round_trip_without_expiry_timing(valkey_server):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        lease_ttl_seconds=3600,
    )
    owner = _digest("owner")
    non_bmp_models = tuple(chr(0x10000 + index) * 128 for index in range(64))
    capabilities = dataclasses.replace(
        _capabilities(), supported_model_ids=non_bmp_models
    )
    try:
        registered = store.register("node-a", capabilities, owner)
        assert registered.capabilities == capabilities
        renewed = store.renew("node-a", owner, capabilities=capabilities)
        assert renewed is not None and renewed.capabilities == capabilities
        assert store.get("node-a") == renewed
    finally:
        store._foundation._client.delete(*_registration_keys(store, "node-a"))
        store.close()


def test_default_reap_batch_returns_64_small_records(valkey_server):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        max_compute_nodes=64,
        lease_ttl_seconds=3600,
    )
    owner = _digest("owner")
    node_ids = tuple(f"node-{index}" for index in range(64))
    try:
        for node_id in node_ids:
            store.register(node_id, _capabilities(), owner)

        _mark_registrations_due(store, node_ids)
        expired = store.expire()
        assert [record.node_id for record in expired] == sorted(node_ids)
        assert len({record.node_id for record in expired}) == 64
        assert store.expire() == ()
    finally:
        store._foundation._client.delete(*_registration_keys(store, *node_ids))
        store.close()


def test_maximum_utf8_reap_respects_byte_budget_across_batches(valkey_server):
    node_ids = ("node-a", "node-b", "node-c")
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        max_compute_nodes=len(node_ids),
        lease_ttl_seconds=3600,
    )
    owner = _digest("owner")
    non_bmp_models = tuple(chr(0x10000 + index) * 128 for index in range(64))
    capabilities = dataclasses.replace(
        _capabilities(), supported_model_ids=non_bmp_models
    )
    try:
        for node_id in node_ids:
            store.register(node_id, capabilities, owner)

        _mark_registrations_due(store, node_ids)
        expired_node_ids = []
        while batch := store.expire():
            expired_node_ids.extend(record.node_id for record in batch)

        assert sorted(expired_node_ids) == sorted(node_ids)
        assert len(set(expired_node_ids)) == len(node_ids)
        assert store.expire() == ()
    finally:
        store._foundation._client.delete(*_registration_keys(store, *node_ids))
        store.close()


def test_non_array_stored_capabilities_are_schema_incompatible(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node_key = store._foundation.config.key("node", store._node_digest("node-a"))
    try:
        store.register("node-a", _capabilities(), _digest("owner"))
        store._foundation._client.hset(node_key, "supported_model_ids", '"model"')
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="state schema incompatible"
        ):
            store.get("node-a")
    finally:
        store._foundation._client.delete(*_registration_keys(store, "node-a"))
        store.close()


def test_registration_contract_is_atomic_across_independent_clients(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, max_compute_nodes=2)
    second = _registration_store(valkey_server, namespace, max_compute_nodes=2)
    owner = _digest("owner")
    try:
        created = first.register("node-b", _capabilities(), owner)
        assert created == second.get("node-b")
        assert (
            second.register(
                "node-b", _capabilities(3), owner
            ).capabilities.max_concurrency
            == 3
        )
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
                return store.register(
                    "node-b", _capabilities(), credential
                ).control_credential_digest
            except RelayStateCredentialMismatch:
                return "rejected"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(compete, (first, second), (owner, _digest("rival")))
            )
        assert sorted(results) == sorted(["rejected", owner])
    finally:
        keys = [
            first._foundation.config.key("schema"),
            first._foundation.config.key("nodes:lease"),
        ]
        for node_id in ("node-a", "node-b", "node-c", "unknown"):
            keys.append(
                first._foundation.config.key("node", first._node_digest(node_id))
            )
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
        assert (
            abs(
                record.registered_at_epoch
                - (server_seconds + server_micros / 1_000_000)
            )
            < 1
        )

        def lease_elapsed():
            seconds, micros = store._foundation.server_time()
            return seconds + micros / 1_000_000 >= record.lease_expires_at_epoch

        _wait_until(lease_elapsed)
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


def test_registration_expiry_backlog_does_not_affect_liveness_or_capacity(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        max_compute_nodes=3,
        node_transition_batch_size=1,
        lease_ttl_seconds=0.02,
    )
    owner = _digest("owner")
    node_ids = ("node-a", "node-b", "node-c", "node-d")
    try:
        records = [
            store.register(node_id, _capabilities(), owner) for node_id in node_ids[:3]
        ]

        def leases_elapsed():
            seconds, micros = store._foundation.server_time()
            return seconds + micros / 1_000_000 >= max(
                record.lease_expires_at_epoch for record in records
            )

        _wait_until(leases_elapsed)

        # The addressed node is expired even when it falls outside bounded cleanup.
        assert store.get("node-c") is None
        assert store.renew("node-c", owner) is None
        rival = _digest("rival")
        assert (
            store.register("node-c", _capabilities(), rival).control_credential_digest
            == rival
        )
        # Unreaped expired scores do not consume live registration capacity.
        assert store.register("node-d", _capabilities(), owner).node_id == "node-d"
    finally:
        keys = [
            store._foundation.config.key("schema"),
            store._foundation.config.key("nodes:lease"),
            *(
                store._foundation.config.key("node", store._node_digest(node_id))
                for node_id in node_ids
            ),
        ]
        store._foundation._client.delete(*keys)
        store.close()


def test_expire_returns_the_records_removed_at_its_atomic_cutoff(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        max_compute_nodes=2,
        node_transition_batch_size=2,
        lease_ttl_seconds=0.02,
    )
    owner = _digest("owner")
    try:
        records = [
            store.register("node-a", _capabilities(), owner),
            store.register("node-b", _capabilities(), owner),
        ]

        def leases_elapsed():
            seconds, micros = store._foundation.server_time()
            return seconds + micros / 1_000_000 >= max(
                record.lease_expires_at_epoch for record in records
            )

        _wait_until(leases_elapsed)

        assert [record.node_id for record in store.expire()] == ["node-a", "node-b"]
        assert store.expire() == ()
    finally:
        keys = [
            store._foundation.config.key("schema"),
            store._foundation.config.key("nodes:lease"),
            store._foundation.config.key("node", store._node_digest("node-a")),
            store._foundation.config.key("node", store._node_digest("node-b")),
        ]
        store._foundation._client.delete(*keys)
        store.close()


def test_unregister_lost_reply_redaction_is_ambiguous_without_replay(
    valkey_server, caplog
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node_id = "node-identity-marker"
    raw_credential = "raw-credential-marker"
    owner = _digest(raw_credential)
    raw_key = store._foundation.config.key("node", store._node_digest(node_id))
    markers = (
        "private-endpoint-marker",
        "datastore-reply-marker",
        raw_key,
        node_id,
        raw_credential,
        owner,
    )
    original_evalsha = store._foundation._client.evalsha
    dispatches = 0
    try:
        store.register(node_id, _capabilities(), owner)

        def lose_reply(*args):
            nonlocal dispatches
            dispatches += 1
            original_evalsha(*args)
            raise redis.ConnectionError(" ".join(markers))

        store._foundation._client.evalsha = lose_reply
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(
                ValkeyUnavailableError, match="^state backend unavailable$"
            ) as caught:
                store.unregister(node_id, owner)
        assert dispatches == 1
        assert caught.value.__cause__ is None
        rendered = "".join(
            (
                str(caught.value),
                repr(caught.value),
                "".join(traceback.format_exception(caught.value)),
                caplog.text,
                repr(store),
                repr(store._foundation),
                repr(store._foundation.config),
                repr(store._foundation.config.direct),
            )
        )
        assert all(marker not in rendered for marker in markers)
        store._foundation._client.evalsha = original_evalsha
        assert store.get(node_id) is None
    finally:
        store._foundation._client.evalsha = original_evalsha
        store._foundation._client.delete(*_registration_keys(store, node_id))
        store.close()


def test_expire_lost_reply_does_not_consume_a_hidden_retry_batch(valkey_server):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        max_compute_nodes=2,
        node_transition_batch_size=1,
    )
    owner = _digest("owner")
    node_ids = ("node-a", "node-b")
    original_evalsha = store._foundation._client.evalsha
    dispatches = 0
    try:
        for node_id in node_ids:
            store.register(node_id, _capabilities(), owner)
        _mark_registrations_due(store, node_ids)

        def lose_reply(*args):
            nonlocal dispatches
            dispatches += 1
            original_evalsha(*args)
            raise redis.ConnectionError("lost reply from private endpoint")

        store._foundation._client.evalsha = lose_reply
        with pytest.raises(ValkeyUnavailableError, match="^state backend unavailable$"):
            store.expire()
        assert dispatches == 1

        store._foundation._client.evalsha = original_evalsha
        remaining = store.expire()
        assert len(remaining) == 1
        assert remaining[0].node_id in node_ids
        assert store.expire() == ()
    finally:
        store._foundation._client.evalsha = original_evalsha
        store._foundation._client.delete(*_registration_keys(store, *node_ids))
        store.close()
