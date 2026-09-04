import dataclasses
import hashlib
import json
import logging
import math
import re
import traceback
from unittest.mock import MagicMock, Mock, patch

import pytest
import redis
from redis.sentinel import MasterNotFoundError
import valkey_relay_state

from valkey_relay_state import (
    ACCEPT_RESPONSE_SCRIPT,
    DirectPrimary,
    ReviewedScript,
    SchemaManifest,
    SentinelPrimary,
    ValkeyConfig,
    ValkeyConfigurationError,
    ValkeyFoundation,
    ValkeyRegistrationStore,
    ValkeyReadOnlyError,
    ValkeySchemaIncompatibleError,
    ValkeyScriptError,
    ValkeyUnavailableError,
    REGISTRATION_TRANSITION_SCRIPT,
    SCRIPT_DIGESTS,
    SERVER_TIME_SCRIPT,
)
from relay_state_store import (
    EncryptedRequestEnvelope,
    EncryptedResponseEnvelope,
    RelayStateCapacityExceeded,
    RelayStateConflict,
    RelayStateCredentialMismatch,
    RelayStateInvalidReservation,
    RelayStateStoreConfig,
    RelayStateStoreError,
    SchedulerNodeState,
)


@pytest.mark.parametrize("key", [None, "x" * 32, bytearray(32), b"x" * 31])
def test_acknowledgement_key_is_required_exact_bytes_and_redacted(key):
    foundation = object.__new__(ValkeyFoundation)
    with pytest.raises(
        RelayStateStoreError, match="acknowledgement key is invalid"
    ) as caught:
        ValkeyRegistrationStore(
            foundation,
            RelayStateStoreConfig(namespace="testing.unit"),
            acknowledgement_key=key,
        )
    assert repr(key) not in repr(caught.value)


def test_acknowledgement_key_is_copied_and_never_represented():
    foundation = object.__new__(ValkeyFoundation)
    key = b"shared-test-acknowledgement-key-32"
    store = ValkeyRegistrationStore(
        foundation,
        RelayStateStoreConfig(namespace="testing.unit"),
        acknowledgement_key=key,
    )
    assert store._acknowledgement_key == key
    assert store._acknowledgement_key is not key
    assert key.decode() not in repr(store)


def test_response_serialization_is_canonical_sorted_utf8():
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "cipher-☃", "key", "iv"
    )
    assert ValkeyRegistrationStore._serialized_response_envelope(envelope) == (
        b'{"cipherkey":"key","ciphertext":"cipher-\xe2\x98\x83","iv":"iv",'
        b'"protocol":"tokenplace_api_v1_relay_e2ee","version":1}'
    )


@pytest.mark.parametrize(
    "raw",
    (
        b"[]",
        b'{"protocol":"tokenplace_api_v1_relay_e2ee"}',
        (
            b'{"protocol":"tokenplace_api_v1_relay_e2ee", "version":1,'
            b'"ciphertext":"cipher","cipherkey":"key","iv":"iv"}'
        ),
    ),
)
def test_response_envelope_decoder_rejects_malformed_or_noncanonical_bytes(raw):
    with pytest.raises(
        ValkeySchemaIncompatibleError, match="state schema incompatible"
    ):
        ValkeyRegistrationStore._decode_response_envelope(raw)


@pytest.mark.parametrize(
    ("generation", "envelope", "maximum", "message"),
    (
        (
            0,
            EncryptedResponseEnvelope(
                "tokenplace_api_v1_relay_e2ee", 1, "c", "k", "i"
            ),
            1024,
            "generation",
        ),
        (1, object(), 1024, "response envelope"),
        (
            1,
            EncryptedResponseEnvelope(
                "tokenplace_api_v1_relay_e2ee", 1, "c", "k", "i"
            ),
            1,
            "byte bound",
        ),
    ),
)
def test_accept_response_rejects_invalid_inputs_before_backend_access(
    generation, envelope, maximum, message
):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    store = registration_store_with_foundation(foundation)
    store._config = dataclasses.replace(
        store.config, max_response_envelope_bytes=maximum
    )

    with pytest.raises(RelayStateStoreError, match=message):
        store.accept_encrypted_response(
            "node", "a" * 64, "consumer", "client", "request", generation, envelope
        )

    foundation.server_time.assert_not_called()
    foundation.execute.assert_not_called()


def test_completed_inspector_index_detects_overflow_and_malformed_shapes():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    store = registration_store_with_foundation(foundation)
    member = b"a" * 64 + b":" + b"b" * 64
    foundation._call.return_value = [(member, 2.0), (member, 3.0)]

    with pytest.raises(
        ValkeySchemaIncompatibleError, match="state schema incompatible"
    ):
        store._completed_index_members("index", 1.0, 1)
    assert foundation._call.call_args.kwargs["num"] == 2

    foundation._call.return_value = [(member,)]
    with pytest.raises(
        ValkeySchemaIncompatibleError, match="state schema incompatible"
    ):
        store._completed_index_members("index", 1.0, 1)


def test_completed_inspector_rejects_raw_size_before_decode_or_hash():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    foundation._call.return_value = [b"oversized"]
    store = registration_store_with_foundation(foundation)

    with patch("valkey_relay_state.hashlib.sha256") as sha256:
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="state schema incompatible"
        ):
            store._completed_hash("key", (b"digest",), {b"digest": 2})
    sha256.assert_not_called()


def test_completed_inspector_distinguishes_disappearance_from_remaining_authority():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    store = registration_store_with_foundation(foundation)
    member = b"a" * 64 + b":" + b"b" * 64

    pipeline = MagicMock()
    foundation._client.pipeline.return_value = pipeline
    pipeline.__enter__.return_value = pipeline
    foundation._call.return_value = [0, None]
    assert store._completed_primary_authority("hash", "index", member) == (0, None)
    pipeline.exists.assert_called_once_with("hash")
    pipeline.zscore.assert_called_once_with("index", member)

    foundation._call.return_value = [1, 2.0]
    assert store._completed_primary_authority("hash", "index", member) == (1, 2.0)


