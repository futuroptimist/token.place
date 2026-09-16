from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest

import relay
from relay_state_store import InMemoryRelayStateStore
from valkey_relay_state import (
    SCRIPT_DIGESTS,
    ValkeySchemaIncompatibleError,
    ValkeyUnavailableError,
)

_PREFIX = relay.API_V1_VALKEY_ENV_PREFIX
_SHARED_KEY = b"replica-stable-acknowledgement-key"


def _valkey_environment(monkeypatch, **overrides):
    values = {
        "DISCOVERY": "direct",
        "HOST": "valkey.internal",
        "PORT": "6379",
        "ENVIRONMENT": "staging",
        "CLUSTER": "relay-a",
        "SCHEMA_MAJOR": "1",
        "READER_REVISION": "1",
        "WRITER_REVISION": "1",
        "ACKNOWLEDGEMENT_KEY": base64.b64encode(_SHARED_KEY).decode(),
    }
    values.update(overrides)
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "valkey")
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(_PREFIX + name, raising=False)
        else:
            monkeypatch.setenv(_PREFIX + name, value)


@pytest.fixture(autouse=True)
def _restore_memory_store(monkeypatch):
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "memory")
    relay._reset_api_v1_relay_state_store()
    yield
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "memory")
    relay._reset_api_v1_relay_state_store()


def test_memory_is_default_and_explicit_memory_selection(monkeypatch):
    monkeypatch.delenv(relay.API_V1_STATE_BACKEND_ENV)
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "memory")
    assert isinstance(relay._new_api_v1_relay_state_store(), InMemoryRelayStateStore)


@pytest.mark.parametrize("backend", ["", "redis", "VALKEY", " valkey"])
def test_unknown_backend_is_rejected_without_fallback(monkeypatch, backend):
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, backend)
    with pytest.raises(ValueError, match="invalid relay state backend"):
        relay._new_api_v1_relay_state_store()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DISCOVERY", "cluster"),
        ("PORT", "6379x"),
        ("ENVIRONMENT", "secret@endpoint"),
        ("SCHEMA_MAJOR", "2"),
        ("READER_REVISION", "2"),
        ("TLS", "yes"),
    ],
)
def test_invalid_valkey_configuration_is_bounded_and_redacted(monkeypatch, name, value):
    _valkey_environment(monkeypatch, **{name: value})
    with pytest.raises(Exception) as caught:
        relay._new_api_v1_relay_state_store()
    rendered = str(caught.value)
    assert len(rendered) < 100
    assert value not in rendered


def test_missing_acknowledgement_key_fails_before_connecting(monkeypatch):
    _valkey_environment(monkeypatch, ACKNOWLEDGEMENT_KEY=None)
    foundation = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", foundation)
    with pytest.raises(ValueError, match="invalid Valkey runtime configuration"):
        relay._new_api_v1_relay_state_store()
    foundation.assert_not_called()


def test_explicit_valkey_constructs_reviewed_schema_and_shared_key(monkeypatch):
    _valkey_environment(monkeypatch)
    foundation = Mock()
    foundation_instance = foundation.return_value
    store = object()
    store_constructor = Mock(return_value=store)
    monkeypatch.setattr(relay, "ValkeyFoundation", foundation)
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", store_constructor)

    assert relay._new_api_v1_relay_state_store() is store

    config, manifest = foundation.call_args.args
    assert config.direct.host == "valkey.internal"
    assert config.direct.port == 6379
    assert config.key_prefix == "tokenplace:{staging:relay-a}:relay:v1:"
    assert dict(manifest.script_digests) == dict(SCRIPT_DIGESTS)
    foundation_instance.initialize_manifest.assert_called_once_with()
    foundation_instance.readiness.assert_called_once_with()
    assert store_constructor.call_args.kwargs["acknowledgement_key"] == _SHARED_KEY
    assert _SHARED_KEY.decode() not in repr(config)


@pytest.mark.parametrize(
    "failure",
    [
        ValkeyUnavailableError("state backend unavailable"),
        ValkeySchemaIncompatibleError("state schema incompatible"),
    ],
)
def test_valkey_startup_failure_closes_and_never_falls_back(monkeypatch, failure):
    _valkey_environment(monkeypatch)
    foundation = Mock()
    foundation.return_value.initialize_manifest.side_effect = failure
    memory_constructor = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", foundation)
    monkeypatch.setattr(relay, "InMemoryRelayStateStore", memory_constructor)

    with pytest.raises(type(failure), match=str(failure)):
        relay._new_api_v1_relay_state_store()

    foundation.return_value.close.assert_called_once_with()
    memory_constructor.assert_not_called()


def test_reset_closes_old_store_before_replacement(monkeypatch):
    old_store = Mock()
    replacement = Mock()
    relay.api_v1_relay_state_store = old_store
    monkeypatch.setattr(
        relay, "_new_api_v1_relay_state_store", Mock(return_value=replacement)
    )

    relay._reset_api_v1_relay_state_store()

    old_store.close.assert_called_once_with()
    assert relay._api_v1_store() is replacement


def test_independent_valkey_stores_receive_identical_acknowledgement_key(monkeypatch):
    _valkey_environment(monkeypatch)
    foundations = []
    keys = []

    def foundation_constructor(*args):
        instance = Mock()
        foundations.append(instance)
        return instance

    def store_constructor(foundation, config, *, acknowledgement_key):
        keys.append(acknowledgement_key)
        return Mock()

    monkeypatch.setattr(relay, "ValkeyFoundation", foundation_constructor)
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", store_constructor)

    relay._new_api_v1_relay_state_store()
    relay._new_api_v1_relay_state_store()

    assert keys == [_SHARED_KEY, _SHARED_KEY]
    assert len(foundations) == 2
