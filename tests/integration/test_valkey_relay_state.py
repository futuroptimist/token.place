import concurrent.futures
import dataclasses
import hashlib
import hmac
import logging
import math
import shutil
import socket
import struct
import subprocess
import time
import traceback
import uuid
from threading import Barrier, Event

import pytest
import redis

from relay_state_store import (
    ComputeNodeCapabilities,
    EncryptedRequestEnvelope,
    EncryptedResponseEnvelope,
    InMemoryRelayStateStore,
    RelayStateCapacityExceeded,
    RelayStateConflict,
    RelayStateCredentialMismatch,
    RelayStateInvalidReservation,
    RelayStateNoCapacity,
    RelayStateStoreConfig,
    SchedulerNodeState,
)
from tests.registration_store_contract import assert_registration_contract
from valkey_relay_state import (
    ACCEPT_RESPONSE_SCRIPT,
    CLAIM_SCRIPT,
    RENEW_CLAIM_SCRIPT,
    RETRIEVE_RESPONSE_SCRIPT,
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

_ACKNOWLEDGEMENT_KEY = b"shared-test-acknowledgement-key-32"


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
        acknowledgement_key=_ACKNOWLEDGEMENT_KEY,
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


def _wait_for_server_epoch(store, boundary, timeout=2.0):
    """Wait for an inclusive lease boundary using only Valkey's clock."""

    def boundary_reached():
        seconds, micros = store._foundation.server_time()
        return seconds + micros / 1_000_000 >= boundary

    _wait_until(boundary_reached, timeout=timeout)

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


def test_scheduler_selection_policy_filters_context_in_memory_and_valkey(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    memory = InMemoryRelayStateStore(
        RelayStateStoreConfig(namespace="context-policy-memory"),
        acknowledgement_key=b"k" * 32,
    )
    stores = (memory, first)
    nodes = (
        ("too-small", _scheduler_policy_capabilities()),
        ("full-context", _scheduler_policy_capabilities(tier="64k-full")),
    )
    selection = None
    try:
        for store in stores:
            for node, capabilities in nodes:
                store.register(node, capabilities, _digest(node))

        deadline = first._foundation.server_time()[0] + 60
        memory_selection = memory.select_and_reserve(
            "memory", "full-context", "qwen3-8b-instruct", "64k-full", deadline
        )
        selection = second.select_and_reserve(
            "valkey", "full-context", "qwen3-8b-instruct", "64k-full", deadline
        )
        assert memory_selection.selected_node_id == "full-context"
        assert selection.selected_node_id == memory_selection.selected_node_id
    finally:
        _delete_scheduler_policy_state(
            first,
            [node for node, _ in nodes],
            (("valkey", "full-context", selection),),
        )
        first.close()
        second.close()


def test_scheduler_selection_policy_prefers_lower_load_in_memory_and_valkey(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    memory = InMemoryRelayStateStore(
        RelayStateStoreConfig(namespace="load-policy-memory"),
        acknowledgement_key=b"k" * 32,
    )
    stores = (memory, first)
    nodes = ("earlier-loaded", "later-idle")
    selection = None
    try:
        for store in stores:
            for node in nodes:
                store.register(node, _scheduler_policy_capabilities(), _digest(node))
            store.set_scheduler_state(
                "earlier-loaded",
                _digest("earlier-loaded"),
                SchedulerNodeState(claimed_work=2),
            )

        deadline = first._foundation.server_time()[0] + 60
        memory_selection = memory.select_and_reserve(
            "memory", "least-load", "qwen3-8b-instruct", "8k-fast", deadline
        )
        selection = second.select_and_reserve(
            "valkey", "least-load", "qwen3-8b-instruct", "8k-fast", deadline
        )
        assert memory_selection.selected_node_id == "later-idle"
        assert selection.selected_node_id == memory_selection.selected_node_id
    finally:
        _delete_scheduler_policy_state(
            first, nodes, (("valkey", "least-load", selection),)
        )
        first.close()
        second.close()


def _race_select_and_reserve(stores, requests, deadline):
    barrier = Barrier(len(requests))

    def select(store, request):
        barrier.wait(timeout=5)
        return store.select_and_reserve(
            "client", request, "qwen3-8b-instruct", "8k-fast", deadline
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futures = [
            pool.submit(select, store, request)
            for store, request in zip(stores, requests, strict=True)
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except RelayStateNoCapacity as error:
                outcomes.append(error)
    return outcomes


def test_selection_final_capacity_race_is_atomic_across_valkey_stores(valkey_server):
    namespace = uuid.uuid4().hex
    limits = dict(
        max_reservations=1,
        max_reservations_per_client=1,
        max_reservations_per_node=1,
        max_queue_depth_per_node=1,
        max_request_lifecycles=1,
    )
    first = _registration_store(valkey_server, namespace, **limits)
    second = _registration_store(valkey_server, namespace, **limits)
    cfg = first._foundation.config
    node_digest = first._node_digest("node")
    requests = ("final-a", "final-b")
    identities = [first._identity("client", request) for request in requests]
    outcomes = []
    try:
        first.register("node", _capabilities(concurrency=1), _digest("owner"))
        deadline = first._foundation.server_time()[0] + 60
        outcomes = _race_select_and_reserve((first, second), requests, deadline)

        created = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        assert len(created) == 1 and created[0].created
        assert sum(isinstance(outcome, RelayStateNoCapacity) for outcome in outcomes) == 1
        assert created[0].reservation_token is not None
        assert len(first.list_reservations()) == 1
        assert first._foundation._client.zcard(cfg.key("reservations:expiry")) == 1
        assert first._foundation._client.zcard(cfg.key("requests:deadline")) == 1
        assert first._foundation._client.hmget(
            cfg.key("cursor"), "_count", "_activity"
        ) == [b"1", b"1"]

        existing = [
            first._foundation._client.exists(cfg.key("request", client, request))
            for client, request in identities
        ]
        assert sorted(existing) == [0, 1]

        memory = InMemoryRelayStateStore(
            RelayStateStoreConfig(namespace="final-slot-memory", **limits),
            acknowledgement_key=b"k" * 32,
        )
        memory.register("node", _capabilities(concurrency=1), _digest("owner"))
        memory_outcomes = _race_select_and_reserve(
            (memory, memory), requests, time.time() + 60
        )
        assert sum(not isinstance(item, Exception) for item in memory_outcomes) == 1
        assert sum(
            isinstance(item, RelayStateNoCapacity) for item in memory_outcomes
        ) == 1
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("node", node_digest),
            *(cfg.key("request", client, request) for client, request in identities),
        ]
        keys.extend(
            cfg.key("reservation", _digest(outcome.reservation_token))
            for outcome in outcomes
            if not isinstance(outcome, Exception) and outcome.reservation_token
        )
        first._foundation._client.delete(*keys)
        first.close()
        second.close()


def test_selection_once_only_same_identity_across_valkey_stores(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    cfg = first._foundation.config
    node_digest = first._node_digest("node")
    client, request = first._identity("client", "same")
    outcomes = []
    try:
        first.register("node", _capabilities(concurrency=1), _digest("owner"))
        deadline = first._foundation.server_time()[0] + 60
        outcomes = _race_select_and_reserve(
            (first, second), ("same", "same"), deadline
        )

        assert sum(outcome.created for outcome in outcomes) == 1
        assert sum(outcome.reservation_token is not None for outcome in outcomes) == 1
        created = next(outcome for outcome in outcomes if outcome.created)
        existing = next(outcome for outcome in outcomes if not outcome.created)
        assert existing.reservation_token is None
        assert existing.selected_node_id == created.selected_node_id == "node"
        assert existing.request_deadline_epoch == created.request_deadline_epoch
        assert existing.reservation_expires_at_epoch == pytest.approx(
            created.reservation_expires_at_epoch
        )
        assert len(first.list_reservations()) == 1
        assert first._foundation._client.zcard(cfg.key("reservations:expiry")) == 1
        assert first._foundation._client.zcard(cfg.key("requests:deadline")) == 1
        assert first._foundation._client.hmget(
            cfg.key("cursor"), "_count", "_activity"
        ) == [b"1", b"1"]

        memory = InMemoryRelayStateStore(
            RelayStateStoreConfig(namespace="same-identity-memory"),
            acknowledgement_key=b"k" * 32,
        )
        memory.register("node", _capabilities(concurrency=1), _digest("owner"))
        memory_outcomes = _race_select_and_reserve(
            (memory, memory), ("same", "same"), time.time() + 60
        )
        assert sum(outcome.created for outcome in memory_outcomes) == 1
        assert sum(outcome.reservation_token is not None for outcome in memory_outcomes) == 1
        assert len(memory.list_reservations()) == 1
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("node", node_digest),
            cfg.key("request", client, request),
        ]
        keys.extend(
            cfg.key("reservation", _digest(outcome.reservation_token))
            for outcome in outcomes
            if outcome.reservation_token
        )
        first._foundation._client.delete(*keys)
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("bound", "limit", "occupation"),
    [
        ("global_reservation", "max_reservations", "reserved"),
        ("per_client_reservation", "max_reservations_per_client", "reserved"),
        ("per_node_reservation", "max_reservations_per_node", "reserved"),
        ("global_lifecycle", "max_request_lifecycles", "reserved"),
        ("per_node_queue_depth", "max_queue_depth_per_node", "queued"),
        ("node_concurrency", None, "claimed"),
    ],
)
def test_selection_capacity_bounds_match_memory_without_rejection_mutation(
    valkey_server, bound, limit, occupation
):
    namespace = uuid.uuid4().hex
    limits = dict(
        max_reservations=8,
        max_reservations_per_client=8,
        max_reservations_per_node=8,
        max_request_lifecycles=8,
        max_queue_depth_per_node=8,
        max_queued_requests=8,
        max_queued_requests_per_client=8,
    )
    if limit is not None:
        limits[limit] = 1
    node_ids = (
        ("node-a", "node-b")
        if bound in {"global_reservation", "per_client_reservation", "global_lifecycle"}
        else ("node-a",)
    )
    candidate_client = "client-a" if bound == "per_client_reservation" else "client-b"
    first = _registration_store(valkey_server, namespace, **limits)
    second = _registration_store(valkey_server, namespace, **limits)
    cfg = first._foundation.config
    occupant = None
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )

    def prepare(store, deadline):
        for node_id in node_ids:
            concurrency = 1 if bound == "node_concurrency" else 8
            store.register(
                node_id, _capabilities(concurrency=concurrency), _digest(node_id)
            )
        if occupation == "claimed":
            assert store.set_scheduler_state(
                "node-a", _digest("node-a"), SchedulerNodeState(claimed_work=1)
            )
            return None
        selected = store.select_and_reserve(
            "client-a", "occupied", "qwen3-8b-instruct", "8k-fast", deadline
        )
        if occupation == "queued":
            store.enqueue_encrypted_request(
                "client-a",
                "occupied",
                selected.reservation_token,
                selected.selected_node_id,
                "qwen3-8b-instruct",
                "8k-fast",
                deadline,
                envelope,
                "cancel",
            )
        return selected

    try:
        deadline = first._foundation.server_time()[0] + 60
        occupant = prepare(first, deadline)
        candidate = first._identity(candidate_client, "candidate")
        node_keys = [
            cfg.key("node", first._node_digest(node_id)) for node_id in node_ids
        ]
        cursor_key = cfg.key("cursor")
        candidate_key = cfg.key("request", *candidate)
        queue_keys = [
            cfg.key("queue", first._node_digest(node_id)) for node_id in node_ids
        ]
        before = {
            "cursor": first._foundation._client.hgetall(cursor_key),
            "nodes": [first._foundation._client.hgetall(key) for key in node_keys],
            "deadlines": first._foundation._client.zrange(
                cfg.key("requests:deadline"), 0, -1, withscores=True
            ),
            "expiries": first._foundation._client.zrange(
                cfg.key("reservations:expiry"), 0, -1, withscores=True
            ),
            "queues": [first._foundation._client.xlen(key) for key in queue_keys],
            "reservations": first.list_reservations(),
        }
        with pytest.raises(RelayStateNoCapacity) as valkey_error:
            second.select_and_reserve(
                candidate_client,
                "candidate",
                "qwen3-8b-instruct",
                "8k-fast",
                deadline,
            )

        memory = InMemoryRelayStateStore(
            RelayStateStoreConfig(namespace=f"capacity-{bound}", **limits),
            acknowledgement_key=b"k" * 32,
        )
        prepare(memory, time.time() + 60)
        with pytest.raises(type(valkey_error.value)):
            memory.select_and_reserve(
                candidate_client,
                "candidate",
                "qwen3-8b-instruct",
                "8k-fast",
                time.time() + 60,
            )

        assert not first._foundation._client.exists(candidate_key)
        assert first._foundation._client.hgetall(cursor_key) == before["cursor"]
        assert [first._foundation._client.hgetall(key) for key in node_keys] == before[
            "nodes"
        ]
        assert (
            first._foundation._client.zrange(
                cfg.key("requests:deadline"), 0, -1, withscores=True
            )
            == before["deadlines"]
        )
        assert (
            first._foundation._client.zrange(
                cfg.key("reservations:expiry"), 0, -1, withscores=True
            )
            == before["expiries"]
        )
        assert [first._foundation._client.xlen(key) for key in queue_keys] == before[
            "queues"
        ]
        assert first.list_reservations() == before["reservations"]
    finally:
        client_a, occupied = first._identity("client-a", "occupied")
        candidate_client_digest, candidate_request = first._identity(
            candidate_client, "candidate"
        )
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            *(cfg.key("node", first._node_digest(node_id)) for node_id in node_ids),
            *(cfg.key("queue", first._node_digest(node_id)) for node_id in node_ids),
            cfg.key("request", client_a, occupied),
            cfg.key("request", candidate_client_digest, candidate_request),
        ]
        if occupant is not None and occupant.reservation_token is not None:
            keys.append(cfg.key("reservation", _digest(occupant.reservation_token)))
        first._foundation._client.delete(*keys)
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
    deadline = time.time() + 10
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
    rounded_deadline = float(f"{time.time() + 60:.6f}")
    deadline = math.nextafter(rounded_deadline, math.inf)
    assert deadline != float(f"{deadline:.6f}")
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
        claim_key = cfg.key("claim", client, request)
        if state == "claimed":
            store._foundation._client.hset(claim_key, mapping={"generation": 1})
            store._foundation._client.zadd(
                cfg.key("claims:expiry"), {client + ":" + request: queued_deadline}
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
        assert not store._foundation._client.exists(claim_key)
        assert store._foundation._client.zscore(
            cfg.key("claims:expiry"), client + ":" + request
        ) is None

    finally:
        keys = [
            cfg.key("schema"), cfg.key("nodes:lease"), cfg.key("cursor"),
            cfg.key("reservations:expiry"), cfg.key("requests:deadline"),
            cfg.key("claims:expiry"), cfg.key("node", node), cfg.key("queue", node),
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


def test_ambiguous_claim_result_is_not_replayed_and_reclaims_monotonically(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, claim_ttl_seconds=0.08)
    second = _registration_store(valkey_server, namespace, claim_ttl_seconds=0.08)
    owner, node_id = _digest("ambiguous-claim-owner"), "ambiguous-claim-node"
    identity = ("ambiguous-claim-client", "ambiguous-claim-request")
    consumer = "ambiguous-claim-consumer"
    cfg = first._foundation.config
    node = first._node_digest(node_id)
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    request_key = cfg.key("request", client, request)
    claim_key = cfg.key("claim", client, request)
    original_evalsha = first._foundation._client.evalsha
    dispatches = 0
    try:
        first.register(node_id, _capabilities(), owner)
        deadline = first._foundation.server_time()[0] + 30
        envelope = _enqueue_claim_fixture(first, node_id, owner, *identity, deadline)
        loaded = first._foundation._client.script_load(CLAIM_SCRIPT.source)
        loaded = loaded.decode() if isinstance(loaded, bytes) else loaded
        assert loaded == CLAIM_SCRIPT.eval_sha1

        def lose_claim_result(*args, **kwargs):
            nonlocal dispatches
            assert args[0] == CLAIM_SCRIPT.eval_sha1
            dispatches += 1
            original_evalsha(*args, **kwargs)
            raise redis.ConnectionError("ambiguous-claim-private-marker")

        first._foundation._client.evalsha = lose_claim_result
        try:
            with pytest.raises(
                ValkeyUnavailableError, match="^state backend unavailable$"
            ) as caught:
                first.claim_queued_request(node_id, owner, consumer)
        finally:
            first._foundation._client.evalsha = original_evalsha
        assert dispatches == 1
        assert caught.value.__cause__ is None
        assert "ambiguous-claim-private-marker" not in "".join(
            traceback.format_exception(caught.value)
        )

        live = second.active_claims(node_id)
        claimed = second.claimed_request(node_id, identity[1])
        assert len(live) == 1 and claimed is not None
        queued, committed = claimed
        assert committed == live[0]
        assert queued.envelope == envelope
        assert (queued.client_public_key, queued.request_id) == identity
        assert second._foundation._client.hget(request_key, "state") == b"claimed"
        assert second._foundation._client.xlen(cfg.key("queue", node)) == 1
        assert second._foundation._client.zcard(cfg.key("requests:deadline")) == 1
        assert second._foundation._client.zcard(cfg.key("claims:expiry")) == 1
        assert (
            second._foundation._client.zscore(cfg.key("claims:expiry"), member)
            == committed.lease_expires_at_epoch
        )
        assert second._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert second._foundation._client.zcard(cfg.key("terminals:expiry")) == 0
        assert (
            second._foundation._client.hget(
                cfg.key("node", node), "scheduler_claimed_work"
            )
            == b"0"
        )
        assert (
            second.claim_queued_request(node_id, owner, "other-consumer").state
            == "empty"
        )

        generations = [committed.generation]
        leases = [committed.lease_expires_at_epoch]
        for consumer_identity in ("reclaim-consumer-one", "reclaim-consumer-two"):
            _wait_for_server_epoch(second, leases[-1])
            assert second.active_claims(node_id) == ()
            assert second._foundation._client.xlen(cfg.key("queue", node)) == 1
            assert second._foundation._client.zcard(cfg.key("requests:deadline")) == 1
            assert second._foundation._client.zcard(cfg.key("terminals:expiry")) == 0
            assert second._foundation._client.hget(request_key, "state") == b"claimed"
            reclaimed = second.claim_queued_request(node_id, owner, consumer_identity)
            assert reclaimed.state == "reclaimed"
            generations.append(reclaimed.generation)
            leases.append(reclaimed.lease_expires_at_epoch)
            assert second._foundation._client.xlen(cfg.key("queue", node)) == 1
            assert second._foundation._client.zcard(cfg.key("claims:expiry")) == 1

        assert generations == sorted(set(generations))
        assert all(new > old for old, new in zip(generations, generations[1:]))
        assert len(second.active_claims(node_id)) == 1
        assert len(second._foundation._client.hgetall(request_key)) > 0
        assert len(second._foundation._client.hgetall(claim_key)) > 0
        for stale_generation, stale_consumer in zip(
            generations[:-1], (consumer, "reclaim-consumer-one")
        ):
            before = _claim_authority_snapshot(second, node_id, *identity)
            stale = second.renew_claim(
                node_id,
                owner,
                stale_consumer,
                *identity,
                stale_generation,
            )
            assert stale.state == "stale_generation"
            assert stale.generation == generations[-1]
            assert _claim_authority_snapshot(second, node_id, *identity) == before
    finally:
        first._foundation._client.evalsha = original_evalsha
        _delete_claim_fixture_state(first, (node_id,), (identity,))
        first._foundation._client.delete(cfg.key("terminals:expiry"))
        first.close()
        second.close()


def test_claim_reclaim_renewal_and_generation_are_atomic_across_clients(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, claim_ttl_seconds=0.08)
    second = _registration_store(valkey_server, namespace, claim_ttl_seconds=0.08)
    owner = _digest("claim-owner")
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    deadline = time.time() + 60
    selection = None
    client_digest = hashlib.sha256(b"client\0claim-client").hexdigest()
    request_digest = hashlib.sha256(b"request\0claim-request").hexdigest()
    node_digest = first._node_digest("claim-node")
    cfg = first._foundation.config
    try:
        first.register("claim-node", _capabilities(concurrency=2), owner)
        selection = first.select_and_reserve(
            "claim-client",
            "claim-request",
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            "cancel",
        )
        first.enqueue_encrypted_request(
            "claim-client",
            "claim-request",
            selection.reservation_token,
            "claim-node",
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            envelope,
            "cancel",
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(
                    lambda store: store.claim_queued_request(
                        "claim-node", owner, "consumer"
                    ),
                    (first, second),
                )
            )
        winner = next(result for result in claims if result.state == "claimed")
        assert [result.state for result in claims].count("claimed") == 1
        assert [result.state for result in claims].count("empty") == 1
        assert first.queued_requests("claim-node") == ()
        assert (
            first.claimed_request("claim-node", "claim-request")[1].generation
            == winner.generation
        )
        assert (
            first.active_claims("claim-node")[0].consumer_identity_digest
            == hashlib.sha256(b"consumer\0consumer").hexdigest()
        )
        renewed = second.renew_claim(
            "claim-node",
            owner,
            "consumer",
            "claim-client",
            "claim-request",
            winner.generation,
        )
        assert renewed.state == "continued" and renewed.generation == winner.generation
        first.set_scheduler_state(
            "claim-node", owner, SchedulerNodeState(draining=True, claimed_work=1)
        )
        renewed = second.renew_claim(
            "claim-node", owner, "consumer", "claim-client", "claim-request",
            winner.generation,
        )
        assert renewed.state == "continued"
        first.set_scheduler_state("claim-node", owner, SchedulerNodeState())
        request_key = cfg.key("request", client_digest, request_digest)
        first._foundation._client.hset(
            request_key, "claim_generation", winner.generation + 1
        )
        with pytest.raises(ValkeySchemaIncompatibleError):
            first.active_claims("claim-node")
        first._foundation._client.hset(
            request_key, "claim_generation", winner.generation
        )
        assert (
            first.renew_claim(
                "claim-node",
                owner,
                "wrong",
                "claim-client",
                "claim-request",
                winner.generation,
            ).state
            == "owner_mismatch"
        )
        _wait_for_server_epoch(first, renewed.lease_expires_at_epoch)
        assert first.active_claims("claim-node") == ()
        reclaimed = second.claim_queued_request("claim-node", owner, "consumer-2")
        assert (
            reclaimed.state == "reclaimed" and reclaimed.generation > winner.generation
        )
        assert (
            first.renew_claim(
                "claim-node",
                owner,
                "consumer",
                "claim-client",
                "claim-request",
                winner.generation,
            ).state
            == "stale_generation"
        )
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            cfg.key("request", client_digest, request_digest),
            cfg.key("claim", client_digest, request_digest),
        ]
        if selection is not None and selection.reservation_token is not None:
            keys.append(
                cfg.key(
                    "reservation",
                    hashlib.sha256(selection.reservation_token.encode()).hexdigest(),
                )
            )
        first._foundation._client.delete(*keys)
        first.close()
        second.close()

def _enqueue_claim_fixture(
    store, node_id, owner, client, request, deadline, envelope=None
):
    selection = store.select_and_reserve(
        client, request, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
    )
    envelope = envelope or EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    store.enqueue_encrypted_request(
        client,
        request,
        selection.reservation_token,
        node_id,
        "qwen3-8b-instruct",
        "8k-fast",
        deadline,
        envelope,
        "cancel",
    )
    return envelope


@pytest.mark.parametrize("stage", ("reserved", "queued", "claimed"))
def test_cancellation_is_shared_retrievable_and_retains_only_live_control(
    valkey_server, stage
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    owner = _digest(f"cancel-owner-{stage}")
    node_id = f"cancel-node-{stage}"
    consumer = f"cancel-consumer-{stage}"
    identity = (f"cancel-client-{stage}", f"cancel-request-{stage}")
    try:
        first.register(node_id, _capabilities(), owner)
        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + 5
        selection = first.select_and_reserve(
            *identity, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        generation = None
        if stage != "reserved":
            first.enqueue_encrypted_request(
                *identity,
                selection.reservation_token,
                node_id,
                "qwen3-8b-instruct",
                "8k-fast",
                deadline,
                EncryptedRequestEnvelope(
                    "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
                ),
                "cancel",
            )
        if stage == "claimed":
            generation = first.claim_queued_request(
                node_id, owner, consumer
            ).generation
        created = second.cancel_or_expire_request(*identity, "cancel")
        assert (created.state, created.reason, created.new_outcome) == (
            "cancelled", "requester_cancelled", True
        )
        assert first.cancel_or_expire_request(
            *identity, "cancel"
        ) == dataclasses.replace(created, new_outcome=False)
        assert second.retrieve_encrypted_response(
            *identity, selection.reservation_token
        ).state == "completed_unavailable"
        assert len(first.control_tombstones()) == (1 if stage == "claimed" else 0)
        if generation is not None:
            control = second.renew_claim_or_read_control(
                node_id, owner, consumer, *identity, generation
            )
            assert (control.state, control.reason) == (
                "cancelled", "requester_cancelled"
            )
    finally:
        _delete_claim_fixture_state(first, (node_id,), (identity,))
        first.close()
        second.close()



@pytest.mark.parametrize("retrieval_state", ("response_ready", "acknowledged", "retrieval_expired"))
@pytest.mark.parametrize("transition", ("cancelled", "expired"))
def test_cancel_or_expire_after_completed_response_preserves_authority(
    valkey_server, retrieval_state, transition
):
    namespace = uuid.uuid4().hex
    options = {
        "terminal_retention_seconds": 30,
        "control_tombstone_ttl_seconds": 30,
        "response_replay_ttl_seconds": 10,
    }
    if retrieval_state == "retrieval_expired":
        options["response_replay_ttl_seconds"] = 0.05
    first = _registration_store(valkey_server, namespace, **options)
    second = _registration_store(valkey_server, namespace, **options)
    node, owner, consumer = "completed-race-node", _digest("completed-race-owner"), "consumer"
    identity = (f"completed-{retrieval_state}-{transition}", "request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "response-ciphertext", "response-key", "response-iv"
    )
    try:
        first.register(node, _capabilities(), owner)
        deadline = first._foundation.server_time()[0] + 2
        selection = first.select_and_reserve(
            *identity, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        first.enqueue_encrypted_request(
            *identity, selection.reservation_token, node, "qwen3-8b-instruct", "8k-fast",
            deadline, EncryptedRequestEnvelope(
                "tokenplace_api_v1_relay_e2ee", 1, "request-ciphertext", "request-key", "request-iv"
            ), "cancel",
        )
        claim = first.claim_queued_request(node, owner, consumer)
        accepted = first.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        if retrieval_state == "acknowledged":
            ready = second.retrieve_encrypted_response(*identity, selection.reservation_token)
            second.retrieve_encrypted_response(
                *identity, selection.reservation_token, ready.acknowledgement_token
            )
        elif retrieval_state == "retrieval_expired":
            _wait_for_server_epoch(first, accepted.replay_expires_at_epoch)
            first._cleanup_completed_records()
        keys, _ = _response_acceptance_authority(first, node, identity)
        before = _exact_key_snapshot(first, keys)
        result = second.cancel_or_expire_request(
            *identity,
            "cancel" if transition == "cancelled" else None,
            status=transition,
            reason=(
                "requester_cancelled"
                if transition == "cancelled"
                else "request_deadline_expired"
            ),
        )
        assert (result.state, result.reason, result.new_outcome) == (
            "completed", "response_completed", False
        )
        assert _exact_key_snapshot(first, keys) == before
        if transition == "cancelled":
            invalid = first.cancel_or_expire_request(*identity, "wrong")
            assert invalid.state == invalid.reason == "invalid_cancellation_proof"
            assert _exact_key_snapshot(first, keys) == before
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


@pytest.mark.parametrize("transition", ("cancelled", "expired"))
def test_accept_response_after_cancel_or_expire_preserves_authority(
    valkey_server, transition
):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server, namespace, terminal_retention_seconds=30,
        control_tombstone_ttl_seconds=30, response_replay_ttl_seconds=30,
    )
    second = _registration_store(
        valkey_server, namespace, terminal_retention_seconds=30,
        control_tombstone_ttl_seconds=30, response_replay_ttl_seconds=30,
    )
    node, owner, consumer = f"{transition}-winner-node", _digest(f"{transition}-owner"), "consumer"
    identity = (f"{transition}-winner-client", "request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "response-ciphertext", "response-key", "response-iv"
    )
    try:
        first.register(node, _capabilities(), owner)
        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + (0.15 if transition == "expired" else 10)
        _enqueue_claim_fixture(first, node, owner, *identity, deadline)
        claim = first.claim_queued_request(node, owner, consumer)
        if transition == "expired":
            _wait_for_server_epoch(first, deadline)
        first.cancel_or_expire_request(
            *identity, "cancel" if transition == "cancelled" else None,
            status=transition,
            reason="requester_cancelled" if transition == "cancelled" else "request_deadline_expired",
        )
        keys, member = _response_acceptance_authority(first, node, identity)
        cfg = first._foundation.config
        client, request = first._identity(*identity)
        node_digest = first._node_digest(node)
        control_key = cfg.key("control", node_digest, client, request)
        control_index = cfg.key("control:expiry")
        before = (_exact_key_snapshot(first, keys), first._foundation._client.hgetall(control_key),
                  first._foundation._client.zscore(control_index, f"{node_digest}:{member}"))
        with pytest.raises(RelayStateConflict, match="response lifecycle conflict"):
            second.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, envelope
            )
        after = (_exact_key_snapshot(first, keys), first._foundation._client.hgetall(control_key),
                 first._foundation._client.zscore(control_index, f"{node_digest}:{member}"))
        assert after == before
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first._foundation._client.delete(first._foundation.config.key("control:expiry"))
        first.close()
        second.close()


@pytest.mark.parametrize("stage", ("reserved", "queued"))
@pytest.mark.parametrize("transition", ("cancelled", "expired"))
def test_generation_zero_terminal_response_conflict_preserves_authority(
    valkey_server, stage, transition
):
    namespace = uuid.uuid4().hex
    options = {
        "terminal_retention_seconds": 30,
        "response_replay_ttl_seconds": 10,
        "control_tombstone_ttl_seconds": 10,
    }
    first = _registration_store(valkey_server, namespace, **options)
    second = _registration_store(valkey_server, namespace, **options)
    node, owner, consumer = "generation-zero-node", _digest("generation-zero-owner"), "consumer"
    identity = (f"generation-zero-{stage}-{transition}", "request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "response-ciphertext", "response-key", "response-iv"
    )
    try:
        first.register(node, _capabilities(), owner)
        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + (0.1 if transition == "expired" else 10)
        selection = first.select_and_reserve(
            *identity, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        if stage == "queued":
            first.enqueue_encrypted_request(
                *identity, selection.reservation_token, node, "qwen3-8b-instruct", "8k-fast",
                deadline, EncryptedRequestEnvelope(
                    "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
                ), "cancel",
            )
        if transition == "expired":
            _wait_for_server_epoch(first, deadline)
        first.cancel_or_expire_request(
            *identity, "cancel" if transition == "cancelled" else None,
            status=transition,
            reason="requester_cancelled" if transition == "cancelled" else "request_deadline_expired",
        )
        keys, _ = _response_acceptance_authority(first, node, identity)
        before = _exact_key_snapshot(first, keys)
        with pytest.raises(RelayStateConflict, match="response lifecycle conflict"):
            second.accept_encrypted_response(node, owner, consumer, *identity, 1, envelope)
        assert _exact_key_snapshot(first, keys) == before
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("generation", "owner", "consumer", "missing"),
    (
        ("0", "", "", "owner_digest"),
        ("0", "", "", "consumer_digest"),
        ("0", _digest("owner"), "", None),
        ("1", "", "", None),
    ),
)
def test_malformed_existing_terminal_fails_without_mutation(
    valkey_server, generation, owner, consumer, missing
):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        terminal_retention_seconds=30,
        response_replay_ttl_seconds=10,
        control_tombstone_ttl_seconds=10,
    )
    node = "malformed-terminal-node"
    identity = (f"malformed-terminal-{generation}-{missing}", "request")
    try:
        store.register(node, _capabilities(), _digest("registration-owner"))
        deadline = store._foundation.server_time()[0] + 10
        selection = store.select_and_reserve(
            *identity, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        store.cancel_or_expire_request(*identity, "cancel")
        keys, _ = _response_acceptance_authority(store, node, identity)
        terminal_key = keys[9]
        store._foundation._client.hset(
            terminal_key, mapping={"generation": generation, "owner_digest": owner,
                                   "consumer_digest": consumer}
        )
        if missing:
            store._foundation._client.hdel(terminal_key, missing)
        before = _exact_key_snapshot(store, keys)
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.cancel_or_expire_request(*identity, "cancel")
        assert _exact_key_snapshot(store, keys) == before
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_terminal_race_returns_authoritative_domain_outcome(valkey_server):
    namespace = uuid.uuid4().hex
    stores = tuple(_registration_store(valkey_server, namespace) for _ in range(2))
    node, owner, consumer = "terminal-race-node", _digest("terminal-race-owner"), "consumer"
    identity = ("terminal-race-client", "terminal-race-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "response-ciphertext", "response-key", "response-iv"
    )
    barrier = Barrier(2)
    try:
        stores[0].register(node, _capabilities(), owner)
        _enqueue_claim_fixture(stores[0], node, owner, *identity, time.time() + 60)
        claim = stores[0].claim_queued_request(node, owner, consumer)

        def cancel():
            barrier.wait()
            return stores[0].cancel_or_expire_request(*identity, "cancel")

        def respond():
            barrier.wait()
            try:
                return stores[1].accept_encrypted_response(
                    node, owner, consumer, *identity, claim.generation, envelope
                )
            except RelayStateConflict:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            cancel_result, response_result = (
                future.result() for future in (executor.submit(cancel), executor.submit(respond))
            )
        cfg = stores[0]._foundation.config
        client, request = stores[0]._identity(*identity)
        terminal = stores[0]._foundation._client.hgetall(
            cfg.key("terminal", client, request)
        )
        outcome = terminal[b"outcome"].decode()
        assert outcome in {"cancelled", "completed"}
        if outcome == "cancelled":
            assert cancel_result.new_outcome is True and response_result == "conflict"
        else:
            assert (cancel_result.state, cancel_result.reason, cancel_result.new_outcome) == (
                "completed", "response_completed", False
            )
            assert response_result.new_outcome is True
        request_state = stores[0]._foundation._client.hget(
            cfg.key("request", client, request), "state"
        ).decode()
        assert request_state == ("cancelled" if outcome == "cancelled" else "response_ready")
        assert stores[0]._foundation._client.exists(
            cfg.key("claim", client, request)
        ) == 0
        assert stores[0]._foundation._client.zscore(
            cfg.key("claims:expiry"), f"{client}:{request}"
        ) is None
        assert stores[0]._foundation._client.exists(
            cfg.key("response", client, request)
        ) == (0 if outcome == "cancelled" else 1)
    finally:
        _delete_claim_fixture_state(stores[0], (node,), (identity,))
        for store in stores:
            store.close()

def test_expired_addressed_control_identity_mismatch_is_not_deleted(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server, namespace, control_tombstone_ttl_seconds=0.001
    )
    second = _registration_store(
        valkey_server, namespace, control_tombstone_ttl_seconds=0.001
    )
    node, owner, consumer = "control-identity-node", _digest("control-owner"), "consumer"
    identity = ("control-identity-client", "control-identity-request")
    cfg = first._foundation.config
    client, request = first._identity(*identity)
    node_digest = first._node_digest(node)
    control_key = cfg.key("control", node_digest, client, request)
    control_index = cfg.key("control:expiry")
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        first.cancel_or_expire_request(*identity, "cancel")
        first._foundation._client.hset(control_key, "client", _digest("mismatch"))
        score = first._foundation._client.zscore(
            control_index, f"{node_digest}:{client}:{request}"
        )
        _wait_for_server_epoch(first, score)
        before = (
            first._foundation._client.hgetall(control_key),
            first._foundation._client.zrange(control_index, 0, -1, withscores=True),
        )

        with pytest.raises(ValkeySchemaIncompatibleError):
            second.renew_claim_or_read_control(
                node, owner, consumer, *identity, claim.generation
            )

        assert before == (
            first._foundation._client.hgetall(control_key),
            first._foundation._client.zrange(control_index, 0, -1, withscores=True),
        )
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first._foundation._client.delete(control_key, control_index)
        first.close()
        second.close()


def _delete_claim_fixture_state(store, node_ids, identities):
    cfg = store._foundation.config
    keys = [
        cfg.key("schema"),
        cfg.key("nodes:lease"),
        cfg.key("cursor"),
        cfg.key("reservations:expiry"),
        cfg.key("requests:deadline"),
        cfg.key("claims:expiry"),
        cfg.key("responses:expiry"),
        cfg.key("terminals:expiry"),
    ]
    for node_id in node_ids:
        node = store._node_digest(node_id)
        keys.extend((cfg.key("node", node), cfg.key("queue", node)))
    for client_id, request_id in identities:
        client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
        request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
        keys.extend(
            (
                cfg.key("request", client, request),
                cfg.key("claim", client, request),
                cfg.key("response", client, request),
                cfg.key("terminal", client, request),
                cfg.key("progress", client, request),
            )
        )
    store._foundation._client.delete(*keys)


def _response_acceptance_authority(store, node_id, identity):
    """Return the exact declared keys and identity member for one acceptance."""
    cfg = store._foundation.config
    client_id, request_id = identity
    client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
    node = store._node_digest(node_id)
    return (
        (
            cfg.key("nodes:lease"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("claim", client, request),
            cfg.key("request", client, request),
            cfg.key("claims:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("response", client, request),
            cfg.key("responses:expiry"),
            cfg.key("terminal", client, request),
            cfg.key("terminals:expiry"),
            cfg.key("progress", client, request),
        ),
        f"{client}:{request}",
    )


def _exact_key_snapshot(store, keys):
    datastore = store._foundation._client
    return (
        tuple(
            datastore.zrange(keys[index], 0, -1, withscores=True)
            for index in (0, 5, 6, 8, 10)
        ),
        tuple(datastore.hgetall(keys[index]) for index in (1, 3, 4, 7, 9, 11)),
        datastore.xrange(keys[2]),
    )


def _accepted_retrieval_fixture(stores, label):
    first = stores[0]
    node = f"{label}-node"
    owner = _digest(f"{label}-owner")
    consumer = f"{label}-consumer"
    identity = (f"{label}-client", f"{label}-request")
    credential = _digest(f"{label}-credential")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee",
        1,
        f"{label}-ciphertext",
        f"{label}-key",
        f"{label}-iv",
    )
    first.register(node, _capabilities(), owner)
    _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
    claim = first.claim_queued_request(node, owner, consumer)
    accepted = first.accept_encrypted_response(
        node, owner, consumer, *identity, claim.generation, envelope
    )
    keys, member = _response_acceptance_authority(first, node, identity)
    retrieval_digest = hashlib.sha256(credential.encode()).hexdigest()
    first._foundation._client.hset(keys[4], "token_digest", retrieval_digest)
    first._foundation._client.hset(
        keys[9], "retrieval_credential_digest", retrieval_digest
    )
    return node, identity, credential, envelope, accepted, keys, member


def test_response_retrieval_namespace_isolation(valkey_server):
    namespaces = (uuid.uuid4().hex, uuid.uuid4().hex)
    stores = tuple(_registration_store(valkey_server, value) for value in namespaces)
    identity = ("shared-retrieval-client", "shared-retrieval-request")
    credential = _digest("shared-retrieval-credential")
    fixtures = []
    try:
        for index, store in enumerate(stores):
            node = f"namespace-retrieval-node-{index}"
            owner = _digest(f"namespace-retrieval-owner-{index}")
            consumer = f"namespace-retrieval-consumer-{index}"
            envelope = EncryptedResponseEnvelope(
                "tokenplace_api_v1_relay_e2ee",
                1,
                f"namespace-retrieval-ciphertext-{index}",
                f"namespace-retrieval-key-{index}",
                f"namespace-retrieval-iv-{index}",
            )
            store.register(node, _capabilities(), owner)
            _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
            claim = store.claim_queued_request(node, owner, consumer)
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, envelope
            )
            keys, member = _response_acceptance_authority(store, node, identity)
            retrieval_digest = hashlib.sha256(credential.encode()).hexdigest()
            store._foundation._client.hset(keys[4], "token_digest", retrieval_digest)
            store._foundation._client.hset(
                keys[9], "retrieval_credential_digest", retrieval_digest
            )
            fixtures.append((node, envelope, keys, member))

        namespace_b = _exact_key_snapshot(stores[1], fixtures[1][2])
        retrieved_a = stores[0].retrieve_encrypted_response(*identity, credential)
        assert retrieved_a.state == "response_ready"
        assert retrieved_a.envelope == fixtures[0][1]
        assert _exact_key_snapshot(stores[1], fixtures[1][2]) == namespace_b

        acknowledged = stores[0].retrieve_encrypted_response(
            *identity, credential, retrieved_a.acknowledgement_token
        )
        assert acknowledged.state == "acknowledged"
        assert _exact_key_snapshot(stores[1], fixtures[1][2]) == namespace_b
        retrieved_b = stores[1].retrieve_encrypted_response(*identity, credential)
        assert retrieved_b.state == "response_ready"
        assert retrieved_b.envelope == fixtures[1][1]
    finally:
        for store, fixture in zip(stores, fixtures):
            _delete_claim_fixture_state(store, (fixture[0],), (identity,))
        for store in stores:
            store.close()


def test_response_retrieval_preserves_additive_fields_and_bounded_backlog(
    valkey_server,
):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, node_transition_batch_size=1
    )
    fixtures = []
    try:
        for label in ("additive-target", "backlog-a", "backlog-b"):
            fixtures.append(_accepted_retrieval_fixture((store,), label))
        target = fixtures[0]
        _, identity, credential, envelope, _, keys, member = target
        datastore = store._foundation._client
        additive = {
            keys[7]: (b"response-extension", b"response-preserved"),
            keys[9]: (b"terminal-extension", b"terminal-preserved"),
            keys[4]: (b"lifecycle-extension", b"lifecycle-preserved"),
        }
        for key, (field, value) in additive.items():
            datastore.hset(key, field, value)

        before = _exact_key_snapshot(store, keys)
        response_before = datastore.hgetall(keys[7])
        terminal_before = datastore.hgetall(keys[9])
        lifecycle_before = datastore.hgetall(keys[4])
        response_index_before = datastore.zrange(keys[8], 0, -1, withscores=True)
        terminal_index_before = datastore.zrange(keys[10], 0, -1, withscores=True)
        backlog_before = [
            (
                tuple(datastore.hgetall(fixture[5][index]) for index in (4, 7, 9)),
                datastore.zscore(fixture[5][8], fixture[6]),
                datastore.zscore(fixture[5][10], fixture[6]),
            )
            for fixture in fixtures[1:]
        ]
        result = store.retrieve_encrypted_response(*identity, credential)
        assert result.state == "response_ready"
        assert result.envelope == envelope
        assert _exact_key_snapshot(store, keys) == before
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.state = "changed"
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.envelope.ciphertext = "changed"

        acknowledged = store.retrieve_encrypted_response(
            *identity, credential, result.acknowledgement_token
        )
        assert acknowledged.state == "acknowledged"
        assert datastore.exists(keys[7]) == 0
        assert datastore.zscore(keys[8], member) is None
        expected_terminal = terminal_before | {b"retrieval_state": b"acknowledged"}
        assert datastore.hgetall(keys[9]) == expected_terminal
        assert datastore.hgetall(keys[4]) == lifecycle_before
        assert datastore.zrange(keys[8], 0, -1, withscores=True) == [
            entry for entry in response_index_before if entry[0].decode() != member
        ]
        assert datastore.zrange(keys[10], 0, -1, withscores=True) == terminal_index_before
        assert response_before
        for key, (field, value) in additive.items():
            if key != keys[7]:
                assert datastore.hget(key, field) == value
        assert [
            (
                tuple(datastore.hgetall(fixture[5][index]) for index in (4, 7, 9)),
                datastore.zscore(fixture[5][8], fixture[6]),
                datastore.zscore(fixture[5][10], fixture[6]),
            )
            for fixture in fixtures[1:]
        ] == backlog_before
    finally:
        _delete_claim_fixture_state(
            store,
            tuple(fixture[0] for fixture in fixtures),
            tuple(fixture[1] for fixture in fixtures),
        )
        store.close()


@pytest.mark.parametrize("failure_stage", ("read", "ack"))
def test_response_retrieval_backend_failures_are_redacted(
    valkey_server, caplog, failure_stage
):
    namespace = f"retrieval-redaction-{uuid.uuid4().hex}"
    endpoint_marker = "retrieval-private-endpoint-marker"
    store = _registration_store(valkey_server, namespace)
    fixture = _accepted_retrieval_fixture((store,), f"redacted-{failure_stage}")
    node, identity, credential, envelope, _, keys, _ = fixture
    initial = store.retrieve_encrypted_response(*identity, credential)
    token = initial.acknowledgement_token
    assert token is not None
    raw_acknowledgement_key = _ACKNOWLEDGEMENT_KEY.decode()
    markers = (
        credential,
        token,
        raw_acknowledgement_key,
        envelope.ciphertext,
        endpoint_marker,
        namespace,
        *keys,
    )
    original_evalsha = store._foundation._client.evalsha
    dispatches = []
    matching_dispatches = 0
    before = _exact_key_snapshot(store, keys)
    store._foundation._client.script_load(RETRIEVE_RESPONSE_SCRIPT.source)

    def fail_retrieval(*args, **kwargs):
        nonlocal matching_dispatches
        if args[0] != RETRIEVE_RESPONSE_SCRIPT.eval_sha1:
            return original_evalsha(*args, **kwargs)
        matching_dispatches += 1
        dispatches.append((args, kwargs))
        if failure_stage == "read" or matching_dispatches == 2:
            raise redis.ConnectionError(" ".join(markers))
        return original_evalsha(*args, **kwargs)

    try:
        store._foundation._client.evalsha = fail_retrieval
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(
                ValkeyUnavailableError, match="^state backend unavailable$"
            ) as caught:
                store.retrieve_encrypted_response(
                    *identity,
                    credential,
                    token if failure_stage == "ack" else None,
                )
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
        assert len(dispatches) == (1 if failure_stage == "read" else 2)
        for dispatched_args, dispatched_kwargs in dispatches:
            assert dispatched_kwargs == {}
            wire = tuple(
                value if isinstance(value, bytes) else str(value).encode()
                for value in dispatched_args[2:]
            )
            assert credential.encode() not in wire
            assert token.encode() not in wire
            assert _ACKNOWLEDGEMENT_KEY not in wire
        assert _exact_key_snapshot(store, keys) == before
    finally:
        store._foundation._client.evalsha = original_evalsha
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_response_retrievals_are_concurrent_replayable_and_independently_derived(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = tuple(_registration_store(valkey_server, namespace) for _ in range(2))
    fixture = _accepted_retrieval_fixture(stores, "concurrent-retrieval")
    node, identity, credential, envelope, accepted, keys, member = fixture
    barrier = Barrier(2, timeout=2)
    originals = [store._foundation._client.evalsha for store in stores]
    try:
        for index, store in enumerate(stores):
            original = originals[index]

            def synchronized(*args, original=original):
                if args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1:
                    barrier.wait()
                return original(*args)

            store._foundation._client.evalsha = synchronized
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(store.retrieve_encrypted_response, *identity, credential)
                for store in stores
            ]
            results = [future.result(timeout=3) for future in futures]
        assert results[0] == results[1]
        result = results[0]
        assert result.state == "response_ready"
        assert result.envelope == envelope
        assert result.replay_expires_at_epoch == accepted.replay_expires_at_epoch
        client_digest, request_digest = stores[0]._identity(*identity)
        response_digest = hashlib.sha256(
            stores[0]._serialized_response_envelope(envelope)
        ).hexdigest()
        message = (
            b"token.place/relay-response-ack/v1\0"
            + bytes.fromhex(client_digest)
            + bytes.fromhex(request_digest)
            + struct.pack("!d", accepted.accepted_at_epoch)
            + bytes.fromhex(response_digest)
        )
        expected = hmac.new(
            _ACKNOWLEDGEMENT_KEY, message, hashlib.sha256
        ).hexdigest()
        assert result.acknowledgement_token == expected
        assert stores[0]._foundation._client.exists(keys[7]) == 1
        assert stores[0]._foundation._client.zscore(keys[8], member) is not None
    finally:
        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        _delete_claim_fixture_state(stores[0], (node,), (identity,))
        for store in stores:
            store.close()


@pytest.mark.parametrize(
    ("kind", "value"),
    (
        ("credential", None),
        ("credential", "short"),
        ("credential", "A" * 64),
        ("credential", "a" * 65),
        ("credential", "b" * 64),
        ("ack", "short"),
        ("ack", "A" * 64),
        ("ack", "a" * 65),
        ("ack", "b" * 64),
    ),
)
def test_response_retrieval_rejects_noncanonical_authority_without_disclosure_or_mutation(
    valkey_server, kind, value
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    fixture = _accepted_retrieval_fixture((store,), f"invalid-{kind}-{uuid.uuid4().hex}")
    node, identity, credential, envelope, _, keys, _ = fixture
    try:
        before = _exact_key_snapshot(store, keys)
        if kind == "credential":
            result = store.retrieve_encrypted_response(*identity, value)
            assert result.state == "invalid_retrieval_credential"
        else:
            result = store.retrieve_encrypted_response(*identity, credential, value)
            assert result.state == "invalid_acknowledgement"
        assert result.envelope is None
        assert result.acknowledgement_token is None
        assert _exact_key_snapshot(store, keys) == before
        assert store.retrieve_encrypted_response(*identity, credential).envelope == envelope
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_response_acknowledgement_committed_lost_reply_requires_explicit_retry(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = tuple(_registration_store(valkey_server, namespace) for _ in range(2))
    fixture = _accepted_retrieval_fixture(stores, "lost-ack")
    node, identity, credential, _, _, keys, member = fixture
    ready = stores[0].retrieve_encrypted_response(*identity, credential)
    original = stores[0]._foundation._client.evalsha
    ack_dispatches = 0
    try:
        def lose_after_commit(*args):
            nonlocal ack_dispatches
            result = original(*args)
            if args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1 and b"ack" in args:
                ack_dispatches += 1
                raise redis.ConnectionError("sensitive lost acknowledgement reply")
            return result

        stores[0]._foundation._client.evalsha = lose_after_commit
        with pytest.raises(ValkeyUnavailableError, match="^state backend unavailable$"):
            stores[0].retrieve_encrypted_response(
                *identity, credential, ready.acknowledgement_token
            )
        assert ack_dispatches == 1
        assert stores[0]._foundation._client.exists(keys[7]) == 0
        assert stores[0]._foundation._client.zscore(keys[8], member) is None
        stores[0]._foundation._client.evalsha = original
        assert stores[1].retrieve_encrypted_response(
            *identity, credential, ready.acknowledgement_token
        ).state == "acknowledged"
    finally:
        stores[0]._foundation._client.evalsha = original
        _delete_claim_fixture_state(stores[0], (node,), (identity,))
        for store in stores:
            store.close()


def test_response_concurrent_acknowledgements_have_coherent_terminal(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = tuple(_registration_store(valkey_server, namespace) for _ in range(2))
    fixture = _accepted_retrieval_fixture(stores, "ack-race")
    node, identity, credential, _, _, keys, member = fixture
    ready = stores[0].retrieve_encrypted_response(*identity, credential)
    barrier = Barrier(2, timeout=2)
    originals = [store._foundation._client.evalsha for store in stores]
    try:
        for index, store in enumerate(stores):
            original = originals[index]

            def synchronized(*args, original=original):
                if args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1 and b"ack" in args:
                    barrier.wait()
                return original(*args)

            store._foundation._client.evalsha = synchronized
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    store.retrieve_encrypted_response,
                    *identity,
                    credential,
                    ready.acknowledgement_token,
                )
                for store in stores
            ]
            outcomes = [future.result(timeout=3).state for future in futures]
        assert outcomes == ["acknowledged", "acknowledged"]
        assert stores[0]._foundation._client.exists(keys[7]) == 0
        assert stores[0]._foundation._client.zscore(keys[8], member) is None
        terminal = stores[0]._foundation._client.hgetall(keys[9])
        assert terminal[b"outcome"] == b"completed"
        assert terminal[b"retrieval_state"] == b"acknowledged"
        terminal_dump = stores[0]._foundation._client.dump(keys[9])
        assert stores[0].retrieve_encrypted_response(*identity, credential).state == "acknowledged"
        assert stores[0]._foundation._client.dump(keys[9]) == terminal_dump
    finally:
        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        _delete_claim_fixture_state(stores[0], (node,), (identity,))
        for store in stores:
            store.close()


def test_response_concurrent_conflicting_acknowledgement_has_one_valid_winner(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = tuple(_registration_store(valkey_server, namespace) for _ in range(2))
    target = _accepted_retrieval_fixture(stores, "conflicting-ack-target")
    unrelated = _accepted_retrieval_fixture(stores, "conflicting-ack-unrelated")
    target_ready = stores[0].retrieve_encrypted_response(*target[1], target[2])
    unrelated_ready = stores[1].retrieve_encrypted_response(
        *unrelated[1], unrelated[2]
    )
    datastore = stores[0]._foundation._client
    unrelated_before = (
        datastore.hgetall(unrelated[5][7]),
        datastore.hgetall(unrelated[5][9]),
        datastore.zscore(unrelated[5][8], unrelated[6]),
    )
    response_expiry_before = datastore.zrange(target[5][8], 0, -1, withscores=True)
    barrier = Barrier(2, timeout=2)
    originals = [store._foundation._client.evalsha for store in stores]
    try:
        for index, store in enumerate(stores):
            original = originals[index]

            def synchronized(*args, original=original):
                if (
                    args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1
                    and b"read" in args
                ):
                    barrier.wait()
                return original(*args)

            store._foundation._client.evalsha = synchronized
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            valid = pool.submit(
                stores[0].retrieve_encrypted_response,
                *target[1],
                target[2],
                target_ready.acknowledgement_token,
            )
            conflicting = pool.submit(
                stores[1].retrieve_encrypted_response,
                *target[1],
                target[2],
                unrelated_ready.acknowledgement_token,
            )
            assert valid.result(timeout=3).state == "acknowledged"
            assert conflicting.result(timeout=3).state == "invalid_acknowledgement"

        assert datastore.exists(target[5][7]) == 0
        assert datastore.zscore(target[5][8], target[6]) is None
        assert datastore.zrange(target[5][8], 0, -1, withscores=True) == [
            entry for entry in response_expiry_before if entry[0] != target[6].encode()
        ]
        terminal = datastore.hgetall(target[5][9])
        assert terminal[b"outcome"] == b"completed"
        assert terminal[b"retrieval_state"] == b"acknowledged"
        assert (
            datastore.hgetall(unrelated[5][7]),
            datastore.hgetall(unrelated[5][9]),
            datastore.zscore(unrelated[5][8], unrelated[6]),
        ) == unrelated_before
        stores[1]._foundation._client.evalsha = originals[1]
        assert stores[1].retrieve_encrypted_response(
            *unrelated[1], unrelated[2]
        ).envelope == unrelated[3]
    finally:
        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        _delete_claim_fixture_state(
            stores[0],
            (target[0], unrelated[0]),
            (target[1], unrelated[1]),
        )
        for store in stores:
            store.close()


def test_response_acknowledgement_versus_expiry_has_coherent_expiry_winner(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = tuple(_registration_store(valkey_server, namespace) for _ in range(2))
    target = _accepted_retrieval_fixture(stores, "ack-expiry-target")
    unrelated = _accepted_retrieval_fixture(stores, "ack-expiry-unrelated")
    target_ready = stores[0].retrieve_encrypted_response(*target[1], target[2])
    datastore = stores[0]._foundation._client
    seconds, micros = stores[0]._foundation.server_time()
    boundary_raw = format(seconds + micros / 1_000_000 + 0.25, ".17g")
    datastore.hset(target[5][7], "replay_expires_at_epoch", boundary_raw)
    datastore.hset(target[5][9], "replay_expires_at_epoch", boundary_raw)
    datastore.zadd(target[5][8], {target[6]: float(boundary_raw)})
    terminal_before = datastore.hgetall(target[5][9])
    unrelated_before = (
        tuple(datastore.dump(unrelated[5][index]) for index in (3, 4, 7, 9, 11)),
        tuple(
            datastore.zscore(unrelated[5][index], unrelated[6])
            for index in (5, 6, 8, 10)
        ),
    )
    response_expiry_before = datastore.zrange(target[5][8], 0, -1, withscores=True)
    dispatch_barrier = Barrier(2, timeout=2)
    acknowledgement_read = Event()
    release_acknowledgement = Event()
    originals = [store._foundation._client.evalsha for store in stores]
    ack_dispatches = 0
    try:
        def pause_after_acknowledgement_read(*args):
            nonlocal ack_dispatches
            is_retrieval = args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1
            if is_retrieval and b"read" in args:
                result = originals[0](*args)
                acknowledgement_read.set()
                assert release_acknowledgement.wait(timeout=2)
                return result
            if is_retrieval and b"ack" in args:
                ack_dispatches += 1
                dispatch_barrier.wait()
            return originals[0](*args)

        def synchronize_expiry_read(*args):
            if args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1 and b"read" in args:
                dispatch_barrier.wait()
            return originals[1](*args)

        stores[0]._foundation._client.evalsha = pause_after_acknowledgement_read
        stores[1]._foundation._client.evalsha = synchronize_expiry_read
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            acknowledge = pool.submit(
                stores[0].retrieve_encrypted_response,
                *target[1],
                target[2],
                target_ready.acknowledgement_token,
            )
            assert acknowledgement_read.wait(timeout=2)
            while True:
                current_seconds, current_micros = stores[1]._foundation.server_time()
                if current_seconds + current_micros / 1_000_000 >= float(boundary_raw):
                    break
            read = pool.submit(
                stores[1].retrieve_encrypted_response, *target[1], target[2]
            )
            release_acknowledgement.set()
            assert [
                read.result(timeout=3).state,
                acknowledge.result(timeout=3).state,
            ] == ["retrieval_expired", "retrieval_expired"]

        assert ack_dispatches == 1
        assert datastore.exists(target[5][7]) == 0
        assert datastore.zscore(target[5][8], target[6]) is None
        assert datastore.zrange(target[5][8], 0, -1, withscores=True) == [
            entry for entry in response_expiry_before if entry[0] != target[6].encode()
        ]
        terminal = datastore.hgetall(target[5][9])
        assert terminal[b"retrieval_state"] == b"retrieval_expired"
        assert terminal == {
            **terminal_before,
            b"retrieval_state": b"retrieval_expired",
        }
        assert (
            tuple(datastore.dump(unrelated[5][index]) for index in (3, 4, 7, 9, 11)),
            tuple(
                datastore.zscore(unrelated[5][index], unrelated[6])
                for index in (5, 6, 8, 10)
            ),
        ) == unrelated_before
        stores[1]._foundation._client.evalsha = originals[1]
        assert stores[1].retrieve_encrypted_response(
            *unrelated[1], unrelated[2]
        ).envelope == unrelated[3]
    finally:
        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        _delete_claim_fixture_state(
            stores[0],
            (target[0], unrelated[0]),
            (target[1], unrelated[1]),
        )
        for store in stores:
            store.close()


def test_response_retrieval_cross_identity_credentials_and_tokens_are_fixed_and_non_mutating(
    valkey_server,
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    first = _accepted_retrieval_fixture((store,), "cross-authority-a")
    second = _accepted_retrieval_fixture((store,), "cross-authority-b")
    fixtures = (first, second)
    try:
        ready = [
            store.retrieve_encrypted_response(*item[1], item[2]) for item in fixtures
        ]
        snapshots = [_exact_key_snapshot(store, item[5]) for item in fixtures]
        assert store.retrieve_encrypted_response(
            *first[1], second[2]
        ).state == "invalid_retrieval_credential"
        assert store.retrieve_encrypted_response(
            *first[1], first[2], ready[1].acknowledgement_token
        ).state == "invalid_acknowledgement"
        assert [
            _exact_key_snapshot(store, item[5]) for item in fixtures
        ] == snapshots
        assert store.retrieve_encrypted_response(*first[1], first[2]).state == "response_ready"
    finally:
        _delete_claim_fixture_state(store, tuple(item[0] for item in fixtures), tuple(item[1] for item in fixtures))
        store.close()


def test_response_replay_expiry_is_inclusive_by_authoritative_valkey_time(
    valkey_server,
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    fixture = _accepted_retrieval_fixture((store,), "inclusive-expiry")
    node, identity, credential, envelope, _, keys, member = fixture
    datastore = store._foundation._client
    try:
        seconds, micros = store._foundation.server_time()
        before_boundary = seconds + micros / 1_000_000 + 10
        raw = format(before_boundary, ".17g")
        datastore.hset(keys[7], "replay_expires_at_epoch", raw)
        datastore.hset(keys[9], "replay_expires_at_epoch", raw)
        datastore.zadd(keys[8], {member: float(raw)})
        assert store.retrieve_encrypted_response(*identity, credential).envelope == envelope

        seconds, micros = store._foundation.server_time()
        boundary = seconds + micros / 1_000_000
        raw = format(boundary, ".17g")
        datastore.hset(keys[7], "replay_expires_at_epoch", raw)
        datastore.hset(keys[9], "replay_expires_at_epoch", raw)
        datastore.zadd(keys[8], {member: float(raw)})
        result = store.retrieve_encrypted_response(*identity, credential)
        assert result.state == "retrieval_expired"
        assert datastore.exists(keys[7]) == 0
        assert datastore.zscore(keys[8], member) is None
        terminal = datastore.hgetall(keys[9])
        assert terminal[b"outcome"] == b"completed"
        assert terminal[b"retrieval_state"] == b"retrieval_expired"
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize(
    "manifest_value",
    (
        _manifest(reader_min=2, reader_max=2),
        _manifest(writer_min=2, writer_max=2, active_writer_revision=2),
    ),
)
def test_response_retrieval_schema_gates_precede_protocol_dispatch(
    valkey_server, manifest_value
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    fixture = _accepted_retrieval_fixture((store,), "retrieval-schema")
    node, identity, credential, _, _, keys, _ = fixture
    schema = store._foundation.config.key("schema")
    original = store._foundation._client.evalsha
    dispatches = []
    try:
        baseline = _exact_key_snapshot(store, keys)
        store._foundation._client.set(schema, manifest_value.encode())

        def record(*args):
            dispatches.append(args[0])
            return original(*args)

        store._foundation._client.evalsha = record
        with pytest.raises(ValkeySchemaIncompatibleError, match="^state schema incompatible$"):
            store.retrieve_encrypted_response(*identity, credential)
        assert RETRIEVE_RESPONSE_SCRIPT.eval_sha1 not in dispatches
        assert _exact_key_snapshot(store, keys) == baseline
    finally:
        store._foundation._client.evalsha = original
        store._foundation._client.set(schema, _manifest().encode())
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()

def test_encrypted_response_acceptance_is_atomic_shared_and_replay_safe(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    node, owner, consumer = "response-node", _digest("response-owner"), "consumer-a"
    identity = ("response-client", "response-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "response-ciphertext", "response-key", "response-iv"
    )
    deadline = time.time() + 60
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, deadline)
        claim = first.claim_queued_request(node, owner, consumer)

        accepted = second.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )
        node_digest = first._node_digest(node)
        first._foundation._client.delete(
            first._foundation.config.key("node", node_digest)
        )
        first._foundation._client.zrem(
            first._foundation.config.key("nodes:lease"), node_digest
        )
        replay = first.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )
        assert accepted.new_outcome is True
        assert dataclasses.replace(accepted, new_outcome=False) == replay
        with pytest.raises(RelayStateConflict, match="response lifecycle conflict"):
            first.accept_encrypted_response(
                node,
                owner,
                consumer,
                *identity,
                claim.generation,
                dataclasses.replace(response, ciphertext="different-ciphertext"),
            )
        assert len(first.response_records()) == len(first.terminal_records()) == 1
        assert first.response_records()[0].envelope == response
        assert first.terminal_records()[0].acknowledgement_digest == (
            second.terminal_records()[0].acknowledgement_digest
        )
        assert first.active_claims(node) == ()
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("request_limit", "response_limit", "request_ciphertext", "response_ciphertext"),
    (
        (256, 2048, "request-ciphertext", "r" * 512),
        (2048, 256, "q" * 512, "response-ciphertext"),
    ),
)
def test_encrypted_response_retrieval_is_replayable_shared_and_acknowledged_once(
    valkey_server,
    request_limit,
    response_limit,
    request_ciphertext,
    response_ciphertext,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server,
        namespace,
        max_envelope_bytes=request_limit,
        max_response_envelope_bytes=response_limit,
    )
    second = _registration_store(
        valkey_server,
        namespace,
        max_envelope_bytes=request_limit,
        max_response_envelope_bytes=response_limit,
    )
    node, owner, consumer = "retrieve-node", _digest("retrieve-owner"), "consumer"
    identity = ("retrieve-client", "retrieve-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, response_ciphertext, "response-key", "response-iv"
    )
    rounded_deadline = float(f"{time.time() + 60:.6f}")
    deadline = math.nextafter(rounded_deadline, math.inf)
    assert deadline != float(f"{deadline:.6f}")
    try:
        first.register(node, _capabilities(), owner)
        selection = first.select_and_reserve(
            *identity, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        assert selection.request_deadline_epoch == deadline
        first.enqueue_encrypted_request(
            *identity,
            selection.reservation_token,
            node,
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            EncryptedRequestEnvelope(
                "tokenplace_api_v1_relay_e2ee", 1, request_ciphertext, "request-key", "request-iv"
            ),
            "cancel",
        )
        claim = first.claim_queued_request(node, owner, consumer)
        assert claim.request_deadline_epoch == deadline
        accepted = first.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )

        initial = first.retrieve_encrypted_response(
            *identity, selection.reservation_token
        )
        replay = second.retrieve_encrypted_response(
            *identity, selection.reservation_token
        )
        assert initial == replay
        assert initial.state == "response_ready"
        assert initial.envelope == response
        assert initial.request_deadline_epoch == deadline
        assert initial.replay_expires_at_epoch == accepted.replay_expires_at_epoch
        assert initial.acknowledgement_token is not None
        assert first.retrieve_encrypted_response(
            *identity, "0" * 64
        ).state == "invalid_retrieval_credential"
        assert first.retrieve_encrypted_response(
            *identity, selection.reservation_token, "f" * 64
        ).state == "invalid_acknowledgement"
        assert first.response_records()[0].envelope == response

        acknowledged = second.retrieve_encrypted_response(
            *identity,
            selection.reservation_token,
            initial.acknowledgement_token,
        )
        duplicate = first.retrieve_encrypted_response(
            *identity,
            selection.reservation_token,
            initial.acknowledgement_token,
        )
        assert acknowledged.state == duplicate.state == "acknowledged"
        assert first.response_records() == ()
        terminal = first.terminal_records()[0]
        assert terminal.outcome == "completed"
        assert terminal.retrieval_state == "acknowledged"
        keys, member = _response_acceptance_authority(first, node, identity)
        assert first._foundation._client.zscore(keys[8], member) is None

        rotated = ValkeyRegistrationStore(
            _foundation(valkey_server, namespace),
            RelayStateStoreConfig(
                namespace="testing.valkey",
                max_envelope_bytes=request_limit,
                max_response_envelope_bytes=response_limit,
            ),
            acknowledgement_key=b"rotated-test-acknowledgement-key!",
        )
        try:
            assert rotated.retrieve_encrypted_response(
                *identity,
                selection.reservation_token,
                initial.acknowledgement_token,
            ).state == "invalid_acknowledgement"
        finally:
            rotated.close()

        client_digest, request_digest = first._identity(*identity)
        terminal_key = first._foundation.config.key(
            "terminal", client_digest, request_digest
        )
        request_key = first._foundation.config.key(
            "request", client_digest, request_digest
        )
        expired_at = time.time() - 1
        expired_accepted = float(f"{expired_at - 2:.6f}")
        first._foundation._client.hset(
            terminal_key,
            mapping={
                "accepted_at_epoch": repr(expired_accepted),
                "replay_expires_at_epoch": format(expired_at - 1, ".17g"),
                "expires_at_epoch": format(expired_at, ".17g"),
            },
        )
        first._foundation._client.zadd(
            first._foundation.config.key("terminals:expiry"),
            {f"{client_digest}:{request_digest}": expired_at},
        )
        assert first.retrieve_encrypted_response(
            *identity, selection.reservation_token
        ).state == "invalid_retrieval_credential"
        assert first._foundation._client.exists(terminal_key, request_key) == 0
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("retrieval_state", "corruption"),
    (
        ("response_ready", "partial_response"),
        ("response_ready", "numeric_text_mismatch"),
        ("acknowledged", "unexpected_response"),
        ("retrieval_expired", "unexpected_expiry_member"),
    ),
)
def test_due_response_retrieval_validates_all_authority_before_cleanup(
    valkey_server, retrieval_state, corruption
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    node, owner, consumer = "due-retrieval-node", _digest("due-owner"), "consumer"
    identity = ("due-client", f"due-{retrieval_state}-{corruption}")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    keys, member = _response_acceptance_authority(store, node, identity)
    datastore = store._foundation._client
    unrelated_member = f"{'f' * 64}:{'e' * 64}"
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        accepted = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        credential = "a" * 64
        retrieval_digest = hashlib.sha256(credential.encode()).hexdigest()
        datastore.hset(keys[4], "token_digest", retrieval_digest)
        datastore.hset(keys[9], "retrieval_credential_digest", retrieval_digest)
        ready = store.retrieve_encrypted_response(*identity, credential)

        if retrieval_state == "acknowledged":
            store.retrieve_encrypted_response(
                *identity, credential, ready.acknowledgement_token
            )
        elif retrieval_state == "retrieval_expired":
            datastore.hset(keys[9], "retrieval_state", "retrieval_expired")
            datastore.delete(keys[7])
            datastore.zrem(keys[8], member)

        now = sum(value / divisor for value, divisor in zip(store._foundation.server_time(), (1, 1_000_000)))
        accepted_raw = format(now - 3, ".17g")
        replay_raw = format(now - 2, ".17g")
        expiry_raw = format(now - 1, ".17g")
        datastore.hset(
            keys[9],
            mapping={
                "accepted_at_epoch": accepted_raw,
                "replay_expires_at_epoch": replay_raw,
                "expires_at_epoch": expiry_raw,
            },
        )
        datastore.zadd(keys[10], {member: float(expiry_raw), unrelated_member: now + 99})
        if retrieval_state == "response_ready":
            datastore.hset(
                keys[7],
                mapping={
                    "accepted_at_epoch": accepted_raw,
                    "replay_expires_at_epoch": replay_raw,
                },
            )
            datastore.zadd(keys[8], {member: float(replay_raw), unrelated_member: now + 98})
        else:
            datastore.zadd(keys[8], {unrelated_member: now + 98})

        if corruption == "partial_response":
            datastore.hdel(keys[7], "response_digest")
        elif corruption == "numeric_text_mismatch":
            datastore.hset(keys[7], "accepted_at_epoch", f"{float(accepted_raw):.16e}")
        elif corruption == "unexpected_response":
            datastore.hset(keys[7], mapping={"unexpected": "present"})
        elif corruption == "unexpected_expiry_member":
            datastore.zadd(keys[8], {member: float(replay_raw)})

        before = _exact_key_snapshot(store, keys)
        with pytest.raises(ValkeySchemaIncompatibleError, match="state schema incompatible"):
            store.retrieve_encrypted_response(*identity, credential)
        assert _exact_key_snapshot(store, keys) == before
        assert datastore.zscore(keys[8], unrelated_member) == pytest.approx(now + 98)
        assert datastore.zscore(keys[10], unrelated_member) == pytest.approx(now + 99)
        assert accepted.replay_expires_at_epoch > accepted.accepted_at_epoch
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        datastore.zrem(keys[8], unrelated_member)
        datastore.zrem(keys[10], unrelated_member)
        store.close()


def test_coherent_due_response_retrieval_reaps_only_addressed_authority(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node, owner, consumer = "due-cleanup-node", _digest("due-cleanup-owner"), "consumer"
    identity = ("due-cleanup-client", "due-cleanup-request")
    keys, member = _response_acceptance_authority(store, node, identity)
    datastore = store._foundation._client
    unrelated_member = f"{'d' * 64}:{'c' * 64}"
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store.accept_encrypted_response(
            node,
            owner,
            consumer,
            *identity,
            claim.generation,
            EncryptedResponseEnvelope(
                "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
            ),
        )
        credential = "b" * 64
        retrieval_digest = hashlib.sha256(credential.encode()).hexdigest()
        datastore.hset(keys[4], "token_digest", retrieval_digest)
        datastore.hset(keys[9], "retrieval_credential_digest", retrieval_digest)
        now = sum(value / divisor for value, divisor in zip(store._foundation.server_time(), (1, 1_000_000)))
        accepted_raw = repr(float(f"{now - 3:.6f}"))
        replay_raw, expiry_raw = (format(now - offset, ".17g") for offset in (2, 1))
        datastore.hset(keys[7], mapping={"accepted_at_epoch": accepted_raw, "replay_expires_at_epoch": replay_raw})
        datastore.hset(keys[9], mapping={"accepted_at_epoch": accepted_raw, "replay_expires_at_epoch": replay_raw, "expires_at_epoch": expiry_raw})
        datastore.zadd(keys[8], {member: float(replay_raw), unrelated_member: now + 98})
        datastore.zadd(keys[10], {member: float(expiry_raw), unrelated_member: now + 99})

        result = store.retrieve_encrypted_response(*identity, credential)
        assert result.state == "invalid_retrieval_credential"
        assert datastore.exists(keys[4], keys[7], keys[9]) == 0
        assert datastore.zscore(keys[8], member) is None
        assert datastore.zscore(keys[10], member) is None
        assert datastore.zscore(keys[8], unrelated_member) == pytest.approx(now + 98)
        assert datastore.zscore(keys[10], unrelated_member) == pytest.approx(now + 99)
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        datastore.zrem(keys[8], unrelated_member)
        datastore.zrem(keys[10], unrelated_member)
        store.close()


def _overprecise_equivalent_float(raw: str) -> str:
    """Return a distinct decimal spelling with the same binary64 value."""
    mantissa, separator, exponent = raw.partition("e")
    if "." not in mantissa:
        mantissa += ".0"
    for width in range(1, 17):
        for digit in "123456789":
            candidate = f"{mantissa}{'0' * (width - 1)}{digit}"
            if separator:
                candidate = f"{candidate}{separator}{exponent}"
            if candidate != raw and float(candidate) == float(raw):
                return candidate
    raise AssertionError("could not construct overprecise equivalent timestamp")


@pytest.mark.parametrize(
    "field",
    (
        "accepted_at_epoch",
        "replay_expires_at_epoch",
        "expires_at_epoch",
        "deadline",
    ),
)
def test_response_retrieval_rejects_coordinated_noncanonical_timestamp_authority(
    valkey_server, field
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    fixture = _accepted_retrieval_fixture(
        (store,), f"canonical-authority-{field}"
    )
    node, identity, credential, _, _, keys, member = fixture
    datastore = store._foundation._client
    unrelated_member = f"{'9' * 64}:{'8' * 64}"
    try:
        source_key = keys[4] if field == "deadline" else keys[9]
        raw = datastore.hget(source_key, field).decode()
        corrupted = _overprecise_equivalent_float(raw)

        if field in {"accepted_at_epoch", "replay_expires_at_epoch"}:
            datastore.hset(keys[7], field, corrupted)
            datastore.hset(keys[9], field, corrupted)
        else:
            datastore.hset(source_key, field, corrupted)
        datastore.zadd(keys[8], {unrelated_member: float(raw) + 100})
        datastore.zadd(keys[10], {unrelated_member: float(raw) + 200})
        before = _exact_key_snapshot(store, keys)

        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ):
            store.retrieve_encrypted_response(*identity, credential)

        assert _exact_key_snapshot(store, keys) == before
        assert datastore.zscore(keys[8], unrelated_member) == pytest.approx(
            float(raw) + 100
        )
        assert datastore.zscore(keys[10], unrelated_member) == pytest.approx(
            float(raw) + 200
        )
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        datastore.zrem(keys[8], unrelated_member)
        datastore.zrem(keys[10], unrelated_member)
        store.close()


def test_response_acknowledgement_revalidates_canonical_authority_after_read(
    valkey_server,
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    fixture = _accepted_retrieval_fixture((store,), "canonical-ack-dispatch")
    node, identity, credential, envelope, _, keys, member = fixture
    datastore = store._foundation._client
    ready = store.retrieve_encrypted_response(*identity, credential)
    original_evalsha = datastore.evalsha
    dispatches = 0
    corrupted_snapshot = None
    canonical_replay = datastore.hget(keys[9], "replay_expires_at_epoch")
    unrelated_member = f"{'7' * 64}:{'6' * 64}"
    datastore.zadd(keys[8], {unrelated_member: time.time() + 100})
    datastore.zadd(keys[10], {unrelated_member: time.time() + 200})
    try:
        def corrupt_before_ack(*args, **kwargs):
            nonlocal dispatches, corrupted_snapshot
            if args[0] == RETRIEVE_RESPONSE_SCRIPT.eval_sha1:
                dispatches += 1
                if dispatches == 2:
                    raw = datastore.hget(keys[9], "replay_expires_at_epoch").decode()
                    corrupted = _overprecise_equivalent_float(raw)
                    datastore.hset(keys[7], "replay_expires_at_epoch", corrupted)
                    datastore.hset(keys[9], "replay_expires_at_epoch", corrupted)
                    corrupted_snapshot = _exact_key_snapshot(store, keys)
            return original_evalsha(*args, **kwargs)

        datastore.evalsha = corrupt_before_ack
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ):
            store.retrieve_encrypted_response(
                *identity, credential, ready.acknowledgement_token
            )

        assert dispatches == 2
        assert corrupted_snapshot is not None
        assert _exact_key_snapshot(store, keys) == corrupted_snapshot
        assert datastore.exists(keys[7]) == 1
        assert datastore.zscore(keys[8], member) is not None
        datastore.evalsha = original_evalsha
        datastore.hset(
            keys[7],
            "replay_expires_at_epoch",
            canonical_replay,
        )
        datastore.hset(
            keys[9],
            "replay_expires_at_epoch",
            canonical_replay,
        )
        assert store.retrieve_encrypted_response(*identity, credential).envelope == envelope
    finally:
        datastore.evalsha = original_evalsha
        _delete_claim_fixture_state(store, (node,), (identity,))
        datastore.zrem(keys[8], unrelated_member)
        datastore.zrem(keys[10], unrelated_member)
        store.close()


def test_response_retrieval_canonical_timestamp_authority_acknowledges(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    fixture = _accepted_retrieval_fixture((store,), "canonical-control")
    node, identity, credential, envelope, _, _, _ = fixture
    try:
        ready = store.retrieve_encrypted_response(*identity, credential)
        assert ready.envelope == envelope
        assert store.retrieve_encrypted_response(
            *identity, credential, ready.acknowledgement_token
        ).state == "acknowledged"
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize(
    ("manifest", "expected_dispatches"),
    (
        (_manifest(reader_min=2, reader_max=2), ()),
        (
            _manifest(writer_min=2, writer_max=2, active_writer_revision=2),
            (SERVER_TIME_SCRIPT.eval_sha1,),
        ),
        (
            _manifest(
                script_digests={
                    **SCRIPT_DIGESTS,
                    ACCEPT_RESPONSE_SCRIPT.name: "0" * 64,
                }
            ),
            (),
        ),
    ),
    ids=("reader", "writer", "acceptance-script-digest"),
)
def test_encrypted_response_compatibility_gates_fail_closed_before_state_or_acceptance_script(
    valkey_server, manifest, expected_dispatches
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node, owner, consumer = (
        "acceptance-compatibility-node",
        _digest("acceptance-compatibility-owner"),
        "acceptance-compatibility-consumer",
    )
    identity = ("acceptance-compatibility-client", "acceptance-compatibility-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    schema_key = cfg.key("schema")
    original_evalsha = store._foundation._client.evalsha
    keys, member = _response_acceptance_authority(store, node, identity)
    try:
        store.register(node, _capabilities(), owner)
        deadline = store._foundation.server_time()[0] + 60
        _enqueue_claim_fixture(store, node, owner, *identity, deadline)
        claim = store.claim_queued_request(node, owner, consumer)
        store._foundation.server_time()
        baseline = _exact_key_snapshot(store, keys)
        dispatches = []

        def record_dispatch(*args, **kwargs):
            dispatches.append(args[0])
            return original_evalsha(*args, **kwargs)

        store._foundation._client.set(schema_key, manifest.encode())
        store._foundation._client.evalsha = record_dispatch
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ):
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, response
            )

        assert tuple(dispatches) == expected_dispatches
        assert ACCEPT_RESPONSE_SCRIPT.eval_sha1 not in dispatches
        assert _exact_key_snapshot(store, keys) == baseline
        assert store._foundation._client.exists(keys[7], keys[9]) == 0
        assert store._foundation._client.zscore(keys[8], member) is None
        assert store._foundation._client.zscore(keys[10], member) is None
    finally:
        store._foundation._client.evalsha = original_evalsha
        store._foundation._client.set(schema_key, _manifest().encode())
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_encrypted_response_namespace_isolation(valkey_server):
    stores = [
        _registration_store(valkey_server, uuid.uuid4().hex) for _ in range(2)
    ]
    node, owner, consumer = (
        "namespace-response-node",
        _digest("namespace-response-owner"),
        "namespace-response-consumer",
    )
    identity = ("namespace-response-client", "namespace-response-request")
    envelopes = (
        EncryptedResponseEnvelope(
            "tokenplace_api_v1_relay_e2ee", 1, "ciphertext-a", "key-a", "iv-a"
        ),
        EncryptedResponseEnvelope(
            "tokenplace_api_v1_relay_e2ee", 1, "ciphertext-b", "key-b", "iv-b"
        ),
    )
    authorities = [
        _response_acceptance_authority(store, node, identity) for store in stores
    ]
    try:
        assert set(authorities[0][0]).isdisjoint(authorities[1][0])
        for store, (keys, _) in zip(stores, authorities):
            assert all(
                key.startswith(store._foundation.config.key_prefix) for key in keys
            )

        claims = []
        for store in stores:
            store.register(node, _capabilities(), owner)
            deadline = store._foundation.server_time()[0] + 60
            _enqueue_claim_fixture(store, node, owner, *identity, deadline)
            claims.append(store.claim_queued_request(node, owner, consumer))

        before = [
            _exact_key_snapshot(store, authority[0])
            for store, authority in zip(stores, authorities)
        ]
        accepted = stores[0].accept_encrypted_response(
            node, owner, consumer, *identity, claims[0].generation, envelopes[0]
        )
        assert accepted.new_outcome is True
        assert _exact_key_snapshot(stores[1], authorities[1][0]) == before[1]
        assert stores[1]._foundation._client.exists(
            authorities[1][0][7], authorities[1][0][9]
        ) == 0
        assert stores[1]._foundation._client.zscore(
            authorities[1][0][8], authorities[1][1]
        ) is None
        assert stores[1]._foundation._client.zscore(
            authorities[1][0][10], authorities[1][1]
        ) is None

        namespace_a = _exact_key_snapshot(stores[0], authorities[0][0])
        accepted_other = stores[1].accept_encrypted_response(
            node, owner, consumer, *identity, claims[1].generation, envelopes[1]
        )
        assert accepted_other.new_outcome is True
        assert _exact_key_snapshot(stores[0], authorities[0][0]) == namespace_a

        namespace_b = _exact_key_snapshot(stores[1], authorities[1][0])
        response_records = stores[0].response_records()
        terminal_records = stores[0].terminal_records()
        assert _exact_key_snapshot(stores[1], authorities[1][0]) == namespace_b
        assert len(response_records) == len(terminal_records) == 1
        assert response_records[0].envelope == envelopes[0]
        assert (
            terminal_records[0].response_digest == response_records[0].response_digest
        )

        namespace_a = _exact_key_snapshot(stores[0], authorities[0][0])
        response_records = stores[1].response_records()
        terminal_records = stores[1].terminal_records()
        assert _exact_key_snapshot(stores[0], authorities[0][0]) == namespace_a
        assert len(response_records) == len(terminal_records) == 1
        assert response_records[0].envelope == envelopes[1]
        assert (
            terminal_records[0].response_digest == response_records[0].response_digest
        )

        completed = [
            _exact_key_snapshot(store, authority[0])
            for store, authority in zip(stores, authorities)
        ]
        for index, store in enumerate(stores):
            replay = store.accept_encrypted_response(
                node,
                owner,
                consumer,
                *identity,
                claims[index].generation,
                envelopes[index],
            )
            assert replay == dataclasses.replace(
                (accepted, accepted_other)[index], new_outcome=False
            )
        assert [
            _exact_key_snapshot(store, authority[0])
            for store, authority in zip(stores, authorities)
        ] == completed
    finally:
        for store in stores:
            _delete_claim_fixture_state(store, (node,), (identity,))
            store.close()


@pytest.mark.parametrize(
    ("backend_error", "expected_error", "expected_message"),
    (
        (
            redis.ConnectionError,
            ValkeyUnavailableError,
            "state backend unavailable",
        ),
        (
            lambda message: redis.ResponseError(f"READONLY {message}"),
            ValkeyReadOnlyError,
            "state backend is not writable",
        ),
        (
            redis.ResponseError,
            ValkeyUnavailableError,
            "state backend command failed",
        ),
    ),
    ids=("connection", "readonly", "command"),
)
def test_encrypted_response_backend_failures_are_typed_redacted_and_not_replayed(
    valkey_server, caplog, backend_error, expected_error, expected_message
):
    namespace = f"acceptance-failure-{uuid.uuid4().hex}"
    store = _registration_store(valkey_server, namespace)
    node = "acceptance-failure-node-marker"
    owner = _digest("acceptance-failure-owner-marker")
    consumer = "acceptance-failure-consumer-marker"
    identity = (
        "acceptance-failure-client-marker",
        "acceptance-failure-request-marker",
    )
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee",
        1,
        "acceptance-failure-ciphertext-marker",
        "acceptance-failure-cipher-key-marker",
        "acceptance-failure-iv-marker",
    )
    keys, member = _response_acceptance_authority(store, node, identity)
    original_evalsha = store._foundation._client.evalsha
    dispatches = []
    endpoint_marker = "acceptance-failure-private-endpoint-marker"
    acknowledgement_marker = _ACKNOWLEDGEMENT_KEY.decode()
    markers = (
        endpoint_marker,
        namespace,
        *keys,
        node,
        owner,
        consumer,
        *identity,
        envelope.ciphertext,
        envelope.cipherkey,
        envelope.iv,
        acknowledgement_marker,
    )
    try:
        store.register(node, _capabilities(), owner)
        deadline = store._foundation.server_time()[0] + 60
        _enqueue_claim_fixture(store, node, owner, *identity, deadline)
        claim = store.claim_queued_request(node, owner, consumer)
        store._foundation._client.script_load(ACCEPT_RESPONSE_SCRIPT.source)
        before = _exact_key_snapshot(store, keys)

        def fail_acceptance(*args, **kwargs):
            if args[0] != ACCEPT_RESPONSE_SCRIPT.eval_sha1:
                return original_evalsha(*args, **kwargs)
            dispatches.append((args, kwargs))
            raise backend_error(" ".join(markers))

        store._foundation._client.evalsha = fail_acceptance
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(
                expected_error, match=f"^{expected_message}$"
            ) as caught:
                store.accept_encrypted_response(
                    node, owner, consumer, *identity, claim.generation, envelope
                )

        assert len(dispatches) == 1
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
        dispatched_args, dispatched_kwargs = dispatches[0]
        assert dispatched_kwargs == {}
        assert all(
            _ACKNOWLEDGEMENT_KEY not in (
                value if isinstance(value, bytes) else str(value).encode()
            )
            for value in dispatched_args[2:]
        )
        assert _exact_key_snapshot(store, keys) == before
        assert store._foundation._client.exists(keys[7], keys[9]) == 0
        assert store._foundation._client.zscore(keys[8], member) is None
        assert store._foundation._client.zscore(keys[10], member) is None
    finally:
        store._foundation._client.evalsha = original_evalsha
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_encrypted_response_committed_lost_reply_is_recovered_without_replay(
    valkey_server, caplog
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    node, owner, consumer = "lost-reply-node", _digest("lost-reply-owner"), "consumer"
    identity = ("lost-reply-client", "lost-reply-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee",
        1,
        "lost-reply-ciphertext-marker",
        "lost-reply-key-marker",
        "lost-reply-iv-marker",
    )
    keys, member = _response_acceptance_authority(first, node, identity)
    cfg = first._foundation.config
    response_key, terminal_key = keys[7], keys[9]
    original_evalsha = first._foundation._client.evalsha
    dispatches = 0
    markers = (
        "private-endpoint-marker",
        response_key,
        terminal_key,
        node,
        owner,
        consumer,
        *identity,
        envelope.ciphertext,
        envelope.cipherkey,
        envelope.iv,
    )
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        first._foundation._client.script_load(ACCEPT_RESPONSE_SCRIPT.source)

        def lose_acceptance_reply(*args):
            nonlocal dispatches
            result = original_evalsha(*args)
            if args[0] == ACCEPT_RESPONSE_SCRIPT.eval_sha1:
                dispatches += 1
                raise redis.ConnectionError(" ".join(markers))
            return result

        first._foundation._client.evalsha = lose_acceptance_reply
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(
                ValkeyUnavailableError, match="^state backend unavailable$"
            ) as caught:
                first.accept_encrypted_response(
                    node, owner, consumer, *identity, claim.generation, envelope
                )
        assert dispatches == 1
        assert caught.value.__cause__ is None
        rendered = "".join(
            (
                str(caught.value),
                repr(caught.value),
                "".join(traceback.format_exception(caught.value)),
                caplog.text,
            )
        )
        assert all(marker not in rendered for marker in markers)

        first._foundation._client.evalsha = original_evalsha
        committed = _exact_key_snapshot(first, keys)
        terminal = first._foundation._client.hgetall(terminal_key)
        recovered = second.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        assert recovered.new_outcome is False
        assert recovered.generation == claim.generation
        assert recovered.accepted_at_epoch == float(terminal[b"accepted_at_epoch"])
        assert recovered.replay_expires_at_epoch == float(
            terminal[b"replay_expires_at_epoch"]
        )
        assert _exact_key_snapshot(first, keys) == committed
        assert first._foundation._client.zscore(cfg.key("responses:expiry"), member)
        assert first._foundation._client.zscore(cfg.key("terminals:expiry"), member)
    finally:
        first._foundation._client.evalsha = original_evalsha
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


def test_encrypted_response_concurrent_identical_acceptance_has_one_outcome(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    stores = (
        _registration_store(valkey_server, namespace),
        _registration_store(valkey_server, namespace),
    )
    first, second = stores
    node, owner, consumer = "identical-node", _digest("identical-owner"), "consumer"
    identity = ("identical-client", "identical-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "same-ciphertext", "same-key", "same-iv"
    )
    barrier = Barrier(2, timeout=2)
    originals = [store._foundation._client.evalsha for store in stores]
    dispatches = [0, 0]
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        for store in stores:
            store._foundation._client.script_load(ACCEPT_RESPONSE_SCRIPT.source)

        for index, store in enumerate(stores):
            original = originals[index]

            def synchronized(*args, index=index, original=original):
                if args[0] == ACCEPT_RESPONSE_SCRIPT.eval_sha1:
                    dispatches[index] += 1
                    barrier.wait()
                return original(*args)

            store._foundation._client.evalsha = synchronized

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    store.accept_encrypted_response,
                    node,
                    owner,
                    consumer,
                    *identity,
                    claim.generation,
                    envelope,
                )
                for store in stores
            ]
            results = [future.result(timeout=3) for future in futures]

        assert dispatches == [1, 1]
        assert sorted(result.new_outcome for result in results) == [False, True]
        assert dataclasses.replace(results[0], new_outcome=False) == dataclasses.replace(
            results[1], new_outcome=False
        )
        keys, member = _response_acceptance_authority(first, node, identity)
        assert len(first._foundation._client.hgetall(keys[7])) == 12
        assert len(first._foundation._client.hgetall(keys[9])) == 16
        assert first._foundation._client.zscore(keys[8], member) is not None
        assert first._foundation._client.zscore(keys[10], member) is not None
    finally:
        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


def test_encrypted_response_concurrent_conflicts_preserve_one_winner(valkey_server):
    namespace = uuid.uuid4().hex
    stores = (
        _registration_store(valkey_server, namespace),
        _registration_store(valkey_server, namespace),
    )
    first, second = stores
    node, owner, consumer = "conflict-node", _digest("conflict-owner"), "consumer"
    identity = ("conflict-client", "conflict-request")
    envelopes = (
        EncryptedResponseEnvelope(
            "tokenplace_api_v1_relay_e2ee", 1, "ciphertext-a", "key-a", "iv-a"
        ),
        EncryptedResponseEnvelope(
            "tokenplace_api_v1_relay_e2ee", 1, "ciphertext-b", "key-b", "iv-b"
        ),
    )
    barrier = Barrier(2, timeout=2)
    originals = [store._foundation._client.evalsha for store in stores]
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        for store in stores:
            store._foundation._client.script_load(ACCEPT_RESPONSE_SCRIPT.source)
        for store, original in zip(stores, originals):

            def synchronized(*args, original=original):
                if args[0] == ACCEPT_RESPONSE_SCRIPT.eval_sha1:
                    barrier.wait()
                return original(*args)

            store._foundation._client.evalsha = synchronized

        def accept(index):
            try:
                return stores[index].accept_encrypted_response(
                    node,
                    owner,
                    consumer,
                    *identity,
                    claim.generation,
                    envelopes[index],
                )
            except RelayStateConflict as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(accept, index) for index in range(2)]
            outcomes = [future.result(timeout=3) for future in futures]
        winner_index = next(
            index
            for index, outcome in enumerate(outcomes)
            if not isinstance(outcome, Exception)
        )
        loser_index = 1 - winner_index
        assert outcomes[winner_index].new_outcome is True
        assert str(outcomes[loser_index]) == "response lifecycle conflict"

        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        keys, member = _response_acceptance_authority(first, node, identity)
        authority = _exact_key_snapshot(first, keys)
        response = first._foundation._client.hgetall(keys[7])
        terminal = first._foundation._client.hgetall(keys[9])
        encoded_winner = first._serialized_response_envelope(envelopes[winner_index])
        assert response[b"envelope"] == encoded_winner
        assert response[b"response_digest"] == (
            hashlib.sha256(encoded_winner).hexdigest().encode()
        )
        assert terminal[b"response_digest"] == response[b"response_digest"]
        assert first._foundation._client.zscore(keys[8], member) == float(
            response[b"replay_expires_at_epoch"]
        )
        assert first._foundation._client.zscore(keys[10], member) == float(
            terminal[b"expires_at_epoch"]
        )

        replay = stores[winner_index].accept_encrypted_response(
            node,
            owner,
            consumer,
            *identity,
            claim.generation,
            envelopes[winner_index],
        )
        assert replay == dataclasses.replace(outcomes[winner_index], new_outcome=False)
        assert _exact_key_snapshot(first, keys) == authority
        with pytest.raises(RelayStateConflict, match="^response lifecycle conflict$"):
            stores[loser_index].accept_encrypted_response(
                node,
                owner,
                consumer,
                *identity,
                claim.generation,
                envelopes[loser_index],
            )
        assert _exact_key_snapshot(first, keys) == authority
    finally:
        for store, original in zip(stores, originals):
            store._foundation._client.evalsha = original
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


def _assert_redacted_acceptance_failure(caught, caplog, markers):
    rendered = "".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
            caplog.text,
        )
    )
    leaked = [str(marker) for marker in markers if str(marker) in rendered]
    assert leaked == []


@pytest.mark.parametrize(
    ("mismatch", "error", "message"),
    (
        ("node", RelayStateCredentialMismatch, "response owner is invalid"),
        ("owner", RelayStateCredentialMismatch, "response owner is invalid"),
        ("consumer", RelayStateCredentialMismatch, "response owner is invalid"),
        ("client", RelayStateConflict, "response lifecycle conflict"),
        ("request", RelayStateConflict, "response lifecycle conflict"),
        ("generation", RelayStateConflict, "response lifecycle conflict"),
    ),
)
def test_encrypted_response_wrong_authority_is_redacted_and_non_mutating(
    valkey_server, caplog, mismatch, error, message
):
    namespace = uuid.uuid4().hex
    stores = (
        _registration_store(valkey_server, namespace),
        _registration_store(valkey_server, namespace),
    )
    first, second = stores
    node, owner, consumer = "authority-node", _digest("authority-owner"), "consumer"
    identity = ("authority-client", "authority-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee",
        1,
        "authority-ciphertext",
        "authority-key",
        "authority-iv",
    )
    attempted = {
        "node": "supplied-wrong-node",
        "owner": _digest("supplied-wrong-owner"),
        "consumer": "supplied-wrong-consumer",
        "client": "supplied-wrong-client",
        "request": "supplied-wrong-request",
        "generation": 919191,
    }
    arguments = [node, owner, consumer, *identity, None, envelope]
    positions = {
        "node": 0,
        "owner": 1,
        "consumer": 2,
        "client": 3,
        "request": 4,
        "generation": 5,
    }
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        arguments[5] = claim.generation
        arguments[positions[mismatch]] = attempted[mismatch]
        attempted_node = arguments[0]
        attempted_identity = (arguments[3], arguments[4])
        valid_keys, valid_member = _response_acceptance_authority(first, node, identity)
        attempted_keys, attempted_member = _response_acceptance_authority(
            first, attempted_node, attempted_identity
        )
        before = (
            _exact_key_snapshot(first, valid_keys),
            _exact_key_snapshot(first, attempted_keys),
        )
        markers = (
            namespace,
            *arguments[:5],
            (
                attempted["generation"]
                if mismatch == "generation"
                else "supplied-generation-marker"
            ),
            envelope.ciphertext,
            envelope.cipherkey,
            envelope.iv,
            *valid_keys,
            *attempted_keys,
        )
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(error, match=f"^{message}$") as caught:
                second.accept_encrypted_response(*arguments)
        _assert_redacted_acceptance_failure(caught, caplog, markers)
        assert (
            _exact_key_snapshot(first, valid_keys),
            _exact_key_snapshot(first, attempted_keys),
        ) == before
        datastore = first._foundation._client
        for keys, member in (
            (valid_keys, valid_member),
            (attempted_keys, attempted_member),
        ):
            assert datastore.exists(keys[7], keys[9]) == 0
            assert datastore.zscore(keys[8], member) is None
            assert datastore.zscore(keys[10], member) is None
    finally:
        _delete_claim_fixture_state(
            first,
            (node, attempted["node"]),
            (
                identity,
                (attempted["client"], identity[1]),
                (identity[0], attempted["request"]),
            ),
        )
        for store in stores:
            store.close()


def test_encrypted_response_stale_generation_is_fenced_after_reclaim(valkey_server):
    namespace = uuid.uuid4().hex
    stores = tuple(
        _registration_store(valkey_server, namespace, claim_ttl_seconds=0.05)
        for _ in range(2)
    )
    first, second = stores
    node, owner, identity = (
        "reclaim-response-node",
        _digest("reclaim-response-owner"),
        ("reclaim-client", "reclaim-request"),
    )
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee",
        1,
        "reclaim-ciphertext",
        "reclaim-key",
        "reclaim-iv",
    )
    keys, _ = _response_acceptance_authority(first, node, identity)
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        old = first.claim_queued_request(node, owner, "old-consumer")
        _wait_for_server_epoch(first, old.lease_expires_at_epoch)
        current = second.claim_queued_request(node, owner, "current-consumer")
        assert current.state == "reclaimed" and current.generation > old.generation

        before = _exact_key_snapshot(first, keys)
        with pytest.raises(RelayStateConflict, match="^response lifecycle conflict$"):
            first.accept_encrypted_response(
                node, owner, "old-consumer", *identity, old.generation, envelope
            )
        assert _exact_key_snapshot(first, keys) == before
        accepted = second.accept_encrypted_response(
            node, owner, "current-consumer", *identity, current.generation, envelope
        )
        assert (
            accepted.new_outcome is True and accepted.generation == current.generation
        )
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        for store in stores:
            store.close()


@pytest.mark.parametrize("removal", ("unregister", "expiry"))
def test_encrypted_response_node_id_reuse_fences_old_claim(valkey_server, removal):
    namespace = uuid.uuid4().hex
    stores = tuple(
        _registration_store(valkey_server, namespace, lease_ttl_seconds=0.05)
        for _ in range(2)
    )
    first, second = stores
    node = "acceptance-reused-node"
    old_owner, new_owner = _digest("acceptance-old-owner"), _digest(
        "acceptance-new-owner"
    )
    identities = (("reuse-client", "old-request"), ("reuse-client", "fresh-request"))
    old_envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "old-ciphertext", "old-key", "old-iv"
    )
    fresh_envelope = dataclasses.replace(old_envelope, ciphertext="fresh-ciphertext")
    old_keys, _ = _response_acceptance_authority(first, node, identities[0])
    try:
        registration = first.register(node, _capabilities(), old_owner)
        _enqueue_claim_fixture(first, node, old_owner, *identities[0], time.time() + 60)
        old = second.claim_queued_request(node, old_owner, "old-consumer")
        if removal == "unregister":
            assert first.unregister(node, old_owner)
        else:
            _wait_for_server_epoch(first, registration.lease_expires_at_epoch)
        second.register(node, _capabilities(), new_owner)

        for attempted_owner in (old_owner, new_owner):
            before = _exact_key_snapshot(first, old_keys)
            with pytest.raises(
                RelayStateCredentialMismatch, match="^response owner is invalid$"
            ):
                second.accept_encrypted_response(
                    node,
                    attempted_owner,
                    "old-consumer",
                    *identities[0],
                    old.generation,
                    old_envelope,
                )
            assert _exact_key_snapshot(first, old_keys) == before

        _enqueue_claim_fixture(
            second, node, new_owner, *identities[1], time.time() + 60
        )
        fresh = first.claim_queued_request(node, new_owner, "fresh-consumer")
        accepted = second.accept_encrypted_response(
            node,
            new_owner,
            "fresh-consumer",
            *identities[1],
            fresh.generation,
            fresh_envelope,
        )
        assert accepted.new_outcome is True
        assert second.response_records()[0].envelope == fresh_envelope
    finally:
        _delete_claim_fixture_state(first, (node,), identities)
        for store in stores:
            store.close()


def test_encrypted_response_retries_retained_retrieval_expired_terminal(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=0.1,
        terminal_retention_seconds=30,
        control_tombstone_ttl_seconds=30,
    )
    second = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=0.1,
        terminal_retention_seconds=30,
        control_tombstone_ttl_seconds=30,
    )
    node, owner, consumer = "expired-node", _digest("expired-owner"), "consumer"
    identity = ("expired-client", "expired-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = first._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    node_digest = first._node_digest(node)
    datastore = first._foundation._client
    declared_keys = (
        cfg.key("nodes:lease"),
        cfg.key("node", node_digest),
        cfg.key("queue", node_digest),
        cfg.key("claim", client, request),
        cfg.key("request", client, request),
        cfg.key("claims:expiry"),
        cfg.key("requests:deadline"),
        cfg.key("response", client, request),
        cfg.key("responses:expiry"),
        cfg.key("terminal", client, request),
        cfg.key("terminals:expiry"),
        cfg.key("progress", client, request),
    )

    def snapshot():
        return tuple(datastore.dump(key) for key in declared_keys)

    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        accepted = first.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )

        for _ in range(200):
            seconds, micros = first._foundation.server_time()
            if seconds + micros / 1_000_000 >= accepted.replay_expires_at_epoch:
                break
            time.sleep(0.01)
        else:
            pytest.fail("authoritative Valkey time did not reach response expiry")
        first._cleanup_completed_records()
        assert datastore.exists(cfg.key("response", client, request)) == 0
        assert datastore.zscore(cfg.key("responses:expiry"), member) is None
        assert datastore.hget(
            cfg.key("terminal", client, request), "retrieval_state"
        ) == b"retrieval_expired"

        datastore.delete(cfg.key("node", node_digest))
        datastore.zrem(cfg.key("nodes:lease"), node_digest)
        before = snapshot()
        retried = second.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        assert retried == dataclasses.replace(accepted, new_outcome=False)
        assert snapshot() == before

        before = snapshot()
        with pytest.raises(RelayStateConflict, match="response lifecycle conflict"):
            second.accept_encrypted_response(
                node,
                owner,
                consumer,
                *identity,
                claim.generation,
                dataclasses.replace(envelope, ciphertext="different-ciphertext"),
            )
        assert snapshot() == before
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


