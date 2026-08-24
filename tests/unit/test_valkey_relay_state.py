import dataclasses
import math

import pytest

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
        ReviewedScript("bad", "return 1", "0" * 64)
    foundation = ValkeyFoundation.__new__(ValkeyFoundation)
    with pytest.raises(ValkeyScriptError, match="unknown reviewed script"):
        foundation.execute("caller_lua")


def test_errors_do_not_disclose_backend_details():
    error = ValkeyUnavailableError("state backend unavailable")
    assert "host" not in repr(error)
    assert "password" not in repr(error)
