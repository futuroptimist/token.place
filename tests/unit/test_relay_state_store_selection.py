from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest

import relay
from relay_state_store import InMemoryRelayStateStore, RelayStateStoreError
from valkey_relay_state import (SCRIPT_DIGESTS, ValkeyReadOnlyError,
                                ValkeyRegistrationStore,
                                ValkeySchemaIncompatibleError,
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
    previous_store = relay.api_v1_relay_state_store
    previous_testing = relay.app.config["TESTING"]
    for name in tuple(relay.os.environ):
        if name == relay.API_V1_STATE_BACKEND_ENV or name.startswith(
            relay._VALKEY_ENV_PREFIX
        ):
            monkeypatch.delenv(name)
    relay.api_v1_relay_state_store = None
    try:
        yield
    finally:
        test_store = relay.api_v1_relay_state_store
        if test_store is not None and test_store is not previous_store:
            close = getattr(test_store, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        relay.api_v1_relay_state_store = previous_store
        relay.app.config["TESTING"] = previous_testing


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


def test_backend_selection_normalizes_case_and_whitespace(monkeypatch):
    monkeypatch.setenv(relay.API_V1_STATE_BACKEND_ENV, "  MEMORY  ")
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
    "sentinels",
    [
        "null",
        '"valkey.internal"',
        "[]",
        '{"host":"valkey.internal","port":26379}',
        '[{"host":"valkey.internal","port":26379}]',
        '[["valkey.internal"]]',
        '[["valkey.internal",26379,"extra"]]',
        '[["valkey.internal",true]]',
        '[["valkey.internal",0]]',
        '[["bad host",26379]]',
    ],
)
def test_malformed_sentinel_shape_is_bounded(monkeypatch, sentinels):
    _set_valkey_env(
        monkeypatch,
        TOKENPLACE_RELAY_VALKEY_DISCOVERY="sentinel",
        TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON=sentinels,
        TOKENPLACE_RELAY_VALKEY_SENTINEL_SERVICE="relay-primary",
    )
    with pytest.raises(
        RelayStateStoreError, match="invalid Valkey runtime configuration"
    ):
        relay._new_api_v1_relay_state_store()


def test_valid_sentinel_authentication_and_tls_configuration(monkeypatch):
    _set_valkey_env(
        monkeypatch,
        TOKENPLACE_RELAY_VALKEY_DISCOVERY="sentinel",
        TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON='[["sentinel-a",26379],["sentinel-b",26380]]',
        TOKENPLACE_RELAY_VALKEY_SENTINEL_SERVICE="relay-primary",
        TOKENPLACE_RELAY_VALKEY_SENTINEL_USERNAME="sentinel-user",
        TOKENPLACE_RELAY_VALKEY_SENTINEL_PASSWORD="sentinel-password",
        TOKENPLACE_RELAY_VALKEY_TLS="true",
        TOKENPLACE_RELAY_VALKEY_TLS_CA_CERT="/run/secrets/ca.pem",
        TOKENPLACE_RELAY_VALKEY_USERNAME="relay-user",
        TOKENPLACE_RELAY_VALKEY_PASSWORD="relay-password",
    )
    configs = []
    foundation = Mock()
    monkeypatch.setattr(
        relay,
        "ValkeyFoundation",
        lambda config, expected: (configs.append(config) or foundation),
    )
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", Mock(return_value=Mock()))

    relay._new_api_v1_relay_state_store()

    config = configs[0]
    assert config.sentinel.sentinels == (
        ("sentinel-a", 26379),
        ("sentinel-b", 26380),
    )
    assert config.sentinel.sentinel_username == "sentinel-user"
    assert config.tls is True
    assert config.tls_ca_cert == "/run/secrets/ca.pem"


@pytest.mark.parametrize(
    ("setting", "value", "attribute"),
    [
        ("TOKENPLACE_RELAY_VALKEY_USERNAME", "  relay-user  ", "username"),
        ("TOKENPLACE_RELAY_VALKEY_PASSWORD", "  relay-password  ", "password"),
        (
            "TOKENPLACE_RELAY_VALKEY_SENTINEL_USERNAME",
            "  sentinel-user  ",
            "sentinel_username",
        ),
        (
            "TOKENPLACE_RELAY_VALKEY_SENTINEL_PASSWORD",
            "  sentinel-password  ",
            "sentinel_password",
        ),
    ],
)
def test_optional_valkey_credentials_are_preserved_exactly(
    monkeypatch, setting, value, attribute
):
    _set_valkey_env(
        monkeypatch,
        TOKENPLACE_RELAY_VALKEY_DISCOVERY="sentinel",
        TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON='[["sentinel-a",26379]]',
        TOKENPLACE_RELAY_VALKEY_SENTINEL_SERVICE="relay-primary",
        **{setting: value},
    )
    configs = []

    class Foundation:
        def __init__(self, config, expected):
            configs.append(config)

        def initialize_manifest(self):
            pass

        def readiness(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(relay, "ValkeyFoundation", Foundation)
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", Mock(return_value=Mock()))

    relay._new_api_v1_relay_state_store()

    target = configs[0].sentinel if attribute.startswith("sentinel_") else configs[0]
    assert getattr(target, attribute) == value


def test_absent_optional_valkey_settings_remain_absent(monkeypatch):
    _set_valkey_env(monkeypatch)
    configs = []
    foundation = Mock()
    monkeypatch.setattr(
        relay,
        "ValkeyFoundation",
        lambda config, expected: (configs.append(config) or foundation),
    )
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", Mock(return_value=Mock()))

    relay._new_api_v1_relay_state_store()

    config = configs[0]
    assert config.username is None
    assert config.password is None
    assert config.tls_ca_cert is None
    assert config.tls_client_cert is None
    assert config.tls_client_key is None


@pytest.mark.parametrize(
    "name",
    [
        "USERNAME",
        "PASSWORD",
        "SENTINEL_USERNAME",
        "SENTINEL_PASSWORD",
        "TLS_CA_CERT",
        "TLS_CLIENT_CERT",
        "TLS_CLIENT_KEY",
    ],
)
def test_explicit_blank_optional_valkey_settings_fail_before_io(
    monkeypatch, caplog, name
):
    secret = " \t "
    changes = {f"TOKENPLACE_RELAY_VALKEY_{name}": secret}
    if name.startswith("SENTINEL_"):
        changes.update(
            TOKENPLACE_RELAY_VALKEY_DISCOVERY="sentinel",
            TOKENPLACE_RELAY_VALKEY_SENTINELS_JSON='[["sentinel-a",26379]]',
            TOKENPLACE_RELAY_VALKEY_SENTINEL_SERVICE="relay-primary",
        )
    _set_valkey_env(monkeypatch, **changes)
    constructor = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", constructor)

    with pytest.raises(
        RelayStateStoreError, match="^invalid Valkey runtime configuration$"
    ) as caught:
        relay._new_api_v1_relay_state_store()

    constructor.assert_not_called()
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in caplog.text


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


def test_cleanup_failure_does_not_replace_original_bounded_failure(monkeypatch):
    _set_valkey_env(monkeypatch)
    foundation = Mock()
    foundation.initialize_manifest.side_effect = ValkeyUnavailableError(
        "state backend unavailable"
    )
    foundation.close.side_effect = RuntimeError("cleanup-secret.example:6380/password")
    monkeypatch.setattr(relay, "ValkeyFoundation", Mock(return_value=foundation))

    with pytest.raises(
        ValkeyUnavailableError, match="^state backend unavailable$"
    ) as caught:
        relay._new_api_v1_relay_state_store()

    assert "cleanup-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("TOKENPLACE_RELAY_VALKEY_SCHEMA_MAJOR", "2"),
        ("TOKENPLACE_RELAY_VALKEY_READER_REVISION", "2"),
        ("TOKENPLACE_RELAY_VALKEY_WRITER_REVISION", "2"),
        ("TOKENPLACE_RELAY_VALKEY_SUPPORTED_SCHEMA_READ_MAX", "2"),
        ("TOKENPLACE_RELAY_VALKEY_SUPPORTED_WRITER_MAX", "2"),
        ("TOKENPLACE_RELAY_VALKEY_ACTIVE_SCHEMA_REVISION", "2"),
        ("TOKENPLACE_RELAY_VALKEY_ACTIVE_WRITER_REVISION", "2"),
    ],
)
def test_environment_cannot_expand_reviewed_schema_support(
    monkeypatch, setting, value
):
    _set_valkey_env(monkeypatch, **{setting: value})
    constructor = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", constructor)

    with pytest.raises(
        RelayStateStoreError, match="^invalid Valkey runtime configuration$"
    ):
        relay._new_api_v1_relay_state_store()

    constructor.assert_not_called()