@pytest.mark.parametrize("terminal_fields", ({"additive": "value"}, {"outcome": "completed"}))
def test_encrypted_response_rejects_preexisting_terminal_authority_without_mutation(
    valkey_server, terminal_fields
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    node, owner, consumer = "orphan-node", _digest("orphan-owner"), "consumer"
    identity = ("orphan-client", "orphan-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    terminal = cfg.key("terminal", client, request)
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store._foundation._client.hset(terminal, mapping=terminal_fields)
        before = store._foundation._client.dump(terminal)

        with pytest.raises(ValkeySchemaIncompatibleError):
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, envelope
            )
        assert store._foundation._client.dump(terminal) == before
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_encrypted_response_complete_terminal_preserves_additive_fields(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    node, owner, consumer = "additive-node", _digest("additive-owner"), "consumer"
    identity = ("additive-client", "additive-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    terminal = cfg.key("terminal", client, request)
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        accepted = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        store._foundation._client.hset(terminal, "additive", "preserved")
        before = store._foundation._client.dump(terminal)

        retried = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        assert retried == dataclasses.replace(accepted, new_outcome=False)
        assert store._foundation._client.dump(terminal) == before
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize("retrieval_state", ("response_ready", "retrieval_expired"))
@pytest.mark.parametrize("corruption", ("empty_node", "oversized_node", "negative_accepted", "future_accepted"))
def test_encrypted_response_rejects_invalid_retained_terminal_bounds_without_mutation(
    valkey_server, retrieval_state, corruption
):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=0.05 if retrieval_state == "retrieval_expired" else 300,
        terminal_retention_seconds=600,
    )
    node, owner, consumer = "bounded-node", _digest("bounded-owner"), "consumer"
    identity = ("bounded-client", "bounded-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    terminal = cfg.key("terminal", client, request)
    response = cfg.key("response", client, request)
    datastore = store._foundation._client
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        accepted = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        if retrieval_state == "retrieval_expired":
            for _ in range(200):
                seconds, micros = store._foundation.server_time()
                if seconds + micros / 1_000_000 >= accepted.replay_expires_at_epoch:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("authoritative Valkey time did not reach response expiry")
            store._cleanup_completed_records()

        field = "node_id" if corruption.endswith("node") else "accepted_at_epoch"
        value = {
            "empty_node": "",
            "oversized_node": "x" * (store.config.max_node_id_bytes + 1),
            "negative_accepted": "-1",
            "future_accepted": repr(time.time() + 60),
        }[corruption]
        datastore.hset(terminal, field, value)
        if retrieval_state == "response_ready":
            datastore.hset(response, field, value)
        before = (datastore.hgetall(terminal), datastore.hgetall(response))

        with pytest.raises(ValkeySchemaIncompatibleError):
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, envelope
            )
        assert (datastore.hgetall(terminal), datastore.hgetall(response)) == before
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "client",
        "request",
        "client_public_key",
        "request_id",
        "node_id",
        "consumer_digest",
        "generation",
        "envelope",
        "accepted_at_epoch",
        "response_digest",
        "replay_expires_at_epoch",
        "status",
        "response_expiry_score",
        "response_expiry_member",
        "oversized_client_public_key",
        "oversized_node_id",
        "oversized_envelope",
    ),
)
def test_encrypted_response_retained_retry_validates_complete_paired_authority(
    valkey_server, corruption
):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=300,
        terminal_retention_seconds=600,
    )
    node, owner, consumer = "retained-node", _digest("retained-owner"), "consumer"
    identity = ("retained-client", "retained-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    response_key = cfg.key("response", client, request)
    response_expiries = cfg.key("responses:expiry")
    datastore = store._foundation._client
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )

        if corruption == "response_expiry_score":
            datastore.zincrby(response_expiries, 1, member)
        elif corruption == "response_expiry_member":
            score = datastore.zscore(response_expiries, member)
            datastore.zrem(response_expiries, member)
            datastore.zadd(response_expiries, {f"{client}:{'0' * 64}": score})
        else:
            field = corruption.removeprefix("oversized_")
            replacements = {
                "client": "0" * 64,
                "request": "0" * 64,
                "client_public_key": "different-client",
                "request_id": "different-request",
                "node_id": "different-node",
                "consumer_digest": "0" * 64,
                "generation": str(claim.generation + 1),
                "envelope": b"different-envelope",
                "accepted_at_epoch": "1",
                "response_digest": "0" * 64,
                "replay_expires_at_epoch": "1",
                "status": "different-status",
            }
            replacement = replacements.get(field)
            if corruption.startswith("oversized_"):
                bound = (
                    store.config.max_response_envelope_bytes
                    if field == "envelope"
                    else store.config.max_node_id_bytes
                    if field == "node_id"
                    else store.config.max_identity_bytes
                )
                replacement = b"x" * (bound + 1)
            datastore.hset(response_key, field, replacement)

        snapshot = (
            datastore.hgetall(response_key),
            datastore.hgetall(cfg.key("terminal", client, request)),
            datastore.hgetall(cfg.key("request", client, request)),
            datastore.zrange(response_expiries, 0, -1, withscores=True),
            datastore.zrange(cfg.key("terminals:expiry"), 0, -1, withscores=True),
        )
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, envelope
            )
        assert snapshot == (
            datastore.hgetall(response_key),
            datastore.hgetall(cfg.key("terminal", client, request)),
            datastore.hgetall(cfg.key("request", client, request)),
            datastore.zrange(response_expiries, 0, -1, withscores=True),
            datastore.zrange(cfg.key("terminals:expiry"), 0, -1, withscores=True),
        )
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "partial_response",
        "partial_terminal",
        "terminal_index_mismatch",
        "malformed_lifecycle",
        "orphan_response_expiry",
    ),
)
def test_encrypted_response_cleanup_rejects_malformed_authority_without_writes(
    valkey_server, corruption
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node, owner, consumer = "cleanup-node", _digest("cleanup-owner"), "consumer"
    identity = ("cleanup-client", "cleanup-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    response = cfg.key("response", client, request)
    terminal = cfg.key("terminal", client, request)
    lifecycle = cfg.key("request", client, request)
    response_index = cfg.key("responses:expiry")
    terminal_index = cfg.key("terminals:expiry")
    datastore = store._foundation._client
    def snapshot():
        return (
            tuple(datastore.hgetall(key) for key in (response, terminal, lifecycle)),
            tuple(
                datastore.zrange(key, 0, -1, withscores=True)
                for key in (response_index, terminal_index)
            ),
        )

    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        accepted = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        due = accepted.accepted_at_epoch
        datastore.hset(response, "replay_expires_at_epoch", repr(due))
        datastore.hset(terminal, "replay_expires_at_epoch", repr(due))
        datastore.zadd(response_index, {member: due})

        if corruption == "partial_response":
            datastore.delete(response)
            datastore.hset(response, "additive", "value")
        elif corruption == "partial_terminal":
            datastore.delete(terminal)
            datastore.hset(terminal, "additive", "value")
        elif corruption == "terminal_index_mismatch":
            datastore.zincrby(terminal_index, 1, member)
        elif corruption == "malformed_lifecycle":
            datastore.hdel(lifecycle, "client_public_key")
        else:
            datastore.delete(response)
            datastore.hset(terminal, "retrieval_state", "retrieval_expired")

        before = snapshot()
        with pytest.raises(ValkeySchemaIncompatibleError):
            store._cleanup_completed_records()
        assert snapshot() == before
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize("cleanup_phase", ("response", "terminal"))
@pytest.mark.parametrize(
    "terminal_field", ("retrieval_credential_digest", "cancellation_token_digest")
)
def test_encrypted_response_cleanup_rejects_mismatched_lifecycle_digests(
    valkey_server, cleanup_phase, terminal_field
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node, owner, consumer = "digest-node", _digest("digest-owner"), "consumer"
    identity = ("digest-client", "digest-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    response = cfg.key("response", client, request)
    terminal = cfg.key("terminal", client, request)
    lifecycle = cfg.key("request", client, request)
    response_index = cfg.key("responses:expiry")
    terminal_index = cfg.key("terminals:expiry")
    datastore = store._foundation._client

    def snapshot():
        return (
            datastore.hgetall(response),
            datastore.hgetall(terminal),
            datastore.hgetall(lifecycle),
            datastore.zrange(response_index, 0, -1, withscores=True),
            datastore.zrange(terminal_index, 0, -1, withscores=True),
        )

    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        accepted = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        due = accepted.accepted_at_epoch
        datastore.hset(response, "replay_expires_at_epoch", repr(due))
        datastore.hset(terminal, "replay_expires_at_epoch", repr(due))
        datastore.zadd(response_index, {member: due})
        if cleanup_phase == "terminal":
            store._cleanup_completed_records()
            datastore.hset(terminal, "expires_at_epoch", repr(due))
            datastore.zadd(terminal_index, {member: due})

        datastore.hset(terminal, terminal_field, "f" * 64)
        before = snapshot()
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="state schema incompatible"
        ):
            store._cleanup_completed_records()
        assert snapshot() == before
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_encrypted_response_cleanup_validates_batch_before_exact_reaping(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node, owner, consumer = "batch-node", _digest("batch-owner"), "consumer"
    identities = (("batch-client-a", "request-a"), ("batch-client-b", "request-b"))
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    datastore = store._foundation._client
    records = []
    try:
        store.register(node, _capabilities(concurrency=2), owner)
        for identity in identities:
            _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
            claim = store.claim_queued_request(node, owner, consumer)
            accepted = store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, envelope
            )
            client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
            request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
            member = f"{client}:{request}"
            response = cfg.key("response", client, request)
            terminal = cfg.key("terminal", client, request)
            lifecycle = cfg.key("request", client, request)
            datastore.hset(response, "replay_expires_at_epoch", repr(accepted.accepted_at_epoch))
            datastore.hset(terminal, "replay_expires_at_epoch", repr(accepted.accepted_at_epoch))
            datastore.zadd(cfg.key("responses:expiry"), {member: accepted.accepted_at_epoch})
            records.append((member, response, terminal, lifecycle, accepted.accepted_at_epoch))

        datastore.hdel(records[1][3], "request_id")
        hash_keys = tuple(key for record in records for key in record[1:4])
        index_keys = (cfg.key("responses:expiry"), cfg.key("terminals:expiry"))

        def snapshot():
            return (
                tuple(datastore.hgetall(key) for key in hash_keys),
                tuple(datastore.zrange(key, 0, -1, withscores=True) for key in index_keys),
            )

        before = snapshot()
        with pytest.raises(ValkeySchemaIncompatibleError):
            store._cleanup_completed_records()
        assert snapshot() == before
    finally:
        _delete_claim_fixture_state(store, (node,), identities)
        store.close()


def test_encrypted_response_cleanup_reaps_exact_response_then_terminal(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    node, owner, consumer = "reap-node", _digest("reap-owner"), "consumer"
    identity = ("reap-client", "reap-request")
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    response = cfg.key("response", client, request)
    terminal = cfg.key("terminal", client, request)
    lifecycle = cfg.key("request", client, request)
    datastore = store._foundation._client
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        accepted = store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, envelope
        )
        due = accepted.accepted_at_epoch
        datastore.hset(response, "replay_expires_at_epoch", repr(due))
        datastore.hset(terminal, mapping={"replay_expires_at_epoch": repr(due), "additive": "preserved"})
        datastore.hset(lifecycle, "additive", "preserved")
        datastore.zadd(cfg.key("responses:expiry"), {member: due})
        lifecycle_before = datastore.dump(lifecycle)

        store._cleanup_completed_records()
        assert datastore.exists(response) == 0
        assert datastore.zscore(cfg.key("responses:expiry"), member) is None
        assert datastore.hget(terminal, "retrieval_state") == b"retrieval_expired"
        assert datastore.hget(terminal, "additive") == b"preserved"
        assert datastore.dump(lifecycle) == lifecycle_before

        datastore.hset(terminal, "expires_at_epoch", repr(due))
        datastore.zadd(cfg.key("terminals:expiry"), {member: due})
        store._cleanup_completed_records()
        assert datastore.exists(terminal) == datastore.exists(lifecycle) == 0
        assert datastore.zscore(cfg.key("terminals:expiry"), member) is None
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_encrypted_response_rejects_stale_preflight_without_mutation(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=0.001,
        terminal_retention_seconds=0.001,
        control_tombstone_ttl_seconds=0.001,
    )
    node, owner, consumer = "response-node", _digest("response-owner"), "consumer-a"
    identity = ("response-client", "response-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    deadline = time.time() + 60
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, deadline)
        claim = store.claim_queued_request(node, owner, consumer)
        current_seconds, current_micros = store._foundation.server_time()
        store._foundation.server_time = lambda: (current_seconds - 1, current_micros)

        with pytest.raises(RelayStateConflict, match="response lifecycle conflict"):
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, response
            )

        assert len(store.active_claims(node)) == 1
        assert store.response_records() == ()
        assert store.terminal_records() == ()
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize(
    ("boundary", "claim_ttl", "deadline_offset"),
    (
        ("claim_lease_boundary", 0.08, 5.0),
        ("request_deadline_boundary", 2.0, 0.15),
    ),
)
def test_encrypted_response_inclusive_expiry_boundaries_are_non_mutating(
    valkey_server, boundary, claim_ttl, deadline_offset
):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server, namespace, claim_ttl_seconds=claim_ttl
    )
    second = _registration_store(
        valkey_server, namespace, claim_ttl_seconds=claim_ttl
    )
    node, owner, consumer = "boundary-node", _digest("boundary-owner"), "consumer"
    identity = (f"{boundary}-client", f"{boundary}-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    keys, member = _response_acceptance_authority(first, node, identity)
    datastore = first._foundation._client
    try:
        first.register(node, _capabilities(), owner)
        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + deadline_offset
        _enqueue_claim_fixture(first, node, owner, *identity, deadline)
        claim = first.claim_queued_request(node, owner, consumer)
        if boundary == "request_deadline_boundary":
            assert claim.lease_expires_at_epoch == claim.request_deadline_epoch
        else:
            assert claim.lease_expires_at_epoch < claim.request_deadline_epoch

        datastore.hset(keys[11], mapping={"stage": "generating"})
        before = _exact_key_snapshot(first, keys)
        claim_before = datastore.hgetall(keys[3])
        lifecycle_before = datastore.hgetall(keys[4])
        queue_before = datastore.xrange(keys[2])
        claim_score = datastore.zscore(keys[5], member)
        deadline_score = datastore.zscore(keys[6], member)
        progress_before = datastore.hgetall(keys[11])
        due = (
            claim.lease_expires_at_epoch
            if boundary == "claim_lease_boundary"
            else claim.request_deadline_epoch
        )
        _wait_for_server_epoch(first, due)

        with pytest.raises(
            RelayStateConflict, match="^response lifecycle conflict$"
        ):
            second.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, response
            )

        assert _exact_key_snapshot(first, keys) == before
        assert datastore.hgetall(keys[3]) == claim_before
        assert datastore.hgetall(keys[4]) == lifecycle_before
        assert datastore.hget(keys[4], "state") == b"claimed"
        assert datastore.xrange(keys[2]) == queue_before
        assert datastore.zscore(keys[5], member) == claim_score
        assert datastore.zscore(keys[6], member) == deadline_score
        assert datastore.hgetall(keys[11]) == progress_before
        assert datastore.exists(keys[7]) == datastore.exists(keys[9]) == 0
        assert datastore.zscore(keys[8], member) is None
        assert datastore.zscore(keys[10], member) is None
    finally:
        _delete_claim_fixture_state(first, (node,), (identity,))
        first.close()
        second.close()


def test_encrypted_response_first_transition_removes_exact_authority_once(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, max_claims=8)
    second = _registration_store(valkey_server, namespace, max_claims=8)
    node, owner, consumer = "transition-node", _digest("transition-owner"), "consumer"
    identities = (("transition-client-a", "request-a"), ("transition-client-b", "request-b"))
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    datastore = first._foundation._client
    keys, member = _response_acceptance_authority(first, node, identities[0])
    neighbor_keys, neighbor_member = _response_acceptance_authority(
        first, node, identities[1]
    )
    node_key = keys[1]
    try:
        first.register(node, _capabilities(concurrency=2), owner)
        claims = []
        for identity in identities:
            _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
            claims.append(first.claim_queued_request(node, owner, consumer))
        datastore.hset(keys[11], mapping={"stage": "generating"})
        datastore.hset(neighbor_keys[11], mapping={"stage": "neighbor"})

        queue_before = datastore.xrange(keys[2])
        addressed_entry = datastore.hget(keys[4], "queue_entry")
        neighbor_hashes_before = tuple(
            datastore.hgetall(neighbor_keys[index]) for index in (3, 4, 11)
        )
        neighbor_scores_before = (
            datastore.zscore(neighbor_keys[5], neighbor_member),
            datastore.zscore(neighbor_keys[6], neighbor_member),
        )
        scheduler_before = datastore.hget(node_key, "scheduler_claimed_work")
        assert scheduler_before == b"0"
        assert len(queue_before) == 2

        accepted = first.accept_encrypted_response(
            node, owner, consumer, *identities[0], claims[0].generation, envelope
        )
        assert accepted.new_outcome
        assert datastore.xrange(keys[2]) == [
            entry for entry in queue_before if entry[0] != addressed_entry
        ]
        assert datastore.exists(keys[3]) == 0
        assert datastore.zscore(keys[5], member) is None
        assert datastore.zscore(keys[6], member) is None
        assert datastore.exists(keys[11]) == 0
        assert datastore.hget(keys[4], "state") == b"response_ready"
        assert datastore.hget(node_key, "scheduler_claimed_work") == scheduler_before
        assert tuple(
            datastore.hgetall(neighbor_keys[index]) for index in (3, 4, 11)
        ) == neighbor_hashes_before
        assert (
            datastore.zscore(keys[5], neighbor_member),
            datastore.zscore(keys[6], neighbor_member),
        ) == neighbor_scores_before

        after = _exact_key_snapshot(first, keys)
        retried = second.accept_encrypted_response(
            node, owner, consumer, *identities[0], claims[0].generation, envelope
        )
        assert not retried.new_outcome
        assert retried == dataclasses.replace(accepted, new_outcome=False)
        assert _exact_key_snapshot(first, keys) == after
        assert tuple(
            datastore.hgetall(neighbor_keys[index]) for index in (3, 4, 11)
        ) == neighbor_hashes_before
        assert (
            datastore.zscore(keys[5], neighbor_member),
            datastore.zscore(keys[6], neighbor_member),
        ) == neighbor_scores_before
    finally:
        _delete_claim_fixture_state(first, (node,), identities)
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("capacity", "limits", "same_client"),
    (
        ("global_active", {"max_queued_requests": 1}, False),
        ("node_active", {"max_queue_depth_per_node": 1}, False),
        ("client_active", {"max_queued_requests_per_client": 1}, True),
        ("global_claim", {"max_claims": 1}, False),
        ("node_claim", {"max_claims_per_node": 1}, False),
    ),
)
def test_encrypted_response_releases_capacity_immediately(
    valkey_server, capacity, limits, same_client
):
    defaults = {
        "max_queued_requests": 8,
        "max_queue_depth_per_node": 8,
        "max_queued_requests_per_client": 8,
        "max_claims": 8,
        "max_claims_per_node": 8,
    }
    defaults.update(limits)
    store = _registration_store(valkey_server, uuid.uuid4().hex, **defaults)
    node, owner, consumer = "release-node", _digest("release-owner"), "consumer"
    first_identity = ("release-client", "first")
    second_identity = (
        "release-client" if same_client else "release-other-client",
        "second",
    )
    identities = (first_identity, second_identity)
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    request_envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    deadline = time.time() + 60
    second_selection = None
    try:
        store.register(node, _capabilities(concurrency=4), owner)
        _enqueue_claim_fixture(store, node, owner, *first_identity, deadline)
        first_claim = store.claim_queued_request(node, owner, consumer)

        if capacity.endswith("claim"):
            _enqueue_claim_fixture(store, node, owner, *second_identity, deadline)
            with pytest.raises(RelayStateCapacityExceeded):
                store.claim_queued_request(node, owner, "second-consumer")
        elif capacity == "node_active":
            with pytest.raises(RelayStateNoCapacity):
                store.select_and_reserve(
                    *second_identity, "qwen3-8b-instruct", "8k-fast", deadline
                )
        else:
            second_selection = store.select_and_reserve(
                *second_identity, "qwen3-8b-instruct", "8k-fast", deadline
            )
            with pytest.raises(RelayStateNoCapacity):
                store.enqueue_encrypted_request(
                    *second_identity,
                    second_selection.reservation_token,
                    node,
                    "qwen3-8b-instruct",
                    "8k-fast",
                    deadline,
                    request_envelope,
                    "cancel",
                )

        store.accept_encrypted_response(
            node, owner, consumer, *first_identity, first_claim.generation, response
        )

        if capacity.endswith("claim"):
            released = store.claim_queued_request(node, owner, "second-consumer")
            assert released.state == "claimed"
        else:
            if capacity == "node_active":
                second_selection = store.select_and_reserve(
                    *second_identity, "qwen3-8b-instruct", "8k-fast", deadline
                )
            released = store.enqueue_encrypted_request(
                *second_identity,
                second_selection.reservation_token,
                node,
                "qwen3-8b-instruct",
                "8k-fast",
                deadline,
                request_envelope,
                "cancel",
            )
            assert released.created
    finally:
        if second_selection is not None and second_selection.reservation_token:
            token = hashlib.sha256(
                second_selection.reservation_token.encode("ascii")
            ).hexdigest()
            store._foundation._client.delete(
                store._foundation.config.key("reservation", token)
            )
        _delete_claim_fixture_state(store, (node,), identities)
        store.close()


