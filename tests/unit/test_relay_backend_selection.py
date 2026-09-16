import base64
from unittest.mock import Mock

import pytest

import relay
from relay_state_store import InMemoryRelayStateStore
from valkey_relay_state import (
    SCRIPT_DIGESTS,
    ValkeyFoundation,
    ValkeyRegistrationStore,
    ValkeySchemaIncompatibleError,
    ValkeyUnavailableError,
)


_PREFIX = "TOKENPLACE_RELAY_VALKEY_"
_SHARED_KEY = b"shared-runtime-acknowledgement-key"


def _valkey_environment(monkeypatch, **changes):
    values = {
        "DISCOVERY": "direct",
        "HOST": "127.0.0.1",
        "PORT": "6379",
        "ENVIRONMENT": "test",
        "CLUSTER": "runtime-selection",
        "SCHEMA_MAJOR": "1",
        "READER_REVISION": "1",
        "WRITER_REVISION": "1",
        "SUPPORTED_READ_MIN": "1",
        "SUPPORTED_READ_MAX": "1",
        "SUPPORTED_WRITER_MIN": "1",
        "SUPPORTED_WRITER_MAX": "1",
        "MIGRATION_EPOCH": "0",
        "CONNECT_TIMEOUT_SECONDS": "0.2",
        "SOCKET_TIMEOUT_SECONDS": "0.2",
        "COMMAND_TIMEOUT_SECONDS": "0.2",
        "RETRY_TIMEOUT_SECONDS": "0.2",
        "RETRY_ATTEMPTS": "0",
        "TLS": "false",
        "ACKNOWLEDGEMENT_KEY_BASE64": base64.b64encode(_SHARED_KEY).decode(),
    }
    values.update(changes)
    monkeypatch.setenv(relay.RELAY_STATE_BACKEND_ENV, "valkey")
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(_PREFIX + name, raising=False)
        else:
            monkeypatch.setenv(_PREFIX + name, value)


@pytest.fixture(autouse=True)
def _restore_store(monkeypatch):
    previous = relay.api_v1_relay_state_store
    relay.api_v1_relay_state_store = None
    yield
    current = relay.api_v1_relay_state_store
    if current is not None and current is not previous:
        close = getattr(current, "close", None)
        if callable(close):
            close()
    relay.api_v1_relay_state_store = previous


def test_memory_is_default_and_can_be_selected_explicitly(monkeypatch):
    monkeypatch.delenv(relay.RELAY_STATE_BACKEND_ENV, raising=False)
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)
    monkeypatch.setenv(relay.RELAY_STATE_BACKEND_ENV, "memory")
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)


def test_unknown_backend_is_rejected_without_memory_fallback(monkeypatch):
    monkeypatch.setenv(relay.RELAY_STATE_BACKEND_ENV, "VALKEY")
    with pytest.raises(RuntimeError, match="selection is invalid"):
        relay._new_api_v1_relay_state_store()


def test_valkey_selection_builds_reviewed_schema_and_shared_key(monkeypatch):
    _valkey_environment(monkeypatch)
    foundations = []

    def foundation_factory(config, manifest):
        foundation = object.__new__(ValkeyFoundation)
        foundation.config = config
        foundation.expected_manifest = manifest
        foundation.initialize_manifest = Mock(return_value=manifest)
        foundation.readiness = Mock()
        foundation.close = Mock()
        foundations.append(foundation)
        return foundation

    monkeypatch.setattr(relay, "ValkeyFoundation", foundation_factory)
    first = relay._new_api_v1_relay_state_store()
    second = relay._new_api_v1_relay_state_store()

    assert isinstance(first, ValkeyRegistrationStore)
    assert first._acknowledgement_key == second._acknowledgement_key == _SHARED_KEY
    assert first._acknowledgement_key is not second._acknowledgement_key
    identity = ("a" * 64, "b" * 64)
    assert first._derive_acknowledgement_token(identity, 1.5, "c" * 64) == (
        second._derive_acknowledgement_token(identity, 1.5, "c" * 64)
    )
    assert foundations[0].expected_manifest.script_digests == SCRIPT_DIGESTS
    assert foundations[0].initialize_manifest.call_count == 1
    assert foundations[0].readiness.call_count == 1
    assert _SHARED_KEY.decode() not in repr(first)


@pytest.mark.parametrize(
    ("setting", "value"),
    (("PORT", "secret-endpoint"), ("TLS", "yes"), ("DISCOVERY", "automatic")),
)
def test_malformed_valkey_configuration_is_bounded_and_redacted(
    monkeypatch, setting, value
):
    _valkey_environment(monkeypatch, **{setting: value})
    with pytest.raises(RuntimeError, match="initialization failed") as caught:
        relay._new_api_v1_relay_state_store()
    assert value not in str(caught.value)


def test_missing_shared_key_fails_before_foundation_construction(monkeypatch):
    _valkey_environment(monkeypatch, ACKNOWLEDGEMENT_KEY_BASE64=None)
    foundation = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", foundation)
    with pytest.raises(RuntimeError, match="initialization failed"):
        relay._new_api_v1_relay_state_store()
    foundation.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    (
        ValkeyUnavailableError("private endpoint"),
        ValkeySchemaIncompatibleError("bad"),
    ),
)
def test_valkey_startup_failure_closes_resources_and_never_falls_back(
    monkeypatch, failure
):
    _valkey_environment(monkeypatch)
    foundation = object.__new__(ValkeyFoundation)
    foundation.initialize_manifest = Mock(side_effect=failure)
    foundation.close = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", Mock(return_value=foundation))

    with pytest.raises(RuntimeError, match="initialization failed") as caught:
        relay._new_api_v1_relay_state_store()

    foundation.close.assert_called_once_with()
    assert "private endpoint" not in str(caught.value)


def test_reset_closes_old_store_and_leaves_no_fallback_on_failure(monkeypatch):
    old = Mock()
    relay.api_v1_relay_state_store = old
    monkeypatch.setattr(
        relay,
        "_new_api_v1_relay_state_store",
        Mock(side_effect=RuntimeError("initialization failed")),
    )

    with pytest.raises(RuntimeError, match="initialization failed"):
        relay._reset_api_v1_relay_state_store()

    old.close.assert_called_once_with()
    assert relay.api_v1_relay_state_store is None
