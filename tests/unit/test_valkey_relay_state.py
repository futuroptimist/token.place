import dataclasses
import logging
import math
import traceback
from unittest.mock import Mock, patch

import pytest
import redis
from redis.sentinel import MasterNotFoundError

from valkey_relay_state import (
    DirectPrimary,
    ReviewedScript,
    SchemaManifest,
    SentinelPrimary,
    ValkeyConfig,
    ValkeyConfigurationError,
    ValkeyFoundation,
    ValkeyReadOnlyError,
    ValkeySchemaIncompatibleError,
    ValkeyScriptError,
    ValkeyUnavailableError,
    SERVER_TIME_SCRIPT,
)


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
        script_digests={SERVER_TIME_SCRIPT.name: SERVER_TIME_SCRIPT.sha256},
        migration_epoch=0,
    )
    values.update(changes)
    return SchemaManifest(**values)


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


def test_retry_budget_is_total_and_deterministic():
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    foundation.config = config(retry_attempts=5, retry_timeout_seconds=0.1)
    operation = Mock(side_effect=redis.ConnectionError("private endpoint"))
    clock = Mock(side_effect=[0.0, 0.04, 0.11])
    with patch("valkey_relay_state.time.monotonic", clock), patch(
        "valkey_relay_state.time.sleep"
    ) as sleep:
        with pytest.raises(ValkeyUnavailableError) as caught:
            foundation._call(operation)
    assert operation.call_count == 2
    sleep.assert_called_once_with(0.05)
    assert caught.value.__cause__ is None


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