@pytest.mark.parametrize(
    ("bound", "same_client", "limits"),
    (
        ("global-response", False, {"max_responses": 1}),
        ("per-client-response", True, {"max_responses_per_client": 1}),
        ("global-terminal", False, {"max_terminal_records": 1}),
        ("per-client-terminal", True, {"max_terminal_records_per_client": 1}),
    ),
)
def test_encrypted_response_capacity_bounds_reject_without_mutation(
    valkey_server, bound, same_client, limits
):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=300,
        terminal_retention_seconds=600,
        **limits,
    )
    second = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=300,
        terminal_retention_seconds=600,
        **limits,
    )
    node, owner, consumer = "capacity-node", _digest("capacity-owner"), "consumer"
    first_client = f"{bound}-client"
    second_client = first_client if same_client else f"{bound}-other-client"
    identities = ((first_client, f"{bound}-first"), (second_client, f"{bound}-second"))
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = first._foundation.config
    node_digest = first._node_digest(node)

    def declared_snapshot():
        zset_keys = [
            cfg.key("nodes:lease"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("responses:expiry"),
            cfg.key("terminals:expiry"),
        ]
        hash_keys = [cfg.key("node", node_digest)]
        for client_id, request_id in identities:
            client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
            request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
            hash_keys.extend(
                (
                    cfg.key("request", client, request),
                    cfg.key("claim", client, request),
                    cfg.key("response", client, request),
                    cfg.key("terminal", client, request),
                    cfg.key("progress", client, request),
                )
            )
        datastore = first._foundation._client
        return (
            {key: datastore.hgetall(key) for key in hash_keys},
            {key: datastore.zrange(key, 0, -1, withscores=True) for key in zset_keys},
            datastore.xrange(cfg.key("queue", node_digest)),
        )

    try:
        first.register(node, _capabilities(), owner)
        generations = []
        for identity in identities:
            _enqueue_claim_fixture(first, node, owner, *identity, time.time() + 60)
            generations.append(
                first.claim_queued_request(node, owner, consumer).generation
            )
            if identity == identities[0]:
                first.accept_encrypted_response(
                    node, owner, consumer, *identity, generations[-1], response
                )

        before = declared_snapshot()
        with pytest.raises(
            RelayStateCapacityExceeded,
            match="^response lifecycle capacity reached$",
        ):
            second.accept_encrypted_response(
                node, owner, consumer, *identities[1], generations[1], response
            )
        assert declared_snapshot() == before
    finally:
        _delete_claim_fixture_state(first, (node,), identities)
        first.close()
        second.close()


@pytest.mark.parametrize(
    "malformed_authority",
    (
        "queue_client_digest",
        "queue_request_digest",
        "request_deadline_nan",
        "request_deadline_inf",
        "request_deadline_zero",
        "node_lease_nan",
        "node_lease_inf",
        "request_sequence_zero",
        "request_sequence_fractional",
        "request_sequence_mismatch",
        "claim_deadline_nan",
        "claim_sequence_zero",
        "claim_sequence_fractional",
        "claim_generation_zero",
        "claim_generation_fractional",
        "claim_lease_nan",
        "oversized_client_public_key",
        "empty_client_public_key",
        "oversized_request_id",
        "empty_request_id",
        "oversized_envelope",
        "empty_envelope",
        "malformed_generation_cursor",
        "generation_cursor_behind_reclaim",
    ),
)
def test_claim_queued_request_rejects_malformed_addressed_authority_without_mutation(
    valkey_server, malformed_authority
):
    store = _registration_store(valkey_server, uuid.uuid4().hex, claim_ttl_seconds=30)
    cfg = store._foundation.config
    client = store._foundation._client
    owner, node_id = _digest("malformed-owner"), "malformed-node"
    client_id, request_id = "malformed-client", "malformed-request"
    client_digest = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
    request_digest = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
    node_digest = store._node_digest(node_id)
    request_key = cfg.key("request", client_digest, request_digest)
    claim_key = cfg.key("claim", client_digest, request_digest)
    queue_key = cfg.key("queue", node_digest)
    cursor_key = cfg.key("cursor")
    claims_expiry_key = cfg.key("claims:expiry")
    member = f"{client_digest}:{request_digest}"
    deadline = store._foundation.server_time()[0] + 120
    node_key = cfg.key("node", node_digest)
    sorted_set_keys = (
        cfg.key("nodes:lease"),
        claims_expiry_key,
        cfg.key("requests:deadline"),
        cfg.key("reservations:expiry"),
    )

    def snapshot():
        return (
            client.hgetall(node_key),
            client.xrange(queue_key),
            client.hgetall(request_key),
            client.hgetall(claim_key),
            client.hgetall(cursor_key),
            tuple(client.zrange(key, 0, -1, withscores=True) for key in sorted_set_keys),
        )
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            store, node_id, owner, client_id, request_id, deadline
        )

        reclaim_case = malformed_authority.startswith("claim_") or (
            malformed_authority == "generation_cursor_behind_reclaim"
        )
        if reclaim_case:
            claimed = store.claim_queued_request(node_id, owner, "first-consumer")
            expired = store._foundation.server_time()[0] - 1
            client.hset(claim_key, "lease_expires", expired)
            client.zadd(claims_expiry_key, {member: expired})

        if malformed_authority == "queue_client_digest":
            client.xdel(queue_key, "1-0")
            client.xadd(
                queue_key,
                {"client": "g" * 64, "request": request_digest},
                id="2-0",
            )
        elif malformed_authority == "queue_request_digest":
            client.xdel(queue_key, "1-0")
            client.xadd(
                queue_key,
                {"client": client_digest, "request": "z" * 64},
                id="2-0",
            )
        elif malformed_authority == "request_deadline_nan":
            client.hset(request_key, "deadline", "nan")
        elif malformed_authority == "request_deadline_inf":
            client.hset(request_key, "deadline", "inf")
        elif malformed_authority == "request_deadline_zero":
            client.hset(request_key, "deadline", "0")
        elif malformed_authority == "node_lease_nan":
            client.hset(node_key, "lease_expires_at_epoch", "nan")
        elif malformed_authority == "node_lease_inf":
            client.hset(node_key, "lease_expires_at_epoch", "inf")
            client.zadd(cfg.key("nodes:lease"), {node_digest: "+inf"})
        elif malformed_authority == "request_sequence_zero":
            client.hset(request_key, "sequence", "0")
        elif malformed_authority == "request_sequence_fractional":
            client.hset(request_key, "sequence", "1.5")
        elif malformed_authority == "request_sequence_mismatch":
            client.hset(request_key, "sequence", "2")
        elif malformed_authority == "claim_deadline_nan":
            client.hset(claim_key, "deadline", "nan")
        elif malformed_authority == "claim_sequence_zero":
            client.hset(claim_key, "sequence", "0")
        elif malformed_authority == "claim_sequence_fractional":
            client.hset(claim_key, "sequence", "1.5")
        elif malformed_authority == "claim_generation_zero":
            client.hset(claim_key, "generation", "0")
            client.hset(request_key, "claim_generation", "0")
        elif malformed_authority == "claim_generation_fractional":
            client.hset(claim_key, "generation", "1.5")
            client.hset(request_key, "claim_generation", "1.5")
        elif malformed_authority == "claim_lease_nan":
            client.hset(claim_key, "lease_expires", "nan")
        elif malformed_authority == "oversized_client_public_key":
            client.hset(
                request_key,
                "client_public_key",
                b"k" * (store.config.max_identity_bytes + 1),
            )
        elif malformed_authority == "empty_client_public_key":
            client.hset(request_key, "client_public_key", b"")
        elif malformed_authority == "oversized_request_id":
            client.hset(
                request_key,
                "request_id",
                b"r" * (store.config.max_identity_bytes + 1),
            )
        elif malformed_authority == "empty_request_id":
            client.hset(request_key, "request_id", b"")
        elif malformed_authority == "oversized_envelope":
            client.hset(
                request_key,
                "envelope",
                b"e" * (store.config.max_envelope_bytes + 1),
            )
        elif malformed_authority == "empty_envelope":
            client.hset(request_key, "envelope", b"")
        elif malformed_authority == "malformed_generation_cursor":
            client.hset(cursor_key, "_claim_generation", "not-an-integer")
        elif malformed_authority == "generation_cursor_behind_reclaim":
            assert claimed.generation > 0
            client.hset(cursor_key, "_claim_generation", claimed.generation - 1)

        before = snapshot()
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ) as caught:
            store.claim_queued_request(node_id, owner, "rejected-consumer")

        assert str(caught.value) == "state schema incompatible"
        assert snapshot() == before
    finally:
        _delete_claim_fixture_state(store, (node_id,), ((client_id, request_id),))
        store.close()