def test_accept_response_script_is_registered_digest_pinned_and_bounded():
    expected_digest = "3559da1040624e6ac52d933a3d116c680d0398243ca4c68b308d0e1c4e10ecd8"  # pragma: allowlist secret
    assert ACCEPT_RESPONSE_SCRIPT.sha256 == expected_digest
    assert SCRIPT_DIGESTS[ACCEPT_RESPONSE_SCRIPT.name] == ACCEPT_RESPONSE_SCRIPT.sha256
    assert hashlib.sha256(ACCEPT_RESPONSE_SCRIPT.source.encode()).hexdigest() == expected_digest
    assert not re.search(
        r"redis\.call\(['\"](?:SCAN|KEYS|FLUSHALL|FLUSHDB|CONFIG)['\"]",
        ACCEPT_RESPONSE_SCRIPT.source,
    )
    stale_guard = (
        "if not replay or not terminal_expiry or replay<=now or terminal_expiry<=now "
        "then return {'malformed'} end"
    )
    assert stale_guard in ACCEPT_RESPONSE_SCRIPT.source
    assert ACCEPT_RESPONSE_SCRIPT.source.index(stale_guard) < (
        ACCEPT_RESPONSE_SCRIPT.source.index("redis.call('HSET',response")
    )
    assert "if not accepted or accepted<0 or accepted>now then return {'malformed'} end" in ACCEPT_RESPONSE_SCRIPT.source
    cleanup_offset = ACCEPT_RESPONSE_SCRIPT.source.index("local function cleanup()")
    assert ACCEPT_RESPONSE_SCRIPT.source.index("validate_response_due(response_due)", cleanup_offset) < ACCEPT_RESPONSE_SCRIPT.source.index("reap(response_due,terminal_due)", cleanup_offset)
    assert "'client_public_key','request_id','node_id','consumer_digest','generation','envelope'" in ACCEPT_RESPONSE_SCRIPT.source
    assert "'retrieval_credential_digest','acknowledgement_digest','cancellation_token_digest','client','request'" in ACCEPT_RESPONSE_SCRIPT.source
    assert "local lv=redis.call('HMGET',lk,'state','client','request','client_public_key','request_id'" in ACCEPT_RESPONSE_SCRIPT.source
    response_validator = ACCEPT_RESPONSE_SCRIPT.source.split(
        "local function validate_response_due(due)", 1
    )[1].split("local function contains", 1)[0]
    terminal_validator = ACCEPT_RESPONSE_SCRIPT.source.split(
        "local function validate_terminal_due(due,response_due)", 1
    )[1].split("local function reap", 1)[0]
    for validator in (response_validator, terminal_validator):
        assert "tv[12]~=lv[12]" in validator
        assert "tv[14]~=lv[13]" in validator
    assert ACCEPT_RESPONSE_SCRIPT.source.index("validate_terminal_due(terminal_due,response_due)", cleanup_offset) < ACCEPT_RESPONSE_SCRIPT.source.index("reap(response_due,terminal_due)", cleanup_offset)
    assert "redis.call('EXISTS',response)~=0 or response_indexed or terminal_indexed" in ACCEPT_RESPONSE_SCRIPT.source
    assert "if entries>=limit then return false end" in ACCEPT_RESPONSE_SCRIPT.source
    assert ACCEPT_RESPONSE_SCRIPT.source.index("for i=1,#members,2 do") < (
        ACCEPT_RESPONSE_SCRIPT.source.index("if entries>=limit then return false end")
    )
    assert "local response_values=redis.call('HMGET',response,'client','request','client_public_key','request_id','node_id','consumer_digest','generation','envelope','accepted_at_epoch','response_digest','replay_expires_at_epoch','status')" in ACCEPT_RESPONSE_SCRIPT.source
    assert "local terminal_exists=redis.call('EXISTS',terminal)" in ACCEPT_RESPONSE_SCRIPT.source
    assert "a<0 or a>now" in ACCEPT_RESPONSE_SCRIPT.source
    assert ACCEPT_RESPONSE_SCRIPT.source.index("elseif response_exists~=0 or response_score") < ACCEPT_RESPONSE_SCRIPT.source.index("return {'existing',tostring(g),tv[9],tv[10]}")


def config(**changes):
    values = dict(
        environment="test",
        cluster="unit",
        schema_major=1,
        reader_revision=2,
        writer_revision=2,
        supported_schema_read_min=1,
        supported_schema_read_max=3,
        supported_writer_min=1,
        supported_writer_max=3,
        direct=DirectPrimary("127.0.0.1", 6379),
    )
    values.update(changes)
    return ValkeyConfig(**values)


def manifest(**changes):
    values = dict(
        schema_major=1,
        active_schema_revision=2,
        active_writer_revision=2,
        reader_min=1,
        reader_max=3,
        writer_min=1,
        writer_max=3,
        script_digests=SCRIPT_DIGESTS,
        migration_epoch=0,
    )
    values.update(changes)
    return SchemaManifest(**values)


def registration_store_with_foundation(foundation):
    store = object.__new__(ValkeyRegistrationStore)
    store._foundation = foundation
    store._config = RelayStateStoreConfig(namespace="testing.unit")
    store._acknowledgement_key = b"shared-test-acknowledgement-key-32"
    return store


def test_select_script_contains_no_scan_commands():
    assert (
        re.search(
            r"redis\.call\(['\"](?:HSCAN|SCAN|KEYS)['\"]",
            valkey_relay_state.SELECT_AND_RESERVE_SOURCE,
        )
        is None
    )


@pytest.mark.parametrize(
    "result",
    [None, [], [1], [b"unknown"], [b"ok", b"extra"], [b"not_found", b"extra"]],
)
def test_registration_transition_rejects_every_malformed_result(result):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = result
    store = registration_store_with_foundation(foundation)

    with pytest.raises(ValkeySchemaIncompatibleError, match="state schema"):
        store._transition("unregister", "node-a", "a" * 64, ())


