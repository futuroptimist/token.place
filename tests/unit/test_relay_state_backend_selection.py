import base64
from unittest.mock import Mock, patch

import pytest

import relay
from relay_state_store import InMemoryRelayStateStore
from valkey_relay_state import (
    DirectPrimary,
    ValkeyConfigurationError,
    ValkeySchemaIncompatibleError,
    ValkeyUnavailableError,
)


VALKEY_ENV = {
    relay.API_V1_STATE_BACKEND_ENV: "valkey",
    relay.API_V1_VALKEY_ENVIRONMENT_ENV: "testing",
    relay.API_V1_VALKEY_CLUSTER_ENV: "selection",
    relay.API_V1_VALKEY_SCHEMA_MAJOR_ENV: "1",
    relay.API_V1_VALKEY_SCHEMA_REVISION_ENV: "1",
    relay.API_V1_VALKEY_MIGRATION_EPOCH_ENV: "0",
    relay.API_V1_VALKEY_DISCOVERY_ENV: "direct",
    relay.API_V1_VALKEY_HOST_ENV: "127.0.0.1",
    relay.API_V1_VALKEY_PORT_ENV: "6379",
    relay.API_V1_VALKEY_TLS_ENV: "false",
    relay.API_V1_VALKEY_ACKNOWLEDGEMENT_KEY_ENV: base64.b64encode(b"k" * 32).decode(),
}


@pytest.fixture(autouse=True)
def restore_authoritative_store():
    previous = relay.api_v1_relay_state_store
    relay.api_v1_relay_state_store = None
    yield
    current = relay.api_v1_relay_state_store
    if current is not None and current is not previous:
        close = getattr(current, "close", None)
        if callable(close):
            close()
    relay.api_v1_relay_state_store = previous


def test_memory_is_default_and_explicit_memory_selection(monkeypatch):
    monkeypatch.delenv(relay.API_V1_STATE_BACKEND_ENV, raising=False)
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "memory")
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)


@pytest.mark.parametrize("backend", ["", "MEMORY", "redis", "valkey "])
def test_unknown_backend_is_rejected_without_memory_fallback(monkeypatch, backend):
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, backend)
    with patch.object(relay, "_memory_api_v1_relay_state_store") as memory:
        with pytest.raises(ValueError, match="invalid API-v1 state backend"):
            relay._new_api_v1_relay_state_store()
    memory.assert_not_called()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        (relay.API_V1_VALKEY_SCHEMA_REVISION_ENV, "not-an-integer"),
        (relay.API_V1_VALKEY_PORT_ENV, "0"),
        (relay.API_V1_VALKEY_TLS_ENV, "yes"),
        (relay.API_V1_VALKEY_DISCOVERY_ENV, "automatic"),
    ],
)
def test_invalid_valkey_configuration_is_bounded_and_does_not_fallback(
    monkeypatch, setting, value
):
    for name, configured in VALKEY_ENV.items():
        monkeypatch.setenv(name, configured)
    monkeypatch.setenv(setting, value)
    with patch.object(relay, "_memory_api_v1_relay_state_store") as memory:
        with pytest.raises(
            ValkeyConfigurationError, match="^invalid Valkey runtime configuration$"
        ) as caught:
            relay._new_api_v1_relay_state_store()
    memory.assert_not_called()
    assert value not in repr(caught.value)


@pytest.mark.parametrize("key", [None, "not-base64", base64.b64encode(b"short").decode()])
def test_valkey_requires_a_valid_shared_acknowledgement_key(monkeypatch, key):
    for name, value in VALKEY_ENV.items():
        monkeypatch.setenv(name, value)
    if key is None:
        monkeypatch.delenv(relay.API_V1_VALKEY_ACKNOWLEDGEMENT_KEY_ENV)
    else:
        monkeypatch.setenv(relay.API_V1_VALKEY_ACKNOWLEDGEMENT_KEY_ENV, key)
    with pytest.raises(ValkeyConfigurationError) as caught:
        relay._new_api_v1_relay_state_store()
    assert key is None or key not in repr(caught.value)


def test_explicit_valkey_constructs_reviewed_manifest_and_shared_key(monkeypatch):
    for name, value in VALKEY_ENV.items():
        monkeypatch.setenv(name, value)
    foundation = Mock()
    store = Mock()
    with (
        patch.object(relay, "ValkeyFoundation", return_value=foundation) as foundation_type,
        patch.object(relay, "ValkeyRegistrationStore", return_value=store) as store_type,
    ):
        assert relay._new_api_v1_relay_state_store() is store

    config, manifest = foundation_type.call_args.args
    assert config.direct == DirectPrimary("127.0.0.1", 6379)
    assert config.tls is False
    assert dict(manifest.script_digests) == dict(relay.SCRIPT_DIGESTS)
    foundation.initialize_manifest.assert_called_once_with()
    foundation.readiness.assert_called_once_with()
    assert store_type.call_args.kwargs["acknowledgement_key"] == b"k" * 32


@pytest.mark.parametrize(
    "failure", [ValkeySchemaIncompatibleError("state schema incompatible"),
                ValkeyUnavailableError("state backend unavailable")]
)
def test_valkey_startup_failure_closes_resources_and_never_falls_back(
    monkeypatch, failure
):
    for name, value in VALKEY_ENV.items():
        monkeypatch.setenv(name, value)
    foundation = Mock()
    foundation.initialize_manifest.side_effect = failure
    with (
        patch.object(relay, "ValkeyFoundation", return_value=foundation),
        patch.object(relay, "_memory_api_v1_relay_state_store") as memory,
    ):
        with pytest.raises(type(failure), match=str(failure)):
            relay._new_api_v1_relay_state_store()
    foundation.close.assert_called_once_with()
    memory.assert_not_called()


def test_reset_closes_previous_store_only_after_replacement_is_ready(monkeypatch):
    previous = Mock()
    replacement = Mock()
    relay.api_v1_relay_state_store = previous
    monkeypatch.setattr(relay, "_new_api_v1_relay_state_store", Mock(return_value=replacement))

    relay._reset_api_v1_relay_state_store()

    assert relay.api_v1_relay_state_store is replacement
    previous.close.assert_called_once_with()


def test_failed_reset_preserves_and_does_not_close_previous_store(monkeypatch):
    previous = Mock()
    relay.api_v1_relay_state_store = previous
    monkeypatch.setattr(
        relay,
        "_new_api_v1_relay_state_store",
        Mock(side_effect=ValkeyUnavailableError("state backend unavailable")),
    )

    with pytest.raises(ValkeyUnavailableError):
        relay._reset_api_v1_relay_state_store()

    assert relay.api_v1_relay_state_store is previous
    previous.close.assert_not_called()


def test_shared_key_is_identical_across_independent_valkey_construction(monkeypatch):
    for name, value in VALKEY_ENV.items():
        monkeypatch.setenv(name, value)
    foundations = [Mock(), Mock()]
    stores = [Mock(), Mock()]
    with (
        patch.object(relay, "ValkeyFoundation", side_effect=foundations),
        patch.object(relay, "ValkeyRegistrationStore", side_effect=stores) as store_type,
    ):
        relay._new_api_v1_relay_state_store()
        relay._new_api_v1_relay_state_store()

    first = store_type.call_args_list[0].kwargs["acknowledgement_key"]
    second = store_type.call_args_list[1].kwargs["acknowledgement_key"]
    assert first == second == b"k" * 32
    assert VALKEY_ENV[relay.API_V1_VALKEY_ACKNOWLEDGEMENT_KEY_ENV] not in repr(
        store_type.call_args_list
    )