def test_claim_fifo_skips_live_claim_and_returns_typed_empty(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    owner, node_id = _digest("fifo-owner"), "fifo-node"
    identities = (
        ("fifo-client-a", "fifo-request-a"),
        ("fifo-client-b", "fifo-request-b"),
    )
    deadline = first._foundation.server_time()[0] + 30
    cfg = first._foundation.config
    node = first._node_digest(node_id)
    try:
        first.register(node_id, _capabilities(concurrency=2), owner)
        envelopes = tuple(
            _enqueue_claim_fixture(first, node_id, owner, *identity, deadline)
            for identity in identities
        )

        claimed = tuple(
            store.claim_queued_request(node_id, owner, f"fifo-consumer-{index}")
            for index, store in enumerate((second, first), 1)
        )
        for result, identity, envelope in zip(claimed, identities, envelopes):
            assert result.state == "claimed"
            assert (result.client_public_key, result.request_id) == identity
            assert result.envelope == envelope

        empty = second.claim_queued_request(node_id, owner, "fifo-consumer-empty")
        assert empty.state == "empty"
        assert empty.generation is None
        assert first._foundation._client.xlen(cfg.key("queue", node)) == 2
        assert first._foundation._client.zcard(cfg.key("claims:expiry")) == 2
        assert first._foundation._client.zcard(cfg.key("requests:deadline")) == 2
        assert first._foundation._client.zcard(cfg.key("reservations:expiry")) == 0
        assert len(first.active_claims(node_id)) == 2
        assert first._foundation._client.hget(
            cfg.key("node", node), "scheduler_claimed_work"
        ) == b"0"
    finally:
        _delete_claim_fixture_state(first, (node_id,), identities)
        first.close()
        second.close()


def test_claim_rejections_are_typed_and_non_mutating(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace, lease_ttl_seconds=0.08)
    owner, node_id = _digest("rejection-owner"), "rejection-node"
    identity = ("rejection-client", "rejection-request")
    cfg = store._foundation.config
    node = store._node_digest(node_id)
    try:
        registration = store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            store, node_id, owner, *identity, store._foundation.server_time()[0] + 30
        )

        def snapshot():
            return (
                store._foundation._client.hgetall(cfg.key("node", node)),
                store._foundation._client.xrange(cfg.key("queue", node)),
                store._foundation._client.zrange(
                    cfg.key("requests:deadline"), 0, -1, withscores=True
                ),
                store._foundation._client.hgetall(cfg.key("cursor")),
                store._foundation._client.zrange(
                    cfg.key("claims:expiry"), 0, -1, withscores=True
                ),
            )

        for attempted_node, attempted_owner in (
            (node_id, _digest("wrong-owner")),
            ("unknown-node", owner),
        ):
            before = snapshot()
            with pytest.raises(
                RelayStateCredentialMismatch, match="claim owner is invalid"
            ):
                store.claim_queued_request(
                    attempted_node, attempted_owner, "rejection-consumer"
                )
            assert snapshot() == before

        _wait_for_server_epoch(store, registration.lease_expires_at_epoch)
        before = snapshot()
        with pytest.raises(
            RelayStateCredentialMismatch, match="claim owner is invalid"
        ):
            store.claim_queued_request(node_id, owner, "rejection-consumer")
        assert snapshot() == before

        store.register(node_id, _capabilities(), owner)
        assert store.unregister(node_id, owner)
        before = snapshot()
        with pytest.raises(
            RelayStateCredentialMismatch, match="claim owner is invalid"
        ):
            store.claim_queued_request(node_id, owner, "rejection-consumer")
        assert snapshot() == before
        assert store._foundation._client.zcard(cfg.key("claims:expiry")) == 0
    finally:
        _delete_claim_fixture_state(store, (node_id, "unknown-node"), (identity,))
        store.close()


