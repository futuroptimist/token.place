from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest

import relay
from relay_state_store import InMemoryRelayStateStore, RelayStateStoreError
from valkey_relay_state import (SCRIPT_DIGESTS, ValkeySchemaIncompatibleError,
                                ValkeyUnavailableError)

VALKEY_ENV = {
    "TOKENPLACE_RELAY_STATE_BACKEND": "valkey",
    "TOKENPLACE_RELAY_VALKEY_DISCOVERY": "direct",
    "TOKENPLACE_RELAY_VALKEY_HOST": "valkey.internal",
    "TOKENPLACE_RELAY_VALKEY_PORT": "6379",
    "TOKENPLACE_RELAY_VALKEY_ENVIRONMENT": "testing",
    "TOKENPLACE_RELAY_VALKEY_CLUSTER": "relay-a",
    "TOKENPLACE_RELAY_VALKEY_SCHEMA_MAJOR": "1",
    "TOKENPLACE_RELAY_VALKEY_READER_REVISION": "1",
    "TOKENPLACE_RELAY_VALKEY_WRITER_REVISION": "1",
    "TOKENPLACE_RELAY_VALKEY_SUPPORTED_SCHEMA_READ_MIN": "1",
    "TOKENPLACE_RELAY_VALKEY_SUPPORTED_SCHEMA_READ_MAX": "1",
    "TOKENPLACE_RELAY_VALKEY_SUPPORTED_WRITER_MIN": "1",
    "TOKENPLACE_RELAY_VALKEY_SUPPORTED_WRITER_MAX": "1",
    "TOKENPLACE_RELAY_VALKEY_ACTIVE_SCHEMA_REVISION": "1",
    "TOKENPLACE_RELAY_VALKEY_ACTIVE_WRITER_REVISION": "1",
    "TOKENPLACE_RELAY_VALKEY_MIGRATION_EPOCH": "0",
    "TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64": base64.b64encode(
        b"shared-acknowledgement-secret-32b"
    ).decode(),
}


@pytest.fixture(autouse=True)
def isolated_selection(monkeypatch):
    for name in tuple(relay.os.environ):
        if name == relay.API_V1_STATE_BACKEND_ENV or name.startswith(
            relay._VALKEY_ENV_PREFIX
        ):
            monkeypatch.delenv(name)
    relay.api_v1_relay_state_store = None
    yield
    relay.api_v1_relay_state_store = None


def _set_valkey_env(monkeypatch, **changes):
    values = {**VALKEY_ENV, **changes}
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_memory_is_default_and_explicit_selection(monkeypatch):
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "memory")
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "redis")
    with pytest.raises(RelayStateStoreError, match="unsupported relay state backend"):
        relay._new_api_v1_relay_state_store()


def test_explicit_valkey_builds_reviewed_schema_and_shared_key(monkeypatch):
    _set_valkey_env(monkeypatch)
    foundations = []

    class Foundation:
        def __init__(self, config, expected):
            self.config = config
            self.expected = expected
            self.initialized = False
            self.ready = False
            foundations.append(self)

        def initialize_manifest(self):
            self.initialized = True

        def readiness(self):
            self.ready = True

        def close(self):
            pass

    stores = []

    class Store:
        def __init__(self, foundation, config, *, acknowledgement_key):
            self.foundation = foundation
            self.config = config
            self.key = acknowledgement_key
            stores.append(self)

    monkeypatch.setattr(relay, "ValkeyFoundation", Foundation)
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", Store)

    first = relay._new_api_v1_relay_state_store()
    second = relay._new_api_v1_relay_state_store()

    assert first.key == second.key == b"shared-acknowledgement-secret-32b"
    assert foundations[0].initialized and foundations[0].ready
    assert foundations[0].expected.script_digests == SCRIPT_DIGESTS
    assert first.config.namespace == "testing.relay-a"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("TOKENPLACE_RELAY_VALKEY_PORT", "not-a-port"),
        ("TOKENPLACE_RELAY_VALKEY_DISCOVERY", "automatic"),
        ("TOKENPLACE_RELAY_VALKEY_TLS", "yes"),
        ("TOKENPLACE_RELAY_VALKEY_SCHEMA_MAJOR", "0"),
        ("TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64", "not base64"),
    ],
)
def test_invalid_valkey_configuration_never_constructs_foundation(
    monkeypatch, setting, value
):
    _set_valkey_env(monkeypatch, **{setting: value})
    constructor = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", constructor)
    with pytest.raises(RelayStateStoreError):
        relay._new_api_v1_relay_state_store()
    constructor.assert_not_called()


def test_missing_shared_acknowledgement_key_fails_before_connect(monkeypatch):
    _set_valkey_env(
        monkeypatch, TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64=None
    )
    constructor = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", constructor)
    with pytest.raises(
        RelayStateStoreError, match="invalid Valkey runtime configuration"
    ):
        relay._new_api_v1_relay_state_store()
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        ValkeyUnavailableError("state backend unavailable"),
        ValkeySchemaIncompatibleError("state schema incompatible"),
    ],
)
def test_valkey_failure_is_redacted_and_never_falls_back(monkeypatch, failure):
    _set_valkey_env(monkeypatch)
    foundation = Mock()
    foundation.initialize_manifest.side_effect = failure
    monkeypatch.setattr(relay, "ValkeyFoundation", Mock(return_value=foundation))
    memory = Mock()
    monkeypatch.setattr(relay, "InMemoryRelayStateStore", memory)

    with pytest.raises(RelayStateStoreError, match=str(failure)) as caught:
        relay._new_api_v1_relay_state_store()

    assert "valkey.internal" not in str(caught.value)
    memory.assert_not_called()
    foundation.close.assert_called_once_with()


def test_reset_replaces_then_closes_old_store(monkeypatch):
    old = Mock()
    new = Mock()
    relay.api_v1_relay_state_store = old
    monkeypatch.setattr(relay, "_new_api_v1_relay_state_store", Mock(return_value=new))

    relay._reset_api_v1_relay_state_store()

    assert relay.api_v1_relay_state_store is new
    old.close.assert_called_once_with()


def test_failed_reset_keeps_existing_store_open(monkeypatch):
    old = Mock()
    relay.api_v1_relay_state_store = old
    monkeypatch.setattr(
        relay,
        "_new_api_v1_relay_state_store",
        Mock(side_effect=ValkeyUnavailableError("state backend unavailable")),
    )

    with pytest.raises(ValkeyUnavailableError):
        relay._reset_api_v1_relay_state_store()

    assert relay.api_v1_relay_state_store is old
    old.close.assert_not_called()