def test_registration_reads_use_server_time_and_never_mutating_transition():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.read_manifest.return_value = manifest()
    foundation.server_time.return_value = (100, 0)
    foundation._client = Mock()
    foundation._client.zscore = Mock()
    foundation._client.zrangebyscore = Mock()
    foundation._client.hmget = Mock()
    foundation._call.side_effect = [99, []]
    store = registration_store_with_foundation(foundation)

    assert store.get("node-a") is None
    assert store.list() == ()
    foundation.execute.assert_not_called()
    foundation.check_write_compatible.assert_not_called()


def _registration_values():
    return (
        b"node-a",
        b"a" * 64,
        b"100",
        b'["model-a"]',
        b"8k-fast",
        b"8192",
        b"1024",
        b"2048",
        b"1",
        b"cpu",
        b"v1",
        b"200",
    )


def test_registration_read_uses_exact_bounded_fields():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    foundation._call.side_effect = [200.0, list(_registration_values())]
    store = registration_store_with_foundation(foundation)

    assert store._read("node-a", 100).node_id == "node-a"
    hmget_call = foundation._call.call_args_list[1]
    assert hmget_call.args[0] is foundation._client.hmget
    assert hmget_call.args[2] == valkey_relay_state._REGISTRATION_FIELDS


@pytest.mark.parametrize(
    ("reply", "expected"),
    [([None] * 12, None), ([None, *(_registration_values()[1:])], "error")],
)
def test_registration_read_distinguishes_absent_and_partial_records(reply, expected):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    foundation._call.side_effect = [200.0, reply]
    store = registration_store_with_foundation(foundation)

    if expected is None:
        assert store._read("node-a", 100) is None
    else:
        with pytest.raises(
            ValkeySchemaIncompatibleError, match="^state schema incompatible$"
        ):
            store._read("node-a", 100)


def test_registration_read_rejects_over_byte_budget_without_value_leakage():
    marker = b"private-marker"
    reply = list(_registration_values())
    reply[3] = marker + b"x" * valkey_relay_state._MAX_RESULT_BYTES
    with pytest.raises(ValkeySchemaIncompatibleError) as caught:
        ValkeyRegistrationStore._fixed_record(reply)
    assert str(caught.value) == "state schema incompatible"
    assert marker.decode() not in repr(caught.value)


@pytest.mark.parametrize("member", [b"A" * 64, b"g" * 64, b"a" * 63, "a" * 64])
def test_registration_list_translates_malformed_index_members(member):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.read_manifest.return_value = manifest()
    foundation.server_time.return_value = (100, 0)
    foundation._client = Mock()
    foundation._call.return_value = [member]
    store = registration_store_with_foundation(foundation)

    with pytest.raises(
        ValkeySchemaIncompatibleError, match="^state schema incompatible$"
    ) as caught:
        store.list()
    assert str(member) not in repr(caught.value)


def test_registration_list_tolerates_concurrent_disappearance():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.read_manifest.return_value = manifest()
    foundation.server_time.return_value = (100, 0)
    foundation._client = Mock()
    foundation._call.side_effect = [[b"a" * 64], [None] * 12]
    store = registration_store_with_foundation(foundation)

    assert store.list() == ()
    assert foundation._call.call_count == 2
    foundation.execute.assert_not_called()
    foundation.check_write_compatible.assert_not_called()


def test_exact_key_prefix_and_hash_tag():
    cfg = config(environment="staging", cluster="relay-a", schema_major=4)
    assert cfg.key_prefix == "tokenplace:{staging:relay-a}:relay:v4:"
    assert cfg.key("schema") == "tokenplace:{staging:relay-a}:relay:v4:schema"
    with pytest.raises(ValkeyConfigurationError):
        cfg.key("bad:{tag}")


def test_direct_and_sentinel_discovery_validation():
    assert config().direct == DirectPrimary("127.0.0.1", 6379)
    sentinel = SentinelPrimary(
        (("sentinel.internal", 26379),), "relay-primary", "user", "secret"
    )
    cfg = config(direct=None, sentinel=sentinel)
    assert cfg.sentinel is sentinel
    assert "internal" not in repr(sentinel) and "secret" not in repr(sentinel)
    with pytest.raises(ValkeyConfigurationError):
        SentinelPrimary((), "relay-primary")
    with pytest.raises(ValkeyConfigurationError):
        SentinelPrimary((("host", 0),), "relay-primary")


@pytest.mark.parametrize(
    "sentinels",
    [
        [["host", 26379]],
        [("host", 26379)],
        (["host", 26379],),
    ],
)
def test_mutable_sentinel_endpoint_collections_are_rejected(sentinels):
    with pytest.raises(
        ValkeyConfigurationError, match="^invalid Sentinel discovery$"
    ) as caught:
        SentinelPrimary(sentinels, "relay-primary")
    assert "host" not in str(caught.value)


def test_valid_sentinel_configuration_is_deeply_immutable():
    sentinel = SentinelPrimary((("host", 26379),), "relay-primary")
    cfg = config(direct=None, sentinel=sentinel)

    with pytest.raises(TypeError):
        sentinel.sentinels[0] = ("other", 26380)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.sentinel = SentinelPrimary((("other", 26380),), "relay-primary")


class MaliciousDiscovery:
    def __repr__(self):
        return "secret-endpoint secret-password"


@pytest.mark.parametrize("field", ["direct", "sentinel"])
def test_invalid_runtime_discovery_objects_fail_closed_and_redacted(field):
    changes = {field: MaliciousDiscovery()}
    if field == "sentinel":
        changes["direct"] = None
    with pytest.raises(
        ValkeyConfigurationError, match="^invalid discovery configuration$"
    ) as caught:
        config(**changes)
    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert "secret" not in rendered