def test_claim_lease_is_bounded_by_request_deadline(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server, namespace, claim_ttl_seconds=2, lease_ttl_seconds=5
    )
    owner, node_id = _digest("deadline-owner"), "deadline-node"
    identity = ("deadline-client", "deadline-request")
    cfg = store._foundation.config
    node = store._node_digest(node_id)
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    try:
        store.register(node_id, _capabilities(), owner)
        seconds, micros = store._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + 0.32
        _enqueue_claim_fixture(store, node_id, owner, *identity, deadline)

        result = store.claim_queued_request(node_id, owner, "deadline-consumer")
        assert result.state == "claimed"
        assert result.request_deadline_epoch == deadline
        assert result.lease_expires_at_epoch <= deadline
        assert store._foundation._client.xlen(cfg.key("queue", node)) == 1
        assert store._foundation._client.hget(
            cfg.key("request", client, request), "claim_generation"
        ) == str(result.generation).encode()

        _wait_for_server_epoch(store, deadline)
        assert store.active_claims(node_id) == ()
        empty = store.claim_queued_request(node_id, owner, "deadline-consumer-final")
        assert empty.state == "empty" and empty.generation is None
        assert store._foundation._client.zscore(
            cfg.key("claims:expiry"), f"{client}:{request}"
        ) == deadline
        assert store._foundation._client.hget(
            cfg.key("request", client, request), "claim_generation"
        ) == str(result.generation).encode()
        assert store._foundation._client.xlen(cfg.key("queue", node)) == 1
    finally:
        _delete_claim_fixture_state(store, (node_id,), (identity,))
        store.close()