def test_invalid_combined_namespace_fails_before_foundation_construction(monkeypatch):
    _set_valkey_env(
        monkeypatch,
        TOKENPLACE_RELAY_VALKEY_ENVIRONMENT="e" * 64,
        TOKENPLACE_RELAY_VALKEY_CLUSTER="c" * 64,
    )
    constructor = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", constructor)

    with pytest.raises(RelayStateStoreError, match="namespace must be"):
        relay._new_api_v1_relay_state_store()

    constructor.assert_not_called()


def test_reset_replaces_then_closes_old_store(monkeypatch):
    old = Mock()
    lock_observations = []
    old.close.side_effect = lambda: lock_observations.append(
        relay._api_v1_stale_lease_eviction_lock.locked()
    )
    new = Mock()
    relay.api_v1_relay_state_store = old
    monkeypatch.setattr(relay, "_new_api_v1_relay_state_store", Mock(return_value=new))

    relay._reset_api_v1_relay_state_store()

    assert relay.api_v1_relay_state_store is new
    old.close.assert_called_once_with()
    assert lock_observations == [False]


def test_successful_reset_publishes_replacement_despite_old_cleanup_failure(
    monkeypatch,
):
    old = Mock()
    old.close.side_effect = RuntimeError("old-store-secret.example:6380/password")
    new = Mock()
    relay.api_v1_relay_state_store = old
    monkeypatch.setattr(relay, "_new_api_v1_relay_state_store", Mock(return_value=new))

    relay._reset_api_v1_relay_state_store()

    assert relay.api_v1_relay_state_store is new
    old.close.assert_called_once_with()