@pytest.mark.parametrize(
    ("config_value", "manifest_value", "error_type", "message"),
    [
        (
            MaliciousDiscovery(),
            None,
            ValkeyConfigurationError,
            "invalid Valkey configuration",
        ),
        (
            None,
            MaliciousDiscovery(),
            ValkeySchemaIncompatibleError,
            "state schema incompatible",
        ),
    ],
)
def test_invalid_foundation_arguments_fail_before_client_construction(
    config_value, manifest_value, error_type, message
):
    config_value = config() if config_value is None else config_value
    manifest_value = manifest() if manifest_value is None else manifest_value
    with (
        patch.object(ValkeyFoundation, "_create_client") as create,
        patch("valkey_relay_state.redis.ConnectionPool") as pool,
        patch("valkey_relay_state.redis.Redis") as redis_class,
        patch("valkey_relay_state.Sentinel") as sentinel_class,
    ):
        with pytest.raises(error_type, match=f"^{message}$") as caught:
            ValkeyFoundation(config_value, manifest_value)
    create.assert_not_called()
    pool.assert_not_called()
    redis_class.assert_not_called()
    sentinel_class.assert_not_called()
    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert "secret" not in rendered


@pytest.mark.parametrize(
    "changes",
    [
        {"direct": None},
        {"sentinel": SentinelPrimary((("host", 26379),), "primary")},
        {"connect_timeout_seconds": math.inf},
        {"socket_timeout_seconds": 31},
        {"command_timeout_seconds": 0},
        {"retry_timeout_seconds": math.nan},
        {"retry_attempts": 6},
    ],
)
def test_discovery_and_timeout_configuration_fails_closed(changes):
    with pytest.raises(ValkeyConfigurationError):
        config(**changes)


def test_tls_auth_and_all_representations_are_redacted():
    cfg = config(
        direct=DirectPrimary("secret-endpoint", 6380),
        tls=True,
        tls_ca_cert="secret-ca",
        tls_client_cert="secret-cert",
        tls_client_key="secret-key",  # pragma: allowlist secret
        username="secret-user",
        password="secret-password",  # pragma: allowlist secret
    )
    rendered = repr(cfg) + repr(cfg.direct)
    for secret in (
        "secret-endpoint",
        "secret-ca",
        "secret-cert",
        "secret-key",
        "secret-user",
        "secret-password",
    ):
        assert secret not in rendered


def test_sentinel_tls_applies_to_discovery_and_primary_connections():
    sentinel = SentinelPrimary((("sentinel.internal", 26379),), "relay-primary")
    cfg = config(
        direct=None,
        sentinel=sentinel,
        tls=True,
        tls_ca_cert="ca.pem",
        tls_client_cert="client.pem",
        tls_client_key="client-key.pem",  # pragma: allowlist secret
    )
    expected = manifest()
    master = Mock()
    with patch("valkey_relay_state.Sentinel") as sentinel_class:
        sentinel_class.return_value.master_for.return_value = master
        foundation = ValkeyFoundation(cfg, expected)

    call = sentinel_class.call_args
    assert call.kwargs["ssl"] is True
    assert call.kwargs["sentinel_kwargs"]["ssl"] is True
    assert call.kwargs["sentinel_kwargs"]["ssl_ca_certs"] == "ca.pem"
    assert call.kwargs["max_connections"] == 32
    assert call.kwargs["sentinel_kwargs"]["max_connections"] == 32
    assert foundation._client is master


@pytest.mark.parametrize(
    "changed",
    [
        {"schema_major": 2},
        {"active_schema_revision": 4},
        {"reader_min": 3},
        {"active_writer_revision": 4},
        {"writer_min": 3},
        {"script_digests": {SERVER_TIME_SCRIPT.name: "0" * 64}},
        {"migration_epoch": 1},
    ],
)
def test_schema_reader_writer_and_digest_gates(changed):
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config()
    foundation.expected_manifest = manifest()
    candidate = manifest(**changed)
    with pytest.raises(
        ValkeySchemaIncompatibleError, match="state schema incompatible"
    ):
        foundation.check_write_compatible(candidate)


def test_read_gate_can_pass_when_writer_gate_rejects():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(writer_revision=3)
    foundation.expected_manifest = manifest()
    candidate = manifest(writer_max=2)
    foundation.check_read_compatible(candidate)
    with pytest.raises(ValkeySchemaIncompatibleError):
        foundation.check_write_compatible(candidate)


def test_manifest_is_immutable_and_round_trips_canonically():
    value = manifest()
    assert SchemaManifest.decode(value.encode()) == value
    with pytest.raises(TypeError):
        value.script_digests["new"] = "0" * 64
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.migration_epoch = 1


def test_arbitrary_and_digest_mismatched_scripts_are_rejected():
    with pytest.raises(ValkeyScriptError):
        ReviewedScript("bad", "return 1", "0" * 64, False)
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    with pytest.raises(ValkeyScriptError, match="unknown reviewed script"):
        foundation.execute("caller_lua")


def test_errors_do_not_disclose_backend_details():
    error = ValkeyUnavailableError("state backend unavailable")
    assert "host" not in repr(error)
    assert "password" not in repr(error)


def test_lazy_sentinel_discovery_error_is_bounded_and_redacted():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=0)

    def unavailable():
        raise MasterNotFoundError("No master at secret-sentinel:26379")

    with pytest.raises(
        ValkeyUnavailableError, match="state backend unavailable"
    ) as caught:
        foundation._call(unavailable)
    assert "secret-sentinel" not in str(caught.value)


def test_invalid_expected_manifest_is_rejected_before_connection_or_creation():
    with patch.object(ValkeyFoundation, "_create_client") as create:
        with pytest.raises(ValkeySchemaIncompatibleError):
            ValkeyFoundation(config(), manifest(schema_major=2))
    create.assert_not_called()