def test_renew_claim_preserves_exact_deadline_representation(valkey_server):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        claim_ttl_seconds=2,
        lease_ttl_seconds=5,
    )
    owner, node_id = _digest("exact-deadline-owner"), "exact-deadline-node"
    consumer = "exact-deadline-consumer"
    identity = ("exact-deadline-client", "exact-deadline-request")
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    member = f"{client}:{request}"
    request_key = cfg.key("request", client, request)
    claim_key = cfg.key("claim", client, request)
    try:
        store.register(node_id, _capabilities(), owner)
        seconds, micros = store._foundation.server_time()
        rounded_deadline = float(f"{seconds + micros / 1_000_000 + 0.45:.6f}")
        deadline = math.nextafter(rounded_deadline, math.inf)
        assert deadline != float(f"{deadline:.6f}")
        _enqueue_claim_fixture(store, node_id, owner, *identity, deadline)
        stored_deadline = store._foundation._client.hget(request_key, "deadline")
        decoded_deadline = float(stored_deadline)
        assert math.isfinite(decoded_deadline)
        assert stored_deadline == format(deadline, ".17g").encode()
        assert decoded_deadline == deadline

        claimed = store.claim_queued_request(node_id, owner, consumer)
        assert claimed.state == "claimed"
        assert store._foundation._client.hget(
            claim_key, "lease_expires"
        ) == stored_deadline
        request_before = store._foundation._client.hgetall(request_key)
        claim_before = store._foundation._client.hgetall(claim_key)
        cursor_before = store._foundation._client.hgetall(cfg.key("cursor"))
        queue_before = store._foundation._client.xrange(
            cfg.key("queue", store._node_digest(node_id))
        )

        renewed = store.renew_claim(
            node_id, owner, consumer, *identity, claimed.generation
        )
        assert renewed.state == "continued"
        assert renewed.lease_expires_at_epoch == decoded_deadline
        assert renewed.lease_expires_at_epoch <= decoded_deadline
        assert store._foundation._client.hget(
            claim_key, "lease_expires"
        ) == stored_deadline
        assert store._foundation._client.zscore(
            cfg.key("claims:expiry"), member
        ) == decoded_deadline

        claim_after = store._foundation._client.hgetall(claim_key)
        assert {k: v for k, v in claim_after.items() if k != b"lease_expires"} == {
            k: v for k, v in claim_before.items() if k != b"lease_expires"
        }
        assert store._foundation._client.hgetall(request_key) == request_before
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
        assert store._foundation._client.xrange(
            cfg.key("queue", store._node_digest(node_id))
        ) == queue_before

        _wait_for_server_epoch(store, deadline)
        before_rejected = _claim_authority_snapshot(store, node_id, *identity)
        rejected = store.renew_claim(
            node_id, owner, consumer, *identity, claimed.generation
        )
        assert rejected.state == "expired"
        assert rejected.reason == "request_deadline_expired"
        assert rejected.generation == claimed.generation
        after_terminalization = _claim_authority_snapshot(store, node_id, *identity)
        assert after_terminalization != before_rejected
        assert after_terminalization[0][b"state"] == b"expired"
        assert after_terminalization[1] == {}
        assert after_terminalization[3] == []
        assert after_terminalization[4] == []
    finally:
        _delete_claim_fixture_state(store, (node_id,), (identity,))
        store.close()


def _claim_authority_snapshot(store, node_id, client_id, request_id):
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
    node = store._node_digest(node_id)
    datastore = store._foundation._client
    return (
        datastore.hgetall(cfg.key("request", client, request)),
        datastore.hgetall(cfg.key("claim", client, request)),
        datastore.hgetall(cfg.key("cursor")),
        datastore.xrange(cfg.key("queue", node)),
        datastore.zrange(cfg.key("claims:expiry"), 0, -1, withscores=True),
    )

def _claim_namespace_snapshot(store, node_id, identities):
    cfg = store._foundation.config
    node = store._node_digest(node_id)
    keys = [
        cfg.key("nodes:lease"),
        cfg.key("cursor"),
        cfg.key("requests:deadline"),
        cfg.key("claims:expiry"),
        cfg.key("node", node),
        cfg.key("queue", node),
    ]
    for client_id, request_id in identities:
        client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
        request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
        keys.extend(
            (cfg.key("request", client, request), cfg.key("claim", client, request))
        )
    client = store._foundation._client
    return tuple(client.dump(key) for key in keys)


def test_claim_namespace_isolation_covers_claim_renewal_and_reads(valkey_server):
    stores = [
        _registration_store(valkey_server, uuid.uuid4().hex, claim_ttl_seconds=2)
        for _ in range(2)
    ]
    node_id = "shared-claim-node"
    identity = ("shared-client-key", "shared-request-id")
    owners = (_digest("namespace-owner-a"), _digest("namespace-owner-b"))
    consumers = ("namespace-consumer-a", "namespace-consumer-b")
    envelopes = (
        EncryptedRequestEnvelope(
            "tokenplace_api_v1_relay_e2ee", 1, "ciphertext-a", "cipherkey-a", "iv-a"
        ),
        EncryptedRequestEnvelope(
            "tokenplace_api_v1_relay_e2ee", 1, "ciphertext-b", "cipherkey-b", "iv-b"
        ),
    )
    try:
        results = []
        for store, owner, consumer, envelope in zip(
            stores, owners, consumers, envelopes
        ):
            store.register(node_id, _capabilities(), owner)
            deadline = store._foundation.server_time()[0] + 30
            selection = store.select_and_reserve(
                *identity, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
            )
            store.enqueue_encrypted_request(
                *identity,
                selection.reservation_token,
                node_id,
                "qwen3-8b-instruct",
                "8k-fast",
                deadline,
                envelope,
                "cancel",
            )
            results.append(store.claim_queued_request(node_id, owner, consumer))

        before_other = _claim_namespace_snapshot(stores[1], node_id, (identity,))
        renewed = stores[0].renew_claim(
            node_id, owners[0], consumers[0], *identity, results[0].generation
        )
        assert renewed.state == "continued"
        assert (
            _claim_namespace_snapshot(stores[1], node_id, (identity,)) == before_other
        )

        for index, store in enumerate(stores):
            active = store.active_claims(node_id)
            claimed = store.claimed_request(node_id, identity[1])
            assert len(active) == 1 and claimed is not None
            queued, claim = claimed
            assert claim == active[0]
            assert claim.control_credential_digest == owners[index]
            assert claim.consumer_identity_digest == store._consumer_digest(
                consumers[index]
            )
            assert queued.envelope == envelopes[index]
            assert claim.envelope == envelopes[index]
            assert store.claimed_request(node_id, "unrelated-request") is None
    finally:
        for store in stores:
            _delete_claim_fixture_state(store, (node_id,), (identity,))
            store.close()


def test_claim_compatibility_gates_fail_closed_before_state_or_scripts(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex, claim_ttl_seconds=2)
    owner, node_id = _digest("compatibility-owner"), "compatibility-node"
    identity = ("compatibility-client", "compatibility-request")
    cfg = store._foundation.config
    schema_key = cfg.key("schema")
    original_call = store._foundation._call
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            store, node_id, owner, *identity, store._foundation.server_time()[0] + 30
        )
        claim = store.claim_queued_request(node_id, owner, "compatibility-consumer")
        baseline = _claim_namespace_snapshot(store, node_id, (identity,))

        cases = (
            (
                _manifest(reader_min=2, reader_max=2),
                (
                    lambda: store.active_claims(node_id),
                    lambda: store.claimed_request(node_id, identity[1]),
                    lambda: store.claim_queued_request(node_id, owner, "consumer-two"),
                    lambda: store.renew_claim(
                        node_id,
                        owner,
                        "compatibility-consumer",
                        *identity,
                        claim.generation,
                    ),
                ),
            ),
            (
                _manifest(writer_min=2, writer_max=2, active_writer_revision=2),
                (
                    lambda: store.claim_queued_request(node_id, owner, "consumer-two"),
                    lambda: store.renew_claim(
                        node_id,
                        owner,
                        "compatibility-consumer",
                        *identity,
                        claim.generation,
                    ),
                ),
            ),
        )
        for manifest, operations in cases:
            store._foundation._client.set(schema_key, manifest.encode())
            for operation in operations:
                commands = []

                def record_command(command, *args, **kwargs):
                    commands.append(command.__name__.lower())
                    return original_call(command, *args, **kwargs)

                store._foundation._call = record_command
                with pytest.raises(
                    ValkeySchemaIncompatibleError,
                    match="^state schema incompatible$",
                ):
                    operation()
                assert not {"zrangebyscore", "hmget", "xrange", "evalsha"}.intersection(
                    commands
                )
                assert (
                    _claim_namespace_snapshot(store, node_id, (identity,)) == baseline
                )

            if manifest.writer_min == 2:
                assert len(store.active_claims(node_id)) == 1
                assert store.claimed_request(node_id, identity[1]) is not None

        for script, mutation in (
            (
                CLAIM_SCRIPT,
                lambda: store.claim_queued_request(node_id, owner, "consumer-two"),
            ),
            (
                RENEW_CLAIM_SCRIPT,
                lambda: store.renew_claim(
                    node_id,
                    owner,
                    "compatibility-consumer",
                    *identity,
                    claim.generation,
                ),
            ),
        ):
            digests = dict(SCRIPT_DIGESTS)
            digests[script.name] = "0" * 64
            store._foundation._client.set(
                schema_key, _manifest(script_digests=digests).encode()
            )
            commands = []

            def record_command(command, *args, **kwargs):
                commands.append(command.__name__.lower())
                return original_call(command, *args, **kwargs)

            store._foundation._call = record_command
            with pytest.raises(ValkeySchemaIncompatibleError):
                mutation()
            assert "evalsha" not in commands
            assert _claim_namespace_snapshot(store, node_id, (identity,)) == baseline
    finally:
        store._foundation._call = original_call
        _delete_claim_fixture_state(store, (node_id,), (identity,))
        store.close()


