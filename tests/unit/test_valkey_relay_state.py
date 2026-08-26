import dataclasses
import logging
import math
import traceback
from unittest.mock import Mock, patch

import pytest
import redis
from redis.sentinel import MasterNotFoundError
import valkey_relay_state

from valkey_relay_state import (
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
from relay_state_store import RelayStateStoreConfig, RelayStateStoreError


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
    return store


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
    foundation._client.hgetall = Mock()
    foundation._call.side_effect = [99_999_999, [], []]
    store = registration_store_with_foundation(foundation)

    assert store.get("node-a") is None
    assert store.list() == ()
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