@pytest.mark.parametrize(
    "script_digests",
    [
        {},
        {**SCRIPT_DIGESTS, "extra_v1": "a" * 64},
        {SERVER_TIME_SCRIPT.name: "a" * 64},
    ],
)
def test_expected_script_digests_must_exactly_match_registry_before_connection(
    script_digests,
):
    expected = manifest(script_digests=script_digests)
    with patch.object(ValkeyFoundation, "_create_client") as create:
        with pytest.raises(ValkeySchemaIncompatibleError):
            ValkeyFoundation(config(), expected)
    create.assert_not_called()

    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config()
    foundation.expected_manifest = expected
    candidate = manifest(script_digests=script_digests)
    with pytest.raises(ValkeySchemaIncompatibleError):
        foundation.check_read_compatible(candidate)
    with pytest.raises(ValkeySchemaIncompatibleError):
        foundation.check_write_compatible(candidate)


def test_retry_budget_is_total_and_deterministic():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=5, retry_timeout_seconds=0.1)
    operation = Mock(side_effect=redis.ConnectionError("private endpoint"))
    clock = Mock(side_effect=[0.0, 0.04, 0.11])
    with (
        patch("valkey_relay_state.time.monotonic", clock),
        patch("valkey_relay_state.time.sleep") as sleep,
    ):
        with pytest.raises(ValkeyUnavailableError) as caught:
            foundation._call(operation)
    assert operation.call_count == 2
    sleep.assert_called_once_with(0.05)
    assert caught.value.__cause__ is None


def test_mutating_script_transport_failure_is_not_retried():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=5)
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = manifest().encode()
    foundation._client.evalsha.side_effect = redis.ConnectionError("private endpoint")

    with pytest.raises(
        ValkeyUnavailableError, match="^state backend unavailable$"
    ) as caught:
        foundation.execute(REGISTRATION_TRANSITION_SCRIPT.name)

    foundation._client.evalsha.assert_called_once()
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("backend_error", "expected_error", "message"),
    [
        (
            redis.ResponseError("READONLY private primary"),
            ValkeyReadOnlyError,
            "state backend is not writable",
        ),
        (
            redis.ResponseError("private command failure"),
            ValkeyUnavailableError,
            "state backend command failed",
        ),
    ],
)
def test_mutating_script_response_failures_are_typed_redacted_and_not_retried(
    backend_error, expected_error, message
):
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=5)
    operation = Mock(side_effect=backend_error)

    with pytest.raises(expected_error, match=f"^{message}$") as caught:
        foundation._call_mutating_script(operation)

    operation.assert_called_once()
    assert "private" not in str(caught.value)


def test_read_only_script_transport_failure_retains_bounded_retry():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=2)
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = manifest().encode()
    foundation._client.evalsha.side_effect = [
        redis.ConnectionError("private endpoint"),
        [b"1", b"2"],
    ]

    with patch("valkey_relay_state.time.sleep"):
        assert foundation.server_time() == (1, 2)

    assert foundation._client.evalsha.call_count == 2


def test_mutating_noscript_recovery_dispatches_loaded_script_only_once():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=5)
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = manifest().encode()
    foundation._client.evalsha.side_effect = [
        redis.exceptions.NoScriptError("missing reviewed script"),
        redis.ConnectionError("lost reply from private endpoint"),
    ]
    foundation._client.script_load.return_value = (
        REGISTRATION_TRANSITION_SCRIPT.eval_sha1
    )

    with pytest.raises(ValkeyUnavailableError, match="^state backend unavailable$"):
        foundation.execute(REGISTRATION_TRANSITION_SCRIPT.name)

    assert foundation._client.evalsha.call_count == 2
    foundation._client.script_load.assert_called_once_with(
        REGISTRATION_TRANSITION_SCRIPT.source
    )
    assert foundation._client.get.call_count == 2


def test_incompatible_execution_reads_only_manifest():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config()
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = manifest(schema_major=2).encode()
    with pytest.raises(ValkeySchemaIncompatibleError):
        foundation.server_time()
    foundation._client.get.assert_called_once()
    foundation._client.evalsha.assert_not_called()
    foundation._client.script_load.assert_not_called()


def test_second_noscript_is_a_bounded_typed_error_without_another_retry():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=5)
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.side_effect = [manifest().encode(), manifest().encode()]
    datastore_detail = "NOSCRIPT reply from secret-endpoint:6379"
    foundation._client.evalsha.side_effect = [
        redis.exceptions.NoScriptError(datastore_detail),
        redis.exceptions.NoScriptError(datastore_detail),
    ]
    foundation._client.script_load.return_value = SERVER_TIME_SCRIPT.eval_sha1

    with pytest.raises(
        ValkeyScriptError, match="^reviewed script recovery failed$"
    ) as caught:
        foundation.server_time()

    assert foundation._client.evalsha.call_count == 2
    foundation._client.script_load.assert_called_once_with(SERVER_TIME_SCRIPT.source)
    assert caught.value.__cause__ is None
    rendered = (
        repr(caught.value)
        + str(caught.value)
        + "".join(traceback.format_exception(caught.value))
    )
    assert datastore_detail not in rendered
    assert "secret-endpoint" not in rendered


@pytest.mark.parametrize("result", ["reply", {"raw": b"reply"}, [b"x"] * 1025])
def test_script_result_decoder_rejects_unbounded_or_unsupported_values(result):
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config()
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = manifest().encode()
    foundation._client.evalsha.return_value = result
    with pytest.raises(ValkeyScriptError, match="invalid reviewed script result"):
        foundation.server_time()


def test_unrelated_reviewed_script_keeps_generic_result_byte_budget():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config()
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = manifest().encode()
    foundation._client.evalsha.return_value = [
        b"x" * (valkey_relay_state._MAX_RESULT_BYTES + 1)
    ]

    with pytest.raises(ValkeyScriptError, match="invalid reviewed script result"):
        foundation.server_time()


def test_false_ping_is_unavailable():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    foundation._client.ping.return_value = False
    with pytest.raises(ValkeyUnavailableError):
        foundation.readiness()
    foundation._client.role.assert_not_called()