@pytest.mark.parametrize("failure", ("connection", "readonly"))
@pytest.mark.parametrize("operation", ("active", "claimed", "claim", "renew"))
def test_claim_backend_failures_are_typed_redacted_and_not_replayed(
    valkey_server, caplog, operation, failure
):
    store = _registration_store(valkey_server, uuid.uuid4().hex, claim_ttl_seconds=2)
    raw_owner = "raw-owner-credential-marker"
    owner, node_id = _digest(raw_owner), "private-node-marker"
    identities = (
        ("public-key-marker", "request-id-marker"),
        ("second-public-key-marker", "second-request-id-marker"),
    )
    consumer = "consumer-identity-marker"
    cfg = store._foundation.config
    endpoint_marker = "private-backend-endpoint-marker"
    ciphertext_marker = "ciphertext-sensitive-marker"
    cipher_key_marker = "sensitive-cipher-key-marker"
    iv_marker = "sensitive-iv-marker"
    key_marker = cfg.key("claim", _digest("private-client"), _digest("private-request"))
    original_evalsha = store._foundation._client.evalsha
    original_zrange = store._foundation._client.zrangebyscore
    calls = 0
    try:
        store.register(node_id, _capabilities(), owner)
        deadline = store._foundation.server_time()[0] + 30
        envelope = EncryptedRequestEnvelope(
            "tokenplace_api_v1_relay_e2ee",
            1,
            ciphertext_marker,
            cipher_key_marker,
            iv_marker,
        )
        for identity in identities:
            _enqueue_claim_fixture(
                store, node_id, owner, *identity, deadline, envelope=envelope
            )
        claimed = store.claim_queued_request(node_id, owner, consumer)
        store._foundation._client.script_load(CLAIM_SCRIPT.source)
        store._foundation._client.script_load(RENEW_CLAIM_SCRIPT.source)
        before = _claim_namespace_snapshot(store, node_id, identities)

        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            message = " ".join(
                (
                    endpoint_marker,
                    raw_owner,
                    owner,
                    consumer,
                    *identities[0],
                    ciphertext_marker,
                    cipher_key_marker,
                    iv_marker,
                    key_marker,
                )
            )
            if failure == "readonly":
                raise redis.ResponseError("READONLY " + message)
            raise redis.ConnectionError(message)

        if operation in {"active", "claimed"}:
            store._foundation._client.zrangebyscore = fail
            invoke = (
                (lambda: store.active_claims(node_id))
                if operation == "active"
                else (lambda: store.claimed_request(node_id, identities[0][1]))
            )
        else:
            store._foundation._client.evalsha = fail
            invoke = (
                (lambda: store.claim_queued_request(node_id, owner, "other-consumer"))
                if operation == "claim"
                else lambda: store.renew_claim(
                    node_id, owner, consumer, *identities[0], claimed.generation
                )
            )
        expected = (
            ValkeyReadOnlyError if failure == "readonly" else ValkeyUnavailableError
        )
        expected_message = (
            "^state backend is not writable$"
            if failure == "readonly"
            else "^state backend unavailable$"
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(expected, match=expected_message) as caught:
                invoke()
        assert calls == (
            1 if operation in {"claim", "renew"} or failure == "readonly" else 2
        )
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
        markers = (
            endpoint_marker,
            raw_owner,
            owner,
            consumer,
            *identities[0],
            ciphertext_marker,
            cipher_key_marker,
            iv_marker,
            key_marker,
        )
        assert all(marker not in rendered for marker in markers)
        assert caught.value.__cause__ is None
        store._foundation._client.evalsha = original_evalsha
        store._foundation._client.zrangebyscore = original_zrange
        assert _claim_namespace_snapshot(store, node_id, identities) == before
    finally:
        store._foundation._client.evalsha = original_evalsha
        store._foundation._client.zrangebyscore = original_zrange
        _delete_claim_fixture_state(store, (node_id,), identities)
        store.close()


def test_claim_unknown_fields_survive_renewal_and_reclaim(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex, claim_ttl_seconds=0.08)
    owner, node_id = _digest("additive-owner"), "additive-node"
    identity = ("additive-client", "additive-request")
    cfg = store._foundation.config
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    request_key = cfg.key("request", client, request)
    claim_key = cfg.key("claim", client, request)
    future_request = b"future-request-value"
    future_claim = b"future-claim-value"
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            store, node_id, owner, *identity, store._foundation.server_time()[0] + 30
        )
        claimed = store.claim_queued_request(node_id, owner, "additive-consumer-one")
        store._foundation._client.hset(request_key, "future_request", future_request)
        store._foundation._client.hset(claim_key, "future_claim", future_claim)

        renewed = store.renew_claim(
            node_id,
            owner,
            "additive-consumer-one",
            *identity,
            claimed.generation,
        )
        assert renewed.state == "continued"
        assert (
            store._foundation._client.hget(request_key, "future_request")
            == future_request
        )
        assert store._foundation._client.hget(claim_key, "future_claim") == future_claim

        _wait_for_server_epoch(store, renewed.lease_expires_at_epoch)
        reclaimed = store.claim_queued_request(node_id, owner, "additive-consumer-two")
        assert reclaimed.state == "reclaimed"
        assert reclaimed.generation > claimed.generation
        assert (
            store._foundation._client.hget(request_key, "future_request")
            == future_request
        )
        assert store._foundation._client.hget(claim_key, "future_claim") == future_claim
    finally:
        _delete_claim_fixture_state(store, (node_id,), (identity,))
        store.close()




def test_renew_claim_authenticates_exact_live_authority(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, claim_ttl_seconds=2)
    second = _registration_store(valkey_server, namespace, claim_ttl_seconds=2)
    owner = _digest("exact-renew-owner")
    other_owner = _digest("exact-renew-other-owner")
    node_id = "exact-renew-node"
    other_node_id = "exact-renew-other-node"
    identity = ("exact-renew-client", "exact-renew-request")
    deadline = first._foundation.server_time()[0] + 60
    cfg = first._foundation.config
    node = first._node_digest(node_id)
    other_node = first._node_digest(other_node_id)
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    try:
        first.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(first, node_id, owner, *identity, deadline)
        claim = first.claim_queued_request(node_id, owner, "exact-consumer")
        first.register(other_node_id, _capabilities(), other_owner)

        rejected = (
            (
                (
                    node_id,
                    _digest("other-owner"),
                    "exact-consumer",
                    *identity,
                    claim.generation,
                ),
                "owner_mismatch",
            ),
            (
                (node_id, owner, "other-consumer", *identity, claim.generation),
                "owner_mismatch",
            ),
            (
                (
                    other_node_id,
                    other_owner,
                    "exact-consumer",
                    *identity,
                    claim.generation,
                ),
                "owner_mismatch",
            ),
            (
                (
                    node_id,
                    owner,
                    "exact-consumer",
                    "other-client",
                    identity[1],
                    claim.generation,
                ),
                "missing_or_expired",
            ),
            (
                (
                    node_id,
                    owner,
                    "exact-consumer",
                    identity[0],
                    "other-request",
                    claim.generation,
                ),
                "missing_or_expired",
            ),
            (
                (node_id, owner, "exact-consumer", *identity, claim.generation + 1),
                "stale_generation",
            ),
        )
        for arguments, expected_state in rejected:
            before = _claim_authority_snapshot(first, node_id, *identity)
            result = second.renew_claim(*arguments)
            assert result.state == expected_state
            if expected_state == "stale_generation":
                assert result.generation == claim.generation
            assert _claim_authority_snapshot(first, node_id, *identity) == before

        before = _claim_authority_snapshot(first, node_id, *identity)
        node_before = first._foundation._client.hgetall(cfg.key("node", node))
        renewed = second.renew_claim(
            node_id, owner, "exact-consumer", *identity, claim.generation
        )
        assert renewed.state == "continued"
        assert renewed.generation == claim.generation
        after = _claim_authority_snapshot(first, node_id, *identity)
        assert after[0] == before[0]
        assert after[1].keys() == before[1].keys()
        assert {k: v for k, v in after[1].items() if k != b"lease_expires"} == {
            k: v for k, v in before[1].items() if k != b"lease_expires"
        }
        stored_lease = float(after[1][b"lease_expires"])
        assert stored_lease == pytest.approx(renewed.lease_expires_at_epoch, abs=0.001)
        assert after[2:4] == before[2:4]
        assert after[4][0] == (before[4][0][0], stored_lease)
        assert first._foundation._client.hgetall(cfg.key("node", node)) == node_before
    finally:
        first._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("node", other_node),
            cfg.key("queue", node),
            cfg.key("queue", other_node),
            cfg.key("request", client, request),
            cfg.key("claim", client, request),
        )
        first.close()
        second.close()

@pytest.mark.parametrize("removal", ("unregister", "expiry"))
def test_generation_and_owner_fencing_survives_node_id_reuse(valkey_server, removal):
    namespace = uuid.uuid4().hex
    stores = [
        _registration_store(
            valkey_server, namespace, lease_ttl_seconds=0.05, claim_ttl_seconds=2
        )
        for _ in range(2)
    ]
    first, second = stores
    old_owner, new_owner = _digest("old-owner"), _digest("new-owner")
    node_id = "reused-node"
    identities = (("reuse-client", "request-old"), ("reuse-client", "request-new"))
    cfg = first._foundation.config
    node = first._node_digest(node_id)
    try:
        registration = first.register(node_id, _capabilities(), old_owner)
        deadline = first._foundation.server_time()[0] + 60
        _enqueue_claim_fixture(first, node_id, old_owner, *identities[0], deadline)
        old = second.claim_queued_request(node_id, old_owner, "old-consumer")
        if removal == "unregister":
            assert second.unregister(node_id, old_owner)
        else:
            _wait_for_server_epoch(first, registration.lease_expires_at_epoch)

        second.register(node_id, _capabilities(), new_owner)
        _enqueue_claim_fixture(second, node_id, new_owner, *identities[1], deadline)
        new = first.claim_queued_request(node_id, new_owner, "new-consumer")
        assert new.generation > old.generation
        assert (
            first.renew_claim(
                node_id, old_owner, "old-consumer", *identities[0], old.generation
            ).state
            == "owner_mismatch"
        )
        assert (
            first.renew_claim(
                node_id, new_owner, "old-consumer", *identities[0], old.generation
            ).state
            == "owner_mismatch"
        )
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("queue", node),
        ]
        for client_id, request_id in identities:
            client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
            request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
            keys.extend(
                (cfg.key("request", client, request), cfg.key("claim", client, request))
            )
        first._foundation._client.delete(*keys)
        for store in stores:
            store.close()

def test_concurrent_renewal_and_reclaim_has_coherent_generation(valkey_server):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace, claim_ttl_seconds=0.08)
    second = _registration_store(valkey_server, namespace, claim_ttl_seconds=0.08)
    owner, node_id = _digest("race-owner"), "race-node"
    identity = ("race-client", "race-request")
    cfg = first._foundation.config
    node = first._node_digest(node_id)
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    try:
        first.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            first, node_id, owner, *identity, first._foundation.server_time()[0] + 60
        )
        old = first.claim_queued_request(node_id, owner, "old-consumer")
        _wait_for_server_epoch(first, old.lease_expires_at_epoch)
        barrier = Barrier(2)

        def renew():
            barrier.wait()
            return first.renew_claim(
                node_id, owner, "old-consumer", *identity, old.generation
            )

        def reclaim():
            barrier.wait()
            return second.claim_queued_request(node_id, owner, "new-consumer")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            renewal_future = pool.submit(renew)
            reclaim_future = pool.submit(reclaim)
        renewal, reclaimed = renewal_future.result(), reclaim_future.result()
        assert renewal.state in {"missing_or_expired", "stale_generation"}
        assert reclaimed.state == "reclaimed"
        assert reclaimed.generation > old.generation
        live = first.active_claims(node_id)
        assert len(live) == 1 and live[0].generation == reclaimed.generation
        assert first._foundation._client.xlen(cfg.key("queue", node)) == 1
        assert (
            len(first._foundation._client.hgetall(cfg.key("request", client, request)))
            > 0
        )
    finally:
        first._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", client, request),
            cfg.key("claim", client, request),
        )
        first.close()
        second.close()

@pytest.mark.parametrize("removal", ("unregister", "expiry"))
def test_concurrent_renewal_and_registration_removal_fences_former_owner(
    valkey_server, removal
):
    namespace = uuid.uuid4().hex
    first = _registration_store(
        valkey_server, namespace, lease_ttl_seconds=0.08, claim_ttl_seconds=2
    )
    second = _registration_store(
        valkey_server, namespace, lease_ttl_seconds=0.08, claim_ttl_seconds=2
    )
    owner, new_owner = _digest("removal-owner"), _digest("replacement-owner")
    node_id = "removal-node"
    identity = ("removal-client", "removal-request")
    cfg = first._foundation.config
    node = first._node_digest(node_id)
    client = hashlib.sha256(f"client\0{identity[0]}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{identity[1]}".encode()).hexdigest()
    try:
        registration = first.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            first, node_id, owner, *identity, first._foundation.server_time()[0] + 60
        )
        claim = first.claim_queued_request(node_id, owner, "removal-consumer")
        if removal == "expiry":
            _wait_for_server_epoch(first, registration.lease_expires_at_epoch)
        barrier = Barrier(2)

        def renew():
            barrier.wait()
            return first.renew_claim(
                node_id, owner, "removal-consumer", *identity, claim.generation
            )

        def remove():
            barrier.wait()
            if removal == "unregister":
                return second.unregister(node_id, owner)
            return second.register(node_id, _capabilities(), new_owner)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            renewal_future = pool.submit(renew)
            removal_future = pool.submit(remove)
        renewal = renewal_future.result()
        removed = removal_future.result()
        if removal == "unregister":
            assert renewal.state in {"continued", "owner_mismatch"}
        else:
            assert renewal.state == "owner_mismatch"
        assert removed
        assert (
            first.renew_claim(
                node_id, owner, "removal-consumer", *identity, claim.generation
            ).state
            == "owner_mismatch"
        )
    finally:
        first._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", client, request),
            cfg.key("claim", client, request),
        )
        first.close()
        second.close()

def test_claim_capacity_fails_closed_on_malformed_live_claim_authority(
    valkey_server,
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner = _digest("authority-owner")
    node_id = "authority-node"
    node = store._node_digest(node_id)
    client_id, request_id = "target-client", "target-request"
    client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
    malformed_client, malformed_request = "a" * 64, "b" * 64
    malformed_member = f"{malformed_client}:{malformed_request}"
    malformed_key = cfg.key("claim", malformed_client, malformed_request)
    deadline = time.time() + 60
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, client_id, request_id, deadline)
        target_key = cfg.key("request", client, request)
        cursor_before = store._foundation._client.hgetall(cfg.key("cursor"))

        store._foundation._client.hset(malformed_key, "node_digest", node)
        store._foundation._client.zadd(
            cfg.key("claims:expiry"), {malformed_member: deadline}
        )
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.claim_queued_request(node_id, owner, "consumer")
        assert store._foundation._client.hget(target_key, "state") == b"queued"
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before

        store._foundation._client.hset(
            malformed_key,
            mapping={
                "client": malformed_client,
                "request": malformed_request,
                "node_digest": node,
                "node_id": node_id,
                "owner_digest": owner,
                "consumer_digest": _digest("consumer-authority"),
                "deadline": deadline,
                "sequence": 1,
                "generation": 1,
                "lease_expires": deadline - 1,
            },
        )
        with pytest.raises(ValkeySchemaIncompatibleError):
            store.claim_queued_request(node_id, owner, "consumer")
        assert store._foundation._client.hget(target_key, "state") == b"queued"
        assert store._foundation._client.hgetall(cfg.key("cursor")) == cursor_before
    finally:
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", client, request),
            malformed_key,
        )
        store.close()


def test_claim_reclaim_is_independent_of_bounded_expired_index_cleanup(
    valkey_server,
):
    store = _registration_store(
        valkey_server,
        uuid.uuid4().hex,
        claim_ttl_seconds=30,
        node_transition_batch_size=1,
    )
    cfg = store._foundation.config
    owner, node_id = _digest("reclaim-owner"), "reclaim-node"
    node = store._node_digest(node_id)
    client_id, request_id = "reclaim-client", "reclaim-request"
    client = hashlib.sha256(f"client\0{client_id}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
    member = f"{client}:{request}"
    deadline = time.time() + 60
    backlog = [f"{'a' * 63}{i}:{'b' * 63}{i}" for i in range(3)]
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, client_id, request_id, deadline)
        first = store.claim_queued_request(node_id, owner, "consumer-one")
        expired = time.time() - 1
        store._foundation._client.hset(
            cfg.key("claim", client, request), "lease_expires", expired
        )
        store._foundation._client.zadd(cfg.key("claims:expiry"), {member: expired})
        store._foundation._client.zadd(
            cfg.key("claims:expiry"),
            {
                backlog_member: expired - index - 1
                for index, backlog_member in enumerate(backlog)
            },
        )

        reclaimed = store.claim_queued_request(node_id, owner, "consumer-two")

        assert reclaimed.state == "reclaimed"
        assert reclaimed.generation > first.generation
        assert (
            store._foundation._client.zscore(cfg.key("claims:expiry"), member) > expired
        )
        assert (
            sum(
                store._foundation._client.zscore(cfg.key("claims:expiry"), item)
                is not None
                for item in backlog
            )
            == len(backlog) - 1
        )
    finally:
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", client, request),
            cfg.key("claim", client, request),
        )
        store.close()


def test_claim_capacity_counts_only_complete_live_claims(valkey_server):
    store = _registration_store(
        valkey_server, uuid.uuid4().hex, max_claims=1, max_claims_per_node=1
    )
    cfg = store._foundation.config
    owner = _digest("capacity-owner")
    nodes = ("capacity-a", "capacity-b")
    digests = tuple(store._node_digest(node) for node in nodes)
    identities = (
        ("capacity-client-a", "capacity-request-a"),
        ("capacity-client-b", "capacity-request-b"),
        ("capacity-client-c", "capacity-request-c"),
    )
    hashed = tuple(
        (
            hashlib.sha256(f"client\0{client}".encode()).hexdigest(),
            hashlib.sha256(f"request\0{request}".encode()).hexdigest(),
        )
        for client, request in identities
    )
    deadline = time.time() + 60
    try:
        for node in nodes:
            store.register(node, _capabilities(), owner)
        for node, (client, request) in zip(nodes, identities[:2]):
            _enqueue_claim_fixture(store, node, owner, client, request, deadline)
        live = store.claim_queued_request(nodes[0], owner, "consumer-a")
        with pytest.raises(RelayStateCapacityExceeded):
            store.claim_queued_request(nodes[1], owner, "consumer-b")
        _enqueue_claim_fixture(store, nodes[0], owner, *identities[2], deadline)
        per_node_store = ValkeyRegistrationStore(
            store._foundation,
            dataclasses.replace(store.config, max_claims=2),
            acknowledgement_key=_ACKNOWLEDGEMENT_KEY,
        )
        with pytest.raises(RelayStateCapacityExceeded):
            per_node_store.claim_queued_request(nodes[0], owner, "consumer-c")

        first_client, first_request = hashed[0]
        expired = time.time() - 1
        store._foundation._client.hset(
            cfg.key("claim", first_client, first_request), "lease_expires", expired
        )
        store._foundation._client.zadd(
            cfg.key("claims:expiry"), {f"{first_client}:{first_request}": expired}
        )
        second = store.claim_queued_request(nodes[1], owner, "consumer-b")
        assert live.state == "claimed"
        assert second.state == "claimed"
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
        ]
        keys.extend(cfg.key("node", node) for node in digests)
        keys.extend(cfg.key("queue", node) for node in digests)
        for client, request in hashed:
            keys.extend(
                (cfg.key("request", client, request), cfg.key("claim", client, request))
            )
        store._foundation._client.delete(*keys)
        store.close()




def test_claim_capacity_fails_closed_on_nonlive_or_mismatched_lifecycle_authority(
    valkey_server,
):
    store = _registration_store(valkey_server, uuid.uuid4().hex, claim_ttl_seconds=30)
    cfg = store._foundation.config
    owner = _digest("lifecycle-authority-owner")
    authority_node, target_node = "authority-node", "target-node"
    authority_identity = ("authority-client", "authority-request")
    target_identity = ("target-client", "target-request")
    authority_digests, target_digests = tuple(
        (
            hashlib.sha256(f"client\0{client}".encode()).hexdigest(),
            hashlib.sha256(f"request\0{request}".encode()).hexdigest(),
        )
        for client, request in (authority_identity, target_identity)
    )
    authority_request_key = cfg.key("request", *authority_digests)
    authority_claim_key = cfg.key("claim", *authority_digests)
    target_request_key = cfg.key("request", *target_digests)
    target_claim_key = cfg.key("claim", *target_digests)
    authority_member = ":".join(authority_digests)
    deadline = time.time() + 120
    try:
        for node_id in (authority_node, target_node):
            store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(
            store, authority_node, owner, *authority_identity, deadline
        )
        store.claim_queued_request(authority_node, owner, "authority-consumer")
        _enqueue_claim_fixture(store, target_node, owner, *target_identity, deadline)

        client = store._foundation._client
        request_authority = client.hgetall(authority_request_key)
        claim_authority = client.hgetall(authority_claim_key)
        original_score = client.zscore(cfg.key("claims:expiry"), authority_member)
        seconds, micros = client.time()
        server_now = seconds + micros / 1_000_000
        cases = (
            (
                {b"deadline": str(server_now).encode()},
                {b"deadline": str(server_now).encode()},
                server_now + 20,
            ),
            (
                {b"deadline": str(server_now + 10).encode()},
                {b"deadline": str(server_now + 10).encode()},
                server_now + 20,
            ),
            (None, {}, original_score),
            ({b"state": b"queued"}, {}, original_score),
            ({b"node_id": b"other-node"}, {}, original_score),
            ({b"deadline": str(deadline - 1).encode()}, {}, original_score),
            ({b"sequence": b"999"}, {}, original_score),
            ({b"claim_generation": b"999"}, {}, original_score),
        )
        for lifecycle_changes, claim_changes, score in cases:
            client.delete(authority_request_key, authority_claim_key)
            client.hset(authority_claim_key, mapping=claim_authority)
            if lifecycle_changes is not None:
                client.hset(
                    authority_request_key,
                    mapping={**request_authority, **lifecycle_changes},
                )
            if claim_changes:
                client.hset(authority_claim_key, mapping=claim_changes)
            client.zadd(cfg.key("claims:expiry"), {authority_member: score})
            target_before = client.hgetall(target_request_key)
            cursor_before = client.hgetall(cfg.key("cursor"))
            capacity_before = client.zrange(
                cfg.key("claims:expiry"), 0, -1, withscores=True
            )

            with pytest.raises(ValkeySchemaIncompatibleError):
                store.claim_queued_request(target_node, owner, "target-consumer")

            assert client.hgetall(target_request_key) == target_before
            assert client.hgetall(cfg.key("cursor")) == cursor_before
            assert (
                client.zrange(cfg.key("claims:expiry"), 0, -1, withscores=True)
                == capacity_before
            )
            assert client.exists(target_claim_key) == 0
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            authority_request_key,
            authority_claim_key,
            target_request_key,
            target_claim_key,
        ]
        for node_id in (authority_node, target_node):
            node_digest = store._node_digest(node_id)
            keys.extend((cfg.key("node", node_digest), cfg.key("queue", node_digest)))
        store._foundation._client.delete(*keys)
        store.close()


def test_renew_claim_fails_closed_on_malformed_expiry_authority(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner, node_id = _digest("renew-authority-owner"), "renew-authority-node"
    identity = ("renew-authority-client", "renew-authority-request")
    client, request = (
        hashlib.sha256(f"{domain}\0{value}".encode()).hexdigest()
        for domain, value in zip(("client", "request"), identity)
    )
    member = f"{client}:{request}"
    claim_key = cfg.key("claim", client, request)
    request_key = cfg.key("request", client, request)
    node_digest = store._node_digest(node_id)
    deadline = time.time() + 60
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, *identity, deadline)
        claimed = store.claim_queued_request(node_id, owner, "renew-consumer")
        datastore = store._foundation._client

        for indexed_expiry in (None, claimed.lease_expires_at_epoch + 1):
            claim_before = datastore.hgetall(claim_key)
            request_before = datastore.hgetall(request_key)
            datastore.zrem(cfg.key("claims:expiry"), member)
            if indexed_expiry is not None:
                datastore.zadd(cfg.key("claims:expiry"), {member: indexed_expiry})
            index_before = datastore.zrange(
                cfg.key("claims:expiry"), 0, -1, withscores=True
            )

            with pytest.raises(
                ValkeySchemaIncompatibleError, match="^state schema incompatible$"
            ):
                store.renew_claim(
                    node_id,
                    owner,
                    "renew-consumer",
                    *identity,
                    claimed.generation,
                )

            assert datastore.hgetall(claim_key) == claim_before
            assert datastore.hgetall(request_key) == request_before
            assert (
                datastore.zrange(cfg.key("claims:expiry"), 0, -1, withscores=True)
                == index_before
            )
    finally:
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            request_key,
            claim_key,
        )
        store.close()


@pytest.mark.parametrize(
    ("record", "field", "value"),
    (
        ("claim", "node_digest", b"\xffredacted-invalid-utf8"),
        ("request", "model", b"m" * 129),
        ("request", "client_public_key", b"i" * 8193),
        ("claim", "deadline", b"nan"),
        ("claim", "lease_expires", b"inf"),
        ("claim", "lease_expires", b"9999999999"),
        ("request", "enqueued_at", b"-inf"),
        ("request", "queue_entry", b"01-0"),
    ),
)
def test_claim_reads_fail_closed_on_malformed_bounded_fields(
    valkey_server, record, field, value
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner, node_id = _digest("read-authority-owner"), "read-authority-node"
    identity = ("read-authority-client", "read-authority-request")
    client, request = (
        hashlib.sha256(f"{domain}\0{item}".encode()).hexdigest()
        for domain, item in zip(("client", "request"), identity)
    )
    node_digest = store._node_digest(node_id)
    keys = {
        "claim": cfg.key("claim", client, request),
        "request": cfg.key("request", client, request),
    }
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, *identity, time.time() + 60)
        store.claim_queued_request(node_id, owner, "read-consumer")
        store._foundation._client.hset(keys[record], field, value)
        if field == "lease_expires" and value == b"9999999999":
            store._foundation._client.zadd(
                cfg.key("claims:expiry"), {f"{client}:{request}": float(value)}
            )

        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ) as caught:
            store.active_claims(node_id)

        rendered = f"{caught.value!s}{caught.value!r}"
        assert "redacted-invalid-utf8" not in rendered
    finally:
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            *keys.values(),
        )
        store.close()