def test_runtime_valkey_failure_is_a_store_error():
    assert issubclass(ValkeyUnavailableError, RelayStateStoreError)


@pytest.mark.parametrize(
    "failure_type",
    [ValkeyUnavailableError, ValkeyReadOnlyError, ValkeySchemaIncompatibleError],
)
def test_selected_valkey_runtime_failure_returns_redacted_bounded_route_response(
    monkeypatch, caplog, failure_type
):
    secret = "redis://user:password@injected.example:6380/key/payload/token"

    class FailingValkeyStore(ValkeyRegistrationStore):
        def __init__(self, foundation, config, *, acknowledgement_key):
            pass

        def get(self, node_id):
            raise failure_type(secret)

    _set_valkey_env(monkeypatch)
    foundation = Mock()
    monkeypatch.setattr(relay, "ValkeyFoundation", Mock(return_value=foundation))
    monkeypatch.setattr(relay, "ValkeyRegistrationStore", FailingValkeyStore)
    relay.api_v1_relay_state_store = relay._new_api_v1_relay_state_store()
    relay.app.config["TESTING"] = True

    response = relay.app.test_client().post(
        "/api/v1/relay/servers/register",
        json={
            "server_public_key": "node-key",
            "capabilities": {
                "supported_model_ids": ["qwen3-8b-instruct"],
                "active_context_tier": "8k-fast",
                "maximum_total_context_tokens": 8192,
                "default_output_token_reservation": 1024,
                "maximum_output_tokens": 1024,
                "max_concurrency": 1,
            },
        },
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "state_backend_unavailable",
            "message": "Relay state is temporarily unavailable",
        }
    }
    assert secret not in response.get_data(as_text=True)
    assert secret not in caplog.text
    assert secret not in repr(relay.api_v1_relay_state_store)


def test_valkey_startup_failure_returns_redacted_bounded_route_response(
    monkeypatch, caplog
):
    secret = "redis://user:password@startup.example:6380/key/payload/token"
    _set_valkey_env(monkeypatch)
    foundation = Mock()
    foundation.initialize_manifest.side_effect = ValkeyUnavailableError(secret)
    monkeypatch.setattr(relay, "ValkeyFoundation", Mock(return_value=foundation))
    relay.app.config["TESTING"] = True

    response = relay.app.test_client().post(
        "/api/v1/relay/servers/register",
        json={
            "server_public_key": "node-key",
            "capabilities": {
                "supported_model_ids": ["qwen3-8b-instruct"],
                "active_context_tier": "8k-fast",
                "maximum_total_context_tokens": 8192,
                "default_output_token_reservation": 1024,
                "maximum_output_tokens": 1024,
                "max_concurrency": 1,
            },
        },
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == {
        "code": "state_backend_unavailable",
        "message": "Relay state is temporarily unavailable",
    }
    assert secret not in response.get_data(as_text=True)
    assert secret not in caplog.text


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