def test_failure_rendering_traceback_and_logs_are_redacted(caplog):
    secrets = ("host.internal", "user", "password", "/secret/ca", "raw:key", "reply")
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=0)

    def fail():
        raise redis.ConnectionError(" ".join(secrets))

    with caplog.at_level(logging.DEBUG):
        try:
            foundation._call(fail)
        except ValkeyUnavailableError as error:
            rendered = (
                repr(error)
                + str(error)
                + "".join(traceback.format_exception(error))
                + caplog.text
            )
    assert all(secret not in rendered for secret in secrets)


@pytest.mark.parametrize(
    ("family", "components", "suffix"),
    [
        *(
            (family, (), family)
            for family in (
                "schema",
                "nodes:lease",
                "cursor",
                "reservations:expiry",
                "requests:deadline",
                "claims:expiry",
                "responses:expiry",
                "control:expiry",
                "node_tombstones:expiry",
                "terminals:expiry",
            )
        ),
        *(
            (family, ("a" * 64,), f"{family}:{'a' * 64}")
            for family in ("node", "reservation", "queue", "node_tombstone")
        ),
        *(
            (family, ("a" * 64, "b" * 64), f"{family}:{'a' * 64}:{'b' * 64}")
            for family in ("request", "claim", "response", "progress", "terminal")
        ),
        (
            "control",
            ("a" * 64, "b" * 64, "c" * 64),
            f"control:{'a' * 64}:{'b' * 64}:{'c' * 64}",
        ),
        (
            "ratelimit",
            ("public-api", "d" * 64, 123),
            f"ratelimit:public-api:{'d' * 64}:123",
        ),
    ],
)
def test_complete_adr_key_families_have_exact_layout(family, components, suffix):
    cfg = config(environment="staging", cluster="relay-a", schema_major=4)
    assert cfg.key(family, *components) == (
        "tokenplace:{staging:relay-a}:relay:v4:" + suffix
    )


@pytest.mark.parametrize(
    ("family", "components"),
    [
        ("unknown", ()),
        (None, ()),
        ("lease", ("a" * 64,)),
        ("worker", ("a" * 64,)),
        ("schema", ("a" * 64,)),
        ("request", ("a" * 64,)),
        ("request", ("a" * 64, "b" * 64, "c" * 64)),
        ("request", ("raw-client", "b" * 64)),
        ("request", ("A" * 64, "b" * 64)),
        ("request", ("{" + "a" * 63, "b" * 64)),
        ("request", (123, "b" * 64)),
        ("ratelimit", ("public:api", "d" * 64, 1)),
        ("ratelimit", ("public api", "d" * 64, 1)),
        ("ratelimit", ("{public}", "d" * 64, 1)),
        ("ratelimit", ("public", "raw-identity", 1)),
        ("ratelimit", ("public", "d" * 64, True)),
        ("ratelimit", ("public", "d" * 64, -1)),
        ("ratelimit", ("public", "d" * 64, 2**63)),
        ("ratelimit", ("public", "d" * 64, "01")),
    ],
)
def test_key_builder_rejects_invalid_family_components_and_extra_hash_tags(
    family, components
):
    with pytest.raises(ValkeyConfigurationError, match="invalid key suffix"):
        config().key(family, *components)


@pytest.mark.parametrize(
    "changes", [{"tls": 1}, {"username": ""}, {"password": 42}, {"tls_ca_cert": " "}]
)
def test_authentication_and_tls_types_are_strict(changes):
    with pytest.raises(ValkeyConfigurationError):
        config(**changes)
    with pytest.raises(ValkeyConfigurationError):
        SentinelPrimary((("host", 26379),), "relay-primary", "")


def test_manifest_construction_bounds_scripts_and_encoded_bytes():
    with pytest.raises(ValkeySchemaIncompatibleError):
        manifest(script_digests={f"s{i}": "a" * 64 for i in range(65)})
    with patch("valkey_relay_state._MAX_RESULT_BYTES", 10):
        with pytest.raises(ValkeySchemaIncompatibleError):
            manifest().encode()


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: DirectPrimary("bad host"),
        lambda: DirectPrimary("host", True),
        lambda: SentinelPrimary((("host", 26379),), "bad service"),
        lambda: config(environment="bad namespace"),
        lambda: config(schema_major=True),
        lambda: config(supported_schema_read_min=3, supported_schema_read_max=2),
        lambda: config(tls=True, tls_client_cert="client.pem"),
        lambda: manifest(schema_major=True),
        lambda: manifest(reader_min=3, reader_max=2),
        lambda: manifest(migration_epoch=True),
        lambda: manifest(script_digests={"bad name": "a" * 64}),
    ],
)
def test_invalid_configuration_and_manifest_branches_are_covered(constructor):
    with pytest.raises((ValkeyConfigurationError, ValkeySchemaIncompatibleError)):
        constructor()


@pytest.mark.parametrize("raw", ["not-bytes", b"[]", b"not-json"])
def test_manifest_decoder_rejects_invalid_encodings(raw):
    with pytest.raises(ValkeySchemaIncompatibleError):
        SchemaManifest.decode(raw)


@pytest.mark.parametrize(
    "result",
    [None, True, 123, b"x" * 65_537, [[[[[[[[[b"too-deep"]]]]]]]]]],
)
def test_script_result_decoder_covers_scalar_and_bound_branches(result):
    if result in (None, True, 123):
        valkey_relay_state._validate_script_result(result)
    else:
        with pytest.raises(ValkeyScriptError):
            valkey_relay_state._validate_script_result(result)


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (redis.ResponseError("READONLY private reply"), ValkeyReadOnlyError),
        (redis.ResponseError("private reply"), ValkeyUnavailableError),
    ],
)
def test_response_errors_are_classified_without_details(error, expected_error):
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=0)
    with pytest.raises(expected_error) as caught:
        foundation._call(Mock(side_effect=error))
    assert "private reply" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_missing_manifest_results_are_typed():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=0)
    foundation.expected_manifest = manifest()
    foundation._client = Mock()
    foundation._client.get.return_value = None

    with pytest.raises(ValkeyUnavailableError):
        foundation.initialize_manifest()
    with pytest.raises(ValkeySchemaIncompatibleError):
        foundation.read_manifest()