def test_claim_reads_tolerate_complete_post_snapshot_removal(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner, node_id = _digest("removed-read-owner"), "removed-read-node"
    identity = ("removed-read-client", "removed-read-request")
    client, request = (
        hashlib.sha256(f"{domain}\0{item}".encode()).hexdigest()
        for domain, item in zip(("client", "request"), identity)
    )
    node_digest = store._node_digest(node_id)
    claim_key = cfg.key("claim", client, request)
    request_key = cfg.key("request", client, request)
    original_call = store._foundation._call
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, *identity, time.time() + 60)
        store.claim_queued_request(node_id, owner, "removed-read-consumer")

        def remove_before_claim_read(operation, *args, **kwargs):
            if operation == store._foundation._client.hmget and args[0] == claim_key:
                store._foundation._client.delete(claim_key, request_key)
            return original_call(operation, *args, **kwargs)

        store._foundation._call = remove_before_claim_read
        assert store.active_claims(node_id) == ()
    finally:
        store._foundation._call = original_call
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            request_key,
            claim_key,
        )
        store.close()


@pytest.mark.parametrize("lifecycle_node_digest", (None, "0" * 64))
@pytest.mark.parametrize("reader", ("active_claims", "claimed_request"))
def test_claim_reads_require_matching_request_node_digest(
    valkey_server, lifecycle_node_digest, reader
):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner, node_id = _digest("lifecycle-digest-owner"), "lifecycle-digest-node"
    identity = ("lifecycle-digest-client", "lifecycle-digest-request")
    client, request = (
        hashlib.sha256(f"{domain}\0{value}".encode()).hexdigest()
        for domain, value in zip(("client", "request"), identity)
    )
    node_digest = store._node_digest(node_id)
    claim_key = cfg.key("claim", client, request)
    request_key = cfg.key("request", client, request)
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, *identity, time.time() + 60)
        store.claim_queued_request(node_id, owner, "lifecycle-digest-consumer")
        if lifecycle_node_digest is None:
            store._foundation._client.hdel(request_key, "node_digest")
        else:
            store._foundation._client.hset(
                request_key, "node_digest", lifecycle_node_digest
            )

        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ):
            if reader == "active_claims":
                store.active_claims(node_id)
            else:
                store.claimed_request(node_id, identity[1])
    finally:
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node_digest),
            cfg.key("queue", node_digest),
            request_key,
            claim_key,
        )
        store.close()


def test_renew_claim_fails_closed_on_malformed_node_lease_authority(valkey_server):
    store = _registration_store(valkey_server, uuid.uuid4().hex)
    cfg = store._foundation.config
    owner, node_id = _digest("renew-node-lease-owner"), "renew-node-lease-node"
    identity = ("renew-node-lease-client", "renew-node-lease-request")
    client, request = (
        hashlib.sha256(f"{domain}\0{value}".encode()).hexdigest()
        for domain, value in zip(("client", "request"), identity)
    )
    member = f"{client}:{request}"
    node_digest = store._node_digest(node_id)
    node_key = cfg.key("node", node_digest)
    claim_key = cfg.key("claim", client, request)
    request_key = cfg.key("request", client, request)
    try:
        store.register(node_id, _capabilities(), owner)
        _enqueue_claim_fixture(store, node_id, owner, *identity, time.time() + 60)
        claimed = store.claim_queued_request(
            node_id, owner, "renew-node-lease-consumer"
        )
        datastore = store._foundation._client
        datastore.hset(node_key, "lease_expires_at_epoch", "+inf")
        datastore.zadd(cfg.key("nodes:lease"), {node_digest: float("inf")})
        claim_before = datastore.hgetall(claim_key)
        request_before = datastore.hgetall(request_key)
        index_before = datastore.zscore(cfg.key("claims:expiry"), member)

        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ):
            store.renew_claim(
                node_id,
                owner,
                "renew-node-lease-consumer",
                *identity,
                claimed.generation,
            )

        assert datastore.hgetall(claim_key) == claim_before
        assert datastore.hgetall(request_key) == request_before
        assert datastore.zscore(cfg.key("claims:expiry"), member) == index_before
    finally:
        store._foundation._client.delete(
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            node_key,
            cfg.key("queue", node_digest),
            request_key,
            claim_key,
        )
        store.close()


def test_claim_result_budget_accepts_large_bounded_result(valkey_server):
    namespace = uuid.uuid4().hex
    max_envelope_bytes = 20_000
    store = _registration_store(
        valkey_server,
        namespace,
        max_envelope_bytes=max_envelope_bytes,
        claim_ttl_seconds=1,
    )
    owner = _digest("large-claim-owner")
    client_public_key = "c" * (store.config.max_identity_bytes - 1)
    request_id = "r" * (store.config.max_identity_bytes - 1)
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee",
        1,
        "x" * 18_000,
        "cipherkey",
        "iv",
    )
    deadline = time.time() + 10
    cfg = store._foundation.config
    selection = None
    node = store._node_digest("large-claim-node")
    client = hashlib.sha256(f"client\0{client_public_key}".encode()).hexdigest()
    request = hashlib.sha256(f"request\0{request_id}".encode()).hexdigest()
    try:
        store.register("large-claim-node", _capabilities(), owner)
        selection = store.select_and_reserve(
            client_public_key,
            request_id,
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            "cancel",
        )
        store.enqueue_encrypted_request(
            client_public_key,
            request_id,
            selection.reservation_token,
            "large-claim-node",
            "qwen3-8b-instruct",
            "8k-fast",
            deadline,
            envelope,
            "cancel",
        )

        claim = store.claim_queued_request(
            "large-claim-node", owner, "large-result-consumer"
        )

        assert claim.state == "claimed"
        assert claim.client_public_key == client_public_key
        assert claim.request_id == request_id
        assert claim.envelope == envelope
        assert len(store.active_claims("large-claim-node")) == 1
        assert (
            store._foundation._client.xlen(
                cfg.key("queue", store._node_digest("large-claim-node"))
            )
            == 1
        )
    finally:
        keys = [
            cfg.key("schema"),
            cfg.key("nodes:lease"),
            cfg.key("cursor"),
            cfg.key("reservations:expiry"),
            cfg.key("requests:deadline"),
            cfg.key("claims:expiry"),
            cfg.key("node", node),
            cfg.key("queue", node),
            cfg.key("request", client, request),
            cfg.key("claim", client, request),
        ]
        if selection is not None and selection.reservation_token is not None:
            keys.append(cfg.key("reservation", _digest(selection.reservation_token)))
        store._foundation._client.delete(*keys)
        store.close()


def test_completed_record_reads_hide_expired_backlog_beyond_cleanup_batch(valkey_server):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=0.05,
        terminal_retention_seconds=60,
        control_tombstone_ttl_seconds=0.001,
        node_transition_batch_size=1,
    )
    node, owner, consumer = "expiry-node", _digest("expiry-owner"), "expiry-consumer"
    identities = tuple(("expiry-client", f"expiry-request-{index}") for index in range(3))
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    cfg = store._foundation.config
    try:
        store.register(node, _capabilities(), owner)
        for identity in identities:
            _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
            claim = store.claim_queued_request(node, owner, consumer)
            store.accept_encrypted_response(
                node, owner, consumer, *identity, claim.generation, response
            )
        response_keys = tuple(
            cfg.key("response", *store._identity(*identity)) for identity in identities
        )
        terminal_keys = tuple(
            cfg.key("terminal", *store._identity(*identity)) for identity in identities
        )
        response_index = cfg.key("responses:expiry")
        terminal_index = cfg.key("terminals:expiry")
        for _ in range(200):
            seconds, micros = store._foundation.server_time()
            if all(
                store._foundation._client.zscore(
                    response_index, ":".join(store._identity(*identity))
                )
                <= seconds + micros / 1_000_000
                for identity in identities
            ):
                break
            time.sleep(0.01)
        else:
            pytest.fail("authoritative Valkey time did not reach response expiry")

        records = store.terminal_records()
        assert len(records) == 3
        assert {record.retrieval_state for record in records} == {"retrieval_expired"}
        assert sum(store._foundation._client.exists(key) for key in response_keys) == 2
        assert store._foundation._client.zcard(response_index) == 2
        assert store._foundation._client.zcard(terminal_index) == 3
        assert sorted(
            store._foundation._client.hget(key, "retrieval_state")
            for key in terminal_keys
        ) == [b"response_ready", b"response_ready", b"retrieval_expired"]
    finally:
        _delete_claim_fixture_state(store, (node,), identities)
        store.close()


@pytest.mark.parametrize("inspector", ("response_records", "terminal_records"))
@pytest.mark.parametrize(
    ("authority", "field", "replacement"),
    (
        ("request", "claim_generation", "999"),
        ("request", "node_digest", "0" * 64),
        ("terminal", "retrieval_credential_digest", "0" * 64),
        ("terminal", "cancellation_token_digest", "0" * 64),
    ),
)
def test_completed_record_inspectors_validate_paired_authority_and_additive_fields(
    valkey_server, inspector, authority, field, replacement
):
    namespace = uuid.uuid4().hex
    store = _registration_store(
        valkey_server,
        namespace,
        response_replay_ttl_seconds=60,
        terminal_retention_seconds=600,
    )
    node, owner, consumer = "inspect-node", _digest("inspect-owner"), "inspect-consumer"
    identity = ("inspect-client", "inspect-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    client, request = store._identity(*identity)
    cfg = store._foundation.config
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )
        terminal_key = cfg.key("terminal", client, request)
        response_key = cfg.key("response", client, request)
        store._foundation._client.hset(terminal_key, "compatible_extension", "retained")
        store._foundation._client.hset(response_key, "compatible_extension", "retained")

        records = getattr(store, inspector)()
        assert len(records) == 1
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            records[0].generation = 99

        store._foundation._client.hset(
            cfg.key(authority, client, request), field, replacement
        )
        with pytest.raises(ValkeySchemaIncompatibleError) as caught:
            getattr(store, inspector)()
        rendered = repr(caught.value)
        assert "ciphertext" not in rendered
        assert client not in rendered and request not in rendered
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


@pytest.mark.parametrize(
    ("inspector", "index_name", "limit_name"),
    (
        ("response_records", "responses:expiry", "max_responses"),
        ("terminal_records", "terminals:expiry", "max_terminal_records"),
    ),
)
def test_completed_record_inspector_detects_live_index_overflow(
    valkey_server, inspector, index_name, limit_name
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace, **{limit_name: 1})
    cfg = store._foundation.config
    first = b"a" * 64 + b":" + b"b" * 64
    second = b"c" * 64 + b":" + b"d" * 64
    try:
        now = store._foundation.server_time()[0]
        store._foundation._client.zadd(
            cfg.key(index_name), {first: now + 100, second: now + 100}
        )
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="state schema incompatible"
        ):
            getattr(store, inspector)()
    finally:
        store._foundation._client.delete(cfg.key(index_name))
        store.close()


@pytest.mark.parametrize(
    ("inspector", "primary_name", "index_name"),
    (
        ("response_records", "response", "responses:expiry"),
        ("terminal_records", "terminal", "terminals:expiry"),
    ),
)
@pytest.mark.parametrize("mutation", ("disappear", "hash_only", "index_only"))
def test_completed_record_inspector_primary_authority_race(
    valkey_server, monkeypatch, inspector, primary_name, index_name, mutation
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    node, owner, consumer = "race-node", _digest("race-owner"), "race-consumer"
    identity = ("race-client", "race-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    client, request = store._identity(*identity)
    cfg = store._foundation.config
    primary_key = cfg.key(primary_name, client, request)
    index_key = cfg.key(index_name)
    member = f"{client}:{request}"
    original = store._completed_hash
    hook_calls = 0
    primary_reads = 0
    after_mutation = None

    def snapshot():
        hash_keys = (
            cfg.key("request", client, request),
            cfg.key("response", client, request),
            cfg.key("terminal", client, request),
        )
        index_keys = (
            cfg.key("responses:expiry"),
            cfg.key("terminals:expiry"),
        )
        return (
            tuple(
                sorted(store._foundation._client.hgetall(key).items())
                for key in hash_keys
            ),
            tuple(
                store._foundation._client.zrange(key, 0, -1, withscores=True)
                for key in index_keys
            ),
        )

    def mutate_before_primary_read(key, fields, limits):
        nonlocal hook_calls, primary_reads, after_mutation
        if key == primary_key:
            primary_reads += 1
            assert primary_reads <= 2
            if hook_calls == 0:
                hook_calls += 1
                if mutation != "hash_only":
                    store._foundation._client.delete(primary_key)
                if mutation != "index_only":
                    store._foundation._client.zrem(index_key, member)
                after_mutation = snapshot()
        return original(key, fields, limits)

    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )
        monkeypatch.setattr(store, "_completed_hash", mutate_before_primary_read)

        if mutation == "disappear":
            assert getattr(store, inspector)() == ()
        else:
            with pytest.raises(
                ValkeySchemaIncompatibleError, match="state schema incompatible"
            ) as caught:
                getattr(store, inspector)()
            assert "ciphertext" not in repr(caught.value)
        assert hook_calls == 1
        assert snapshot() == after_mutation
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_completed_record_terminal_inspector_retries_cleanup_race(
    valkey_server, monkeypatch
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    node, owner, consumer = "transition-node", _digest("transition-owner"), "consumer"
    identity = ("transition-client", "transition-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    client, request = store._identity(*identity)
    cfg = store._foundation.config
    response_key = cfg.key("response", client, request)
    terminal_key = cfg.key("terminal", client, request)
    response_index = cfg.key("responses:expiry")
    member = f"{client}:{request}"
    original = store._completed_hash
    hook_calls = 0

    def transition_before_response_read(key, fields, limits):
        nonlocal hook_calls
        if key == response_key:
            hook_calls += 1
            assert hook_calls == 1
            store._foundation._client.delete(response_key)
            store._foundation._client.zrem(response_index, member)
            store._foundation._client.hset(
                terminal_key, "retrieval_state", "retrieval_expired"
            )
        return original(key, fields, limits)

    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )
        monkeypatch.setattr(store, "_completed_hash", transition_before_response_read)

        records = store.terminal_records()
        assert len(records) == 1
        assert records[0].retrieval_state == "retrieval_expired"
        assert hook_calls == 1
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def test_completed_record_terminal_inspector_rejects_unknown_response_authority(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    store = _registration_store(valkey_server, namespace)
    node, owner, consumer = "unknown-node", _digest("unknown-owner"), "consumer"
    identity = ("unknown-client", "unknown-request")
    response = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )
    client, request = store._identity(*identity)
    cfg = store._foundation.config
    response_key = cfg.key("response", client, request)
    terminal_key = cfg.key("terminal", client, request)
    response_index = cfg.key("responses:expiry")
    member = f"{client}:{request}"
    try:
        store.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(store, node, owner, *identity, time.time() + 60)
        claim = store.claim_queued_request(node, owner, consumer)
        store.accept_encrypted_response(
            node, owner, consumer, *identity, claim.generation, response
        )
        store._foundation._client.delete(response_key)
        store._foundation._client.zrem(response_index, member)
        store._foundation._client.hset(
            terminal_key, "retrieval_state", "retrieval_expired"
        )
        store._foundation._client.hset(response_key, "compatible_extension", "retained")
        before = (
            store._foundation._client.dump(response_key),
            store._foundation._client.dump(terminal_key),
            store._foundation._client.dump(response_index),
        )

        with pytest.raises(
            ValkeySchemaIncompatibleError, match="state schema incompatible"
        ) as caught:
            store.terminal_records()
        assert "ciphertext" not in repr(caught.value)
        assert before == (
            store._foundation._client.dump(response_key),
            store._foundation._client.dump(terminal_key),
            store._foundation._client.dump(response_index),
        )
    finally:
        _delete_claim_fixture_state(store, (node,), (identity,))
        store.close()


def _retained_authority_keys(store, node_id, identity):
    cfg = store._foundation.config
    client, request = store._identity(*identity)
    node = store._node_digest(node_id)
    return {
        "request": cfg.key("request", client, request),
        "response": cfg.key("response", client, request),
        "terminal": cfg.key("terminal", client, request),
        "control": cfg.key("control", node, client, request),
        "response_index": cfg.key("responses:expiry"),
        "terminal_index": cfg.key("terminals:expiry"),
        "control_index": cfg.key("control:expiry"),
        "deadline_index": cfg.key("requests:deadline"),
        "member": f"{client}:{request}",
        "control_member": f"{node}:{client}:{request}",
    }


def _force_retained_authority_due(store, keys, *, completed=False):
    client = store._foundation._client
    seconds, micros = store._foundation.server_time()
    now = seconds + micros / 1_000_000
    accepted, replay, terminal_expiry = now - 3, now - 2, now - 1
    if client.hget(keys["terminal"], "outcome") == b"expired":
        client.hset(keys["request"], "deadline", str(now - 4))
    if completed:
        client.hset(
            keys["response"],
            mapping={
                "accepted_at_epoch": str(accepted),
                "replay_expires_at_epoch": str(replay),
            },
        )
        client.zadd(keys["response_index"], {keys["member"]: replay})
    else:
        replay = accepted
    client.hset(
        keys["terminal"],
        mapping={
            "accepted_at_epoch": str(accepted),
            "replay_expires_at_epoch": str(replay),
            "expires_at_epoch": str(terminal_expiry),
        },
    )
    client.zadd(keys["terminal_index"], {keys["member"]: terminal_expiry})
    if client.exists(keys["control"]):
        client.hset(keys["control"], "expires_at_epoch", str(terminal_expiry))
        client.zadd(
            keys["control_index"], {keys["control_member"]: terminal_expiry}
        )


def test_retention_reaper_reclaims_capacity_only_after_complete_validation(
    valkey_server,
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    constrained = _registration_store(
        valkey_server,
        namespace,
        max_terminal_records=3,
        max_terminal_records_per_client=3,
        max_control_tombstones=1,
        max_control_tombstones_per_node=1,
    )
    node, owner, consumer = "retention-node", _digest("retention-owner"), "consumer"
    identities = tuple((f"retention-client-{i}", f"retention-request-{i}") for i in range(4))
    keys = []
    try:
        first.register(node, _capabilities(), owner)
        # A completed response exercises the response-ready retained shape.
        _enqueue_claim_fixture(first, node, owner, *identities[2], time.time() + 60)
        claim = first.claim_queued_request(node, owner, consumer)
        first.accept_encrypted_response(
            node,
            owner,
            consumer,
            *identities[2],
            claim.generation,
            EncryptedResponseEnvelope(
                "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
            ),
        )
        keys.append(_retained_authority_keys(first, node, identities[2]))

        # A claimed cancellation supplies the paired control authority.
        _enqueue_claim_fixture(first, node, owner, *identities[0], time.time() + 60)
        first.claim_queued_request(node, owner, consumer)
        first.cancel_or_expire_request(*identities[0], "cancel")
        keys.append(_retained_authority_keys(first, node, identities[0]))

        # Build an authoritative reserved deadline expiry.
        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + 30
        selection = first.select_and_reserve(
            *identities[1], "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        expired_keys = _retained_authority_keys(first, node, identities[1])
        past = seconds + micros / 1_000_000 - 1
        reservation = first._foundation.config.key(
            "reservation", hashlib.sha256(selection.reservation_token.encode()).hexdigest()
        )
        first._foundation._client.hset(expired_keys["request"], "deadline", str(past))
        first._foundation._client.hset(reservation, "deadline", str(past))
        first._foundation._client.zadd(
            expired_keys["deadline_index"], {expired_keys["member"]: past}
        )
        first.cancel_or_expire_request(
            *identities[1], status="expired", reason="request_deadline_expired"
        )
        keys.append(expired_keys)


        for index, authority in enumerate(keys):
            _force_retained_authority_due(first, authority, completed=authority == keys[0])

        # All three terminal slots and the control slot are occupied until this
        # transition validates and atomically reaps the complete bounded batch.
        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + 60
        selection = constrained.select_and_reserve(
            *identities[3], "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        assert constrained.cancel_or_expire_request(
            *identities[3], "cancel"
        ).new_outcome
        assert selection.reservation_token
        assert first._foundation._client.zcard(keys[0]["terminal_index"]) == 1
        assert first._foundation._client.zcard(keys[0]["control_index"]) == 0
    finally:
        _delete_claim_fixture_state(first, (node,), identities)
        first._foundation._client.delete(
            first._foundation.config.key("control:expiry"),
            *(authority[name] for authority in keys for name in ("control",)),
        )
        first.close()
        constrained.close()


@pytest.mark.parametrize(
    ("candidate", "corruption"),
    (
        ("due", "partial_terminal"),
        ("due", "oversized_node"),
        ("due", "noncanonical_time"),
        ("due", "missing_request"),
        ("due", "score_mismatch"),
        ("due", "partial_control"),
        ("due", "control_score_mismatch"),
        ("due", "orphan_control"),
        ("due", "orphan_response"),
        ("due", "cross_record_identity"),
        ("due", "cross_record_request"),
        ("due", "lifecycle_generation_mismatch"),
        ("due", "claimed_lifecycle_fields_missing"),
        ("live", "partial_terminal"),
        ("live", "score_mismatch"),
        ("live", "lifecycle_generation_mismatch"),
        ("live", "claimed_lifecycle_fields_missing"),
    ),
)
def test_retention_reaper_rejects_malformed_authority_without_mutation(
    valkey_server, candidate, corruption
):
    namespace = uuid.uuid4().hex
    first = _registration_store(valkey_server, namespace)
    second = _registration_store(valkey_server, namespace)
    node, owner, consumer = "malformed-node", _digest("malformed-owner"), "consumer"
    retained = ("malformed-client", "malformed-request")
    attempted = ("attempted-client", "attempted-request")
    authority = _retained_authority_keys(first, node, retained)
    attempted_authority = _retained_authority_keys(first, node, attempted)
    cfg = first._foundation.config
    client = first._foundation._client
    try:
        first.register(node, _capabilities(), owner)
        _enqueue_claim_fixture(first, node, owner, *retained, time.time() + 60)
        first.claim_queued_request(node, owner, consumer)
        first.cancel_or_expire_request(*retained, "cancel")
        if candidate == "due":
            _force_retained_authority_due(first, authority)

        if corruption == "partial_terminal":
            client.hdel(authority["terminal"], "reason")
        elif corruption == "oversized_node":
            client.hset(authority["terminal"], "node_id", "x" * 8193)
        elif corruption == "noncanonical_time":
            client.hset(authority["terminal"], "accepted_at_epoch", "01")
        elif corruption == "missing_request":
            client.delete(authority["request"])
        elif corruption == "score_mismatch":
            score = client.zscore(authority["terminal_index"], authority["member"])
            client.zadd(authority["terminal_index"], {authority["member"]: score + 1})
        elif corruption == "partial_control":
            client.hdel(authority["control"], "reason")
        elif corruption == "control_score_mismatch":
            score = client.zscore(authority["control_index"], authority["control_member"])
            client.zadd(authority["control_index"], {authority["control_member"]: score + 1})
        elif corruption == "orphan_control":
            client.delete(authority["terminal"])
            client.zrem(authority["terminal_index"], authority["member"])
        elif corruption == "orphan_response":
            client.hset(authority["response"], "client", _digest("orphan"))
        elif corruption == "cross_record_request":
            client.hset(authority["request"], "node_id", "other-node")
        elif corruption == "lifecycle_generation_mismatch":
            client.hset(authority["request"], "claim_generation", "2")
        elif corruption == "claimed_lifecycle_fields_missing":
            client.hdel(
                authority["request"],
                "client_public_key",
                "request_id",
                "sequence",
                "queue_entry",
                "envelope",
            )
        else:
            client.hset(authority["control"], "request", _digest("other-request"))

        seconds, micros = first._foundation.server_time()
        deadline = seconds + micros / 1_000_000 + 60
        second.select_and_reserve(
            *attempted, "qwen3-8b-instruct", "8k-fast", deadline, "cancel"
        )
        snapshot_keys = tuple(
            dict.fromkeys(
                (
                    authority["request"],
                    authority["response"],
                    authority["terminal"],
                    authority["control"],
                    authority["response_index"],
                    authority["terminal_index"],
                    authority["control_index"],
                    attempted_authority["request"],
                    cfg.key("reservation", (client.hget(
                        attempted_authority["request"], "token_digest"
                    ) or b"").decode()),
                    cfg.key("reservations:expiry"),
                    attempted_authority["deadline_index"],
                )
            )
        )
        def snapshot():
            values = []
            for key in snapshot_keys:
                kind = client.type(key)
                if kind == b"hash":
                    value = tuple(sorted(client.hgetall(key).items()))
                elif kind == b"zset":
                    value = tuple(client.zrange(key, 0, -1, withscores=True))
                else:
                    value = client.get(key) if kind == b"string" else None
                values.append((kind, value))
            return tuple(values)

        before = snapshot()
        with pytest.raises(ValkeySchemaIncompatibleError):
            second.cancel_or_expire_request(*attempted, "cancel")
        assert snapshot() == before
    finally:
        _delete_claim_fixture_state(first, (node,), (retained, attempted))
        client.delete(authority["control"], authority["control_index"])
        first.close()
        second.close()