@pytest.mark.parametrize("result", [[b"invalid"], [-1, 0], [0, 1_000_000]])
def test_server_time_rejects_malformed_and_out_of_range_results(result):
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    with patch.object(foundation, "execute", return_value=result):
        with pytest.raises(ValkeyScriptError, match="invalid reviewed script result"):
            foundation.server_time()


def test_registration_deadline_failure_is_typed_before_record_decoding():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = [b"deadline"]
    store = registration_store_with_foundation(foundation)
    store._config = RelayStateStoreConfig(
        namespace="testing.unit",
        lease_ttl_seconds=float.fromhex("0x1.fffffffffffffp+1023"),
    )

    with pytest.raises(
        RelayStateStoreError, match="^registration deadline must be finite$"
    ):
        store._transition(
            "register", "node-a", "a" * 64, (b"node-a", *(b"" for _ in range(8)))
        )

    assert foundation.execute.call_args.args[2][4] == b"1.7976931348623157e+308"


@pytest.mark.parametrize(
    "result",
    [None, [], [b"\xff"], [1]],
)
def test_scheduler_result_status_rejects_malformed_values(result):
    with pytest.raises(ValkeySchemaIncompatibleError, match="state schema"):
        ValkeyRegistrationStore._ascii_status(result)


@pytest.mark.parametrize("value", ["text", b"x" * 65_537, b"\xff"])
def test_scheduler_text_decoder_rejects_unbounded_or_invalid_values(value):
    with pytest.raises(ValkeySchemaIncompatibleError, match="state schema"):
        ValkeyRegistrationStore._decode_text(value)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("", "request"), "request identity is invalid"),
        (("client", "x" * 8_193), "request identity is invalid"),
    ],
)
def test_scheduler_identity_validation_is_typed(args, message):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    store = registration_store_with_foundation(foundation)
    with pytest.raises(RelayStateStoreError, match=message):
        store._identity(*args)


@pytest.mark.parametrize(
    ("model", "tier", "deadline", "message"),
    [
        ("", "8k-fast", 10, "requested model is invalid"),
        ("model", "unknown", 10, "requested context tier is invalid"),
        ("model", "8k-fast", math.inf, "request deadline must be finite"),
    ],
)
def test_scheduler_request_validation_is_typed(model, tier, deadline, message):
    store = registration_store_with_foundation(Mock(spec=ValkeyFoundation))
    with pytest.raises(RelayStateStoreError, match=message):
        store._model_tier_deadline(model, tier, deadline)


@pytest.mark.parametrize("token", [None, "", "x" * 1_025])
def test_scheduler_cancellation_proof_validation_is_typed(token):
    store = registration_store_with_foundation(Mock(spec=ValkeyFoundation))
    with pytest.raises(RelayStateStoreError, match="cancellation proof is invalid"):
        store._cancellation_digest(token)


def test_consumer_identity_uses_its_specific_byte_bound():
    foundation = Mock(spec=ValkeyFoundation)
    store = registration_store_with_foundation(foundation)
    store._config = dataclasses.replace(
        store.config, max_identity_bytes=8, max_consumer_identity_bytes=4
    )

    with pytest.raises(RelayStateStoreError, match="consumer identity is invalid"):
        store._consumer_digest("12345")


def test_claimed_request_validates_identity_before_reading_state():
    foundation = Mock(spec=ValkeyFoundation)
    store = registration_store_with_foundation(foundation)
    store._config = dataclasses.replace(store.config, max_identity_bytes=4)

    with pytest.raises(RelayStateStoreError, match="request identity is invalid"):
        store.claimed_request("node-a", "12345")
    foundation.read_manifest.assert_not_called()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ([b"owner"], RelayStateCredentialMismatch),
        ([b"capacity"], RelayStateCapacityExceeded),
        ([b"schema"], ValkeySchemaIncompatibleError),
        ([b"claimed"], ValkeySchemaIncompatibleError),
        (
            [b"claimed", b"0", b"9", b"10", b"client", b"request", b"{}"],
            ValkeySchemaIncompatibleError,
        ),
        (
            [
                b"claimed",
                b"1",
                b"11",
                b"10",
                b"client",
                b"request",
                b"{}",
            ],
            ValkeySchemaIncompatibleError,
        ),
        (
            [
                b"claimed",
                b"1",
                b"9",
                b"10",
                b"",
                b"request",
                b"{}",
            ],
            ValkeySchemaIncompatibleError,
        ),
    ],
)
def test_claim_translates_malformed_and_fixed_script_results(result, expected):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = result
    store = registration_store_with_foundation(foundation)

    with pytest.raises(expected):
        store.claim_queued_request("node-a", "a" * 64, "consumer")


def test_claim_empty_and_valid_results_are_decoded():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    store = registration_store_with_foundation(foundation)
    foundation.execute.return_value = [b"empty"]
    assert store.claim_queued_request("node-a", "a" * 64, "consumer").state == "empty"

    envelope = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "ciphertext": "ciphertext",
        "cipherkey": "cipherkey",
        "iv": "iv",
    }
    foundation.execute.return_value = [
        b"reclaimed",
        b"2",
        b"9",
        b"10",
        b"client",
        b"request",
        json.dumps(envelope).encode(),
    ]
    result = store.claim_queued_request("node-a", "a" * 64, "consumer")
    assert (result.state, result.generation, result.client_public_key) == (
        "reclaimed",
        2,
        "client",
    )


@pytest.mark.parametrize("raw", ["not-bytes", b"[]", b"{}", b"not-json"])
def test_claim_envelope_decoder_rejects_malformed_values(raw):
    with pytest.raises(ValkeySchemaIncompatibleError, match="state schema"):
        ValkeyRegistrationStore._decode_request_envelope(raw)


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        ([b"missing_or_expired"], "missing_or_expired"),
        ([b"owner_mismatch"], "owner_mismatch"),
        ([b"stale_generation", b"2"], "stale_generation"),
        ([b"continued", b"2", b"9.5"], "continued"),
    ],
)
def test_renew_claim_decodes_each_fixed_result(result, expected_status):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = result
    store = registration_store_with_foundation(foundation)

    renewal = store.renew_claim("node-a", "a" * 64, "consumer", "client", "request", 2)
    assert renewal.state == expected_status


@pytest.mark.parametrize(
    "result",
    [
        [b"schema"],
        [b"stale_generation", b"0"],
        [b"continued", b"0", b"9"],
        [b"continued", b"1", b"nan"],
        [b"unknown"],
    ],
)
def test_renew_claim_rejects_malformed_script_results(result):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = result
    store = registration_store_with_foundation(foundation)

    with pytest.raises(ValkeySchemaIncompatibleError, match="state schema"):
        store.renew_claim("node-a", "a" * 64, "consumer", "client", "request", 1)


def test_claim_input_and_acknowledgement_validation_precede_dispatch():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    store = registration_store_with_foundation(foundation)

    with pytest.raises(RelayStateStoreError, match="consumer identity"):
        store.claim_queued_request("node-a", "a" * 64, "")
    with pytest.raises(RelayStateStoreError, match="request id is required"):
        store.claimed_request("node-a", "")
    with pytest.raises(RelayStateStoreError, match="claim generation"):
        store.renew_claim("node-a", "a" * 64, "consumer", "client", "request", 0)
    with pytest.raises(RelayStateStoreError, match="control tombstones"):
        store.renew_claim_or_read_control(
            "node-a",
            "a" * 64,
            "consumer",
            "client",
            "request",
            1,
            acknowledge=True,
        )
    foundation.execute.assert_not_called()


def test_renew_or_read_control_delegates_when_not_acknowledging():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = [b"missing_or_expired"]
    store = registration_store_with_foundation(foundation)

    result = store.renew_claim_or_read_control(
        "node-a", "a" * 64, "consumer", "client", "request", 1
    )
    assert result.state == "missing_or_expired"


def test_live_claims_tolerates_a_removed_registration_after_read_gate():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation._client = Mock()
    foundation.server_time.return_value = (1, 0)
    foundation._call.return_value = [None, None, None]
    store = registration_store_with_foundation(foundation)

    assert store.active_claims("node-a") == ()
    foundation.check_read_compatible.assert_called_once_with(
        foundation.read_manifest.return_value
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ([b"credential_mismatch"], RelayStateCredentialMismatch),
        ([b"schema"], ValkeySchemaIncompatibleError),
        ([b"ok", b"extra"], ValkeySchemaIncompatibleError),
    ],
)
def test_scheduler_state_translates_fixed_script_results(result, expected):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = result
    store = registration_store_with_foundation(foundation)
    with pytest.raises(expected):
        store.set_scheduler_state("node-a", "a" * 64, SchedulerNodeState())


def test_scheduler_state_rejects_wrong_state_type_before_dispatch():
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    store = registration_store_with_foundation(foundation)
    with pytest.raises(RelayStateStoreError, match="scheduler state"):
        store.set_scheduler_state("node-a", "a" * 64, object())
    foundation.execute.assert_not_called()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ([b"invalid"], RelayStateInvalidReservation),
        ([b"conflict"], RelayStateConflict),
        ([b"schema"], ValkeySchemaIncompatibleError),
        ([b"created"], ValkeySchemaIncompatibleError),
    ],
)
def test_enqueue_translates_fixed_script_results(result, expected):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.execute.return_value = result
    store = registration_store_with_foundation(foundation)
    envelope = EncryptedRequestEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "cipherkey", "iv"
    )
    with pytest.raises(expected):
        store.enqueue_encrypted_request(
            "client",
            "request",
            "a" * 64,
            "node-a",
            "model",
            "8k-fast",
            10,
            envelope,
            "cancel",
        )


@pytest.mark.parametrize(
    ("reply", "error"),
    (
        ([b"owner"], RelayStateCredentialMismatch),
        ([b"missing"], RelayStateConflict),
        ([b"stale", b"2"], RelayStateConflict),
        ([b"conflict"], RelayStateConflict),
        ([b"malformed"], RelayStateConflict),
        ([b"capacity"], RelayStateCapacityExceeded),
        ([b"schema"], ValkeySchemaIncompatibleError),
        ([b"malformed", b"extra"], ValkeySchemaIncompatibleError),
        ([b"stale"], ValkeySchemaIncompatibleError),
        ([b"stale", b"01"], ValkeySchemaIncompatibleError),
        ([b"accepted", b"1", b"nan", b"2"], ValkeySchemaIncompatibleError),
        ([b"existing", b"1", b"-1", b"2"], ValkeySchemaIncompatibleError),
        ([b"existing", b"1", b"2", b"1"], ValkeySchemaIncompatibleError),
    ),
)
def test_accept_response_decodes_only_fixed_bounded_results(reply, error):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.server_time.return_value = (1, 0)
    foundation.execute.return_value = reply
    store = registration_store_with_foundation(foundation)
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )

    with pytest.raises(error):
        store.accept_encrypted_response(
            "node", "a" * 64, "consumer", "client", "request", 1, envelope
        )


@pytest.mark.parametrize(("status", "new_outcome"), ((b"accepted", True), (b"existing", False)))
def test_accept_response_decodes_fixed_success_results(status, new_outcome):
    foundation = Mock(spec=ValkeyFoundation)
    foundation.config = config()
    foundation.server_time.return_value = (1, 0)
    foundation.execute.return_value = [status, b"1", b"1.0", b"2.0"]
    store = registration_store_with_foundation(foundation)
    envelope = EncryptedResponseEnvelope(
        "tokenplace_api_v1_relay_e2ee", 1, "ciphertext", "key", "iv"
    )

    result = store.accept_encrypted_response(
        "node", "a" * 64, "consumer", "client", "request", 1, envelope
    )

    assert result.new_outcome is new_outcome
    assert (result.generation, result.accepted_at_epoch, result.replay_expires_at_epoch) == (
        1,
        1.0,
        2.0,
    )
