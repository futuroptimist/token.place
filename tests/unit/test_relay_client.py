"""
Unit tests for the relay client module.
"""
import base64
import builtins
import json
import math
import pytest
import sys
import requests
import jsonschema
from unittest.mock import MagicMock, patch
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.networking import relay_client as relay_client_module
from utils.networking.relay_client import RelayClient, MESSAGE_SCHEMA, RELAY_RESPONSE_SCHEMA

# Common test data
TEST_VALID_RESPONSE = {
    'client_public_key': 'Y2xpZW50X2tleV9iNjQ=',  # Base64 encoded "client_key_b64"
    'chat_history': 'encrypted_data',
    'cipherkey': 'key',
    'iv': 'iv',
    'next_ping_in_x_seconds': 5
}

TEST_ERROR_RESPONSE = {
    'error': 'Connection refused',
    'next_ping_in_x_seconds': 10
}

TEST_NO_REQUEST_RESPONSE = {
    'next_ping_in_x_seconds': 5
}


def _load_compute_node_bridge_module():
    import importlib.util

    module_path = (
        Path(__file__).resolve().parents[2]
        / "desktop-tauri"
        / "src-tauri"
        / "python"
        / "compute_node_bridge.py"
    )
    module_dir = str(module_path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location("compute_node_bridge_for_unit_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sanitize_relay_target_handles_malformed_ports_and_ipv6():
    assert relay_client_module._sanitize_relay_target('https://relay.example:bad') == 'unknown'
    assert relay_client_module._sanitize_relay_target('http://[::1') == 'unknown'
    assert (
        relay_client_module._sanitize_relay_target('http://[::1]:8000/path?token=abc#debug')
        == 'http://[::1]:8000'
    )
    assert (
        relay_client_module._sanitize_relay_target(
            'https://user:pass@[2001:db8::1]:9443/source?token=abc'
        )
        == 'https://[2001:db8::1]:9443'
    )


def test_load_jsonschema_returns_none_on_import_error(monkeypatch):
    import importlib

    def _raise_import_error(name: str):
        raise ImportError("simulated transitive import failure")

    monkeypatch.setattr(importlib, "import_module", _raise_import_error)
    assert relay_client_module._load_jsonschema() is None


def test_max_poll_failures_defaults_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("TOKENPLACE_MAX_POLL_FAILURES", raising=False)
    assert relay_client_module._max_poll_failures_before_stop() == 18


def test_max_poll_failures_honours_override(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_MAX_POLL_FAILURES", "3")
    assert relay_client_module._max_poll_failures_before_stop() == 3


def test_desktop_bridge_registration_fresh_helper_rejects_stale_client_state(monkeypatch):
    bridge = _load_compute_node_bridge_module()
    client = MagicMock()
    client.api_v1_registration_fresh.return_value = False

    assert bridge._registration_fresh(client, "https://staging.token.place") is False
    client.api_v1_registration_fresh.assert_called_once_with("https://staging.token.place")


def test_desktop_bridge_registration_fresh_helper_accepts_recent_heartbeat(monkeypatch):
    bridge = _load_compute_node_bridge_module()
    client = MagicMock()
    client.api_v1_registration_fresh.return_value = True

    assert bridge._registration_fresh(client, "https://staging.token.place") is True


def test_desktop_bridge_cached_poll_wait_helper_uses_relay_hint(monkeypatch):
    bridge = _load_compute_node_bridge_module()
    client = MagicMock()
    client._api_v1_relay_wait_hints = {
        "https://staging.token.place": {"poll_wait_seconds": "30"}
    }

    assert (
        bridge._cached_poll_wait_seconds(client, "https://staging.token.place", 15)
        == 30.0
    )


def test_validate_with_fallback_accepts_message_schema_without_jsonschema():
    payload = {
        "client_public_key": "abc",
        "chat_history": "def",
        "cipherkey": "ghi",
        "iv": "jkl",
    }

    with patch.object(
        relay_client_module,
        "_load_jsonschema",
        side_effect=RuntimeError("jsonschema is required for relay schema validation at runtime"),
    ):
        relay_client_module._validate_with_fallback(payload, MESSAGE_SCHEMA)


def test_validate_with_fallback_rejects_missing_required_field_without_jsonschema():
    payload = {
        "client_public_key": "abc",
        "chat_history": "def",
        "cipherkey": "ghi",
    }

    with patch.object(
        relay_client_module,
        "_load_jsonschema",
        side_effect=RuntimeError("jsonschema is required for relay schema validation at runtime"),
    ):
        with pytest.raises(ValueError, match="Missing required field: iv"):
            relay_client_module._validate_with_fallback(payload, MESSAGE_SCHEMA)

# Create a better time mock with a context manager
class TimeMock:
    """A context manager for mocking time.sleep"""
    def __init__(self, mock_sleep):
        self.mock_sleep = mock_sleep
        self.sleep_calls = []

    def __enter__(self):
        # Save original side_effect if it exists
        self.original_side_effect = self.mock_sleep.side_effect

        # Create a wrapper to capture the sleep calls
        def wrapper(seconds):
            self.sleep_calls.append(seconds)
            # If there's an original side effect that's callable, call it
            if callable(self.original_side_effect):
                return self.original_side_effect(seconds)
            return None

        self.mock_sleep.side_effect = wrapper
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        # Restore original side_effect (if needed)
        if hasattr(self, 'original_side_effect'):
            self.mock_sleep.side_effect = self.original_side_effect

    def assert_slept_for(self, seconds):
        """Assert that sleep was called with the given duration"""
        assert seconds in self.sleep_calls, f"Expected sleep({seconds}), got {self.sleep_calls}"


def test_init_propagates_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(
        'utils.networking.relay_client.get_config_lazy',
        MagicMock(side_effect=KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        RelayClient('http://localhost', 8080, object(), object())


def test_compose_relay_url_preserves_ipv6_brackets():
    """IPv6 relay targets must keep brackets when injecting the port."""

    result = RelayClient._compose_relay_url('http://[2001:db8::1]', 8080)
    assert result == 'http://[2001:db8::1]:8080'


def test_compose_relay_url_keeps_https_without_injected_port():
    result = RelayClient._compose_relay_url('https://token.place', None)
    assert result == 'https://token.place'


def test_compose_relay_url_honors_explicit_https_port_override():
    result = RelayClient._compose_relay_url('https://token.place', 7443)
    assert result == 'https://token.place:7443'


def test_compose_relay_url_keeps_explicit_localhost_port():
    result = RelayClient._compose_relay_url('http://localhost:5000', 1234)
    assert result == 'http://localhost:5000'


def test_init_can_disable_configured_server_fallbacks():
    mock_config = MagicMock()
    mock_config.is_production = False
    mock_config.get.side_effect = lambda key, default: {
        'relay.request_timeout': 10,
        'relay.cluster_only': True,
        'relay.server_url': 'https://token.place',
        'relay.additional_servers': ['https://relay-backup.example'],
    }.get(key, default)

    with patch('utils.networking.relay_client.get_config_lazy', return_value=mock_config):
        client = RelayClient(
            base_url='http://127.0.0.1:5010',
            port=None,
            crypto_manager=object(),
            model_manager=object(),
            include_configured_servers=False,
        )

    assert client.relay_urls == ('http://127.0.0.1:5010',)


def test_init_excludes_config_and_env_fallback_relays_when_configured_servers_disabled(monkeypatch):
    mock_config = MagicMock()
    mock_config.is_production = False
    mock_config.get.side_effect = lambda key, default: {
        'relay.request_timeout': 10,
        'relay.cluster_only': True,
        'relay.server_url': 'https://token.place',
        'relay.additional_servers': ['https://relay-backup.example'],
        'relay.cloudflare_fallback_urls': ['https://cloudflare-a.example'],
    }.get(key, default)

    monkeypatch.setenv(
        'TOKEN_PLACE_RELAY_UPSTREAMS',
        'https://token.place,https://env-upstream.example',
    )
    monkeypatch.setenv('TOKEN_PLACE_RELAY_CLOUDFLARE_URL', 'https://token.place')
    monkeypatch.setenv(
        'TOKEN_PLACE_RELAY_CLOUDFLARE_URLS',
        '["https://token.place","https://env-cloudflare.example"]',
    )

    with patch('utils.networking.relay_client.get_config_lazy', return_value=mock_config):
        client = RelayClient(
            base_url='http://127.0.0.1:5010',
            port=None,
            crypto_manager=object(),
            model_manager=object(),
            include_configured_servers=False,
        )

    assert client.relay_urls == ('http://127.0.0.1:5010',)
    assert 'https://token.place' not in client.relay_urls


class TestRelayClient:
    """Test class for RelayClient."""

    @pytest.fixture
    def mock_crypto_manager(self):
        """Fixture for a mock crypto manager."""
        mock = MagicMock()
        mock.public_key_b64 = 'mock_public_key_b64'
        mock.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv'
        }
        mock.decrypt_message.return_value = [
            {"role": "user", "content": "What is the capital of France?"}
        ]
        return mock

    @pytest.fixture
    def mock_model_manager(self):
        """Fixture for a mock model manager."""
        mock = MagicMock()
        mock.llama_cpp_get_response.return_value = [
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": "The capital of France is Paris."}
        ]
        mock.runtime = MagicMock()
        mock.runtime.create_chat_completion.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The capital of France is Paris.",
                    }
                }
            ]
        }
        mock.get_llm_instance.return_value = mock.runtime
        mock.create_chat_completion_with_recovery = None
        mock.use_mock_llm = True
        return mock

    @pytest.fixture
    def config_values(self):
        """Fixture for mock config values."""
        return {
            'relay.request_timeout': 15
        }

    @pytest.fixture
    def relay_client(self, mock_crypto_manager, mock_model_manager, config_values):
        """Fixture that returns a relay client instance with mocked dependencies."""
        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            # Create a MagicMock that also implements get method
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://localhost",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager
            )
            return client

    @pytest.fixture
    def mock_http_response(self):
        """Fixture for mock HTTP response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Success"
        return mock_response

    def test_initialization(self, relay_client, mock_crypto_manager, mock_model_manager, config_values):
        """Test RelayClient initialization."""
        assert relay_client.base_url == "http://localhost"
        assert relay_client.port == 5000
        assert relay_client.crypto_manager == mock_crypto_manager
        assert relay_client.model_manager == mock_model_manager
        assert relay_client.relay_url == "http://localhost:5000"
        assert relay_client.stop_polling is True  # Now initialized to True
        assert relay_client._request_timeout == 15  # Value from config fixture

    def test_start_stop_methods(self, relay_client):
        """Test start and stop methods."""
        # Client starts with stop_polling = True
        assert relay_client.stop_polling is True

        # After start(), stop_polling should be False
        relay_client.start()
        assert relay_client.stop_polling is False

        # After stop(), stop_polling should be True again
        relay_client.stop()
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_success(self, mock_post, relay_client):
        """Unregister should post to /api/v1/relay/servers/unregister and return True on success after registration."""

        relay_client._api_v1_registered_relays.add(relay_client.relay_url)
        relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = 1.0
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = relay_client.unregister_from_relay()

        assert result is True
        mock_post.assert_called_once_with(
            'http://localhost:5000/api/v1/relay/servers/unregister',
            json={'server_public_key': 'mock_public_key_b64'},
            timeout=relay_client._request_timeout,
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_skips_when_never_registered(self, mock_post, relay_client):
        """Unregister should be a local no-op before API v1 registration succeeds."""

        assert relay_client.unregister_from_relay() is True
        mock_post.assert_not_called()
        assert relay_client._unregister_complete is True

    def test_unregister_from_relay_attempts_all_configured_relays(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Unregister should attempt all relay targets before returning."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.additional_servers': ['http://backup-relay:6000'],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        client._api_v1_registered_relays.update(client._relay_urls)
        for relay_url in client._relay_urls:
            client._api_v1_last_heartbeat_at[relay_url] = 1.0
        success_response = MagicMock()
        success_response.status_code = 200
        failure_response = MagicMock()
        failure_response.status_code = 503
        failure_response.text = "Service unavailable"

        with patch('utils.networking.relay_client.requests.post') as mock_post:
            mock_post.side_effect = [success_response, failure_response]
            assert client.unregister_from_relay() is False

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            'http://primary-relay:5000/api/v1/relay/servers/unregister',
            'http://backup-relay:6000/api/v1/relay/servers/unregister',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_retries_after_transient_failure(self, mock_post, relay_client):
        """A failed unregister attempt should not make later attempts no-ops."""

        relay_client._api_v1_registered_relays.add(relay_client.relay_url)
        relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = 1.0
        failure_response = MagicMock()
        failure_response.status_code = 503
        success_response = MagicMock()
        success_response.status_code = 200
        mock_post.side_effect = [failure_response, success_response]

        assert relay_client.unregister_from_relay() is False
        assert relay_client.unregister_from_relay() is True

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            'http://localhost:5000/api/v1/relay/servers/unregister',
            'http://localhost:5000/api/v1/relay/servers/unregister',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_keeps_failed_registered_relay_for_retry(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Partial multi-relay unregisters should retain only failed local state."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.additional_servers': ['http://backup-relay:6000'],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        primary = 'http://primary-relay:5000'
        backup = 'http://backup-relay:6000'
        client._api_v1_registered_relays.update({primary, backup})
        client._api_v1_last_heartbeat_at.update({primary: 1.0, backup: 2.0})
        client._api_v1_relay_wait_hints = {
            primary: {'next_ping_in_x_seconds': 30},
            backup: {'next_ping_in_x_seconds': 30},
        }
        success_response = MagicMock(status_code=200)
        failure_response = MagicMock(status_code=503)
        retry_success_response = MagicMock(status_code=200)
        mock_post.side_effect = [success_response, failure_response, retry_success_response]

        assert client.unregister_from_relay() is False
        assert client._api_v1_registered_relays == {backup}
        assert primary not in client._api_v1_last_heartbeat_at
        assert primary not in client._api_v1_relay_wait_hints
        assert backup in client._api_v1_last_heartbeat_at
        assert backup in client._api_v1_relay_wait_hints

        assert client.unregister_from_relay() is True

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            f'{primary}/api/v1/relay/servers/unregister',
            f'{backup}/api/v1/relay/servers/unregister',
            f'{backup}/api/v1/relay/servers/unregister',
        ]
        assert client._api_v1_registered_relays == set()
        assert client._api_v1_last_heartbeat_at == {}
        assert client._api_v1_relay_wait_hints == {}

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_uses_registration_token(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Registration token headers should be forwarded to unregister calls."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.server_registration_token': 'alpha-token',
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://localhost",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        client._api_v1_registered_relays.add(client.relay_url)
        client._api_v1_last_heartbeat_at[client.relay_url] = 1.0
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        assert client.unregister_from_relay() is True
        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.kwargs['headers'] == {'X-Relay-Server-Token': 'alpha-token'}

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_success(self, mock_post, relay_client, mock_crypto_manager):
        """Test successful ping to relay."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = TEST_NO_REQUEST_RESPONSE
        mock_post.return_value = mock_response

        # Call the method
        result = relay_client.ping_relay()

        # Check the result
        assert result == TEST_NO_REQUEST_RESPONSE

        # Verify mock calls
        mock_post.assert_called_once_with(
            'http://localhost:5000/sink',
            json={'server_public_key': 'mock_public_key_b64'},
            timeout=relay_client._request_timeout
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_uses_registration_token(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Registration tokens should be sent with sink requests when configured."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.server_registration_token': 'alpha-token',
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://localhost",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = TEST_NO_REQUEST_RESPONSE
        mock_post.return_value = mock_response

        result = client.ping_relay()

        assert result == TEST_NO_REQUEST_RESPONSE
        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.kwargs['headers'] == {'X-Relay-Server-Token': 'alpha-token'}

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_http_error(self, mock_post, relay_client):
        """Test ping to relay with HTTP error."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_post.return_value = mock_response

        # Call the method
        result = relay_client.ping_relay()

        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "HTTP 500"
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout

    def test_ping_relay_failover_to_additional_servers(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """RelayClient should fail over to additional servers when the primary fails."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.additional_servers': ['http://backup-relay:6000'],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        client._api_v1_registered_relays.update(client._relay_urls)
        for relay_url in client._relay_urls:
            client._api_v1_last_heartbeat_at[relay_url] = 1.0
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = TEST_NO_REQUEST_RESPONSE

        failure_response = MagicMock()
        failure_response.status_code = 503
        failure_response.text = "Service unavailable"

        with patch('utils.networking.relay_client.requests.post') as mock_post:
            mock_post.side_effect = [failure_response, success_response]

            result = client.ping_relay()

        assert result == TEST_NO_REQUEST_RESPONSE

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            'http://primary-relay:5000/sink',
            'http://backup-relay:6000/sink',
        ]
        assert client.relay_url == 'http://backup-relay:6000'

    def test_ping_relay_cloudflare_fallback(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """RelayClient should fall back to Cloudflare relays when configured."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.cloudflare_fallback_urls': [
                'https://relay.cloudflare.workers.dev/api/v1'
            ],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        failure_response = MagicMock()
        failure_response.status_code = 503
        failure_response.text = "Service unavailable"

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = TEST_NO_REQUEST_RESPONSE

        with patch('utils.networking.relay_client.requests.post') as mock_post:
            mock_post.side_effect = [failure_response, success_response]

            result = client.ping_relay()

        assert result == TEST_NO_REQUEST_RESPONSE

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            'http://primary-relay:5000/sink',
            'https://relay.cloudflare.workers.dev/api/v1/sink',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_sticky_failover_after_failure(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Once failover occurs, subsequent calls should start with the healthy relay."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.additional_servers': ['http://backup-relay:6000'],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        failure_response = MagicMock()
        failure_response.status_code = 503
        failure_response.text = "Service unavailable"

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = TEST_NO_REQUEST_RESPONSE

        second_success_response = MagicMock()
        second_success_response.status_code = 200
        second_success_response.json.return_value = TEST_NO_REQUEST_RESPONSE

        mock_post.side_effect = [
            failure_response,
            success_response,
            second_success_response,
        ]

        first_result = client.ping_relay()
        second_result = client.ping_relay()

        assert first_result == TEST_NO_REQUEST_RESPONSE
        assert second_result == TEST_NO_REQUEST_RESPONSE

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            'http://primary-relay:5000/sink',
            'http://backup-relay:6000/sink',
            'http://backup-relay:6000/sink',
        ]
        assert client.relay_url == 'http://backup-relay:6000'

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_rotates_between_successful_servers(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Successful sink calls should round-robin across configured relay targets."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.additional_servers': ['http://backup-relay:6000'],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        primary_response = MagicMock()
        primary_response.status_code = 200
        primary_response.json.return_value = TEST_NO_REQUEST_RESPONSE

        secondary_response = MagicMock()
        secondary_response.status_code = 200
        secondary_response.json.return_value = TEST_NO_REQUEST_RESPONSE

        mock_post.side_effect = [primary_response, secondary_response]

        first_result = client.ping_relay()
        second_result = client.ping_relay()

        assert first_result == TEST_NO_REQUEST_RESPONSE
        assert second_result == TEST_NO_REQUEST_RESPONSE

        requested_urls = [call.args[0] for call in mock_post.call_args_list]
        assert requested_urls == [
            'http://primary-relay:5000/sink',
            'http://backup-relay:6000/sink',
        ]
        assert client.relay_url == 'http://backup-relay:6000'

    def test_cluster_only_uses_remote_targets_first(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Cluster-only mode should skip the local relay host."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.additional_servers': ['https://cluster-relay:7443'],
            'relay.cluster_only': True,
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://primary-relay",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        assert client.relay_urls == ('https://cluster-relay:7443',)
        assert client.relay_url == 'https://cluster-relay:7443'

    def test_cluster_only_without_targets_raises(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Cluster-only mode should fail fast when no remote targets are configured."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.cluster_only': True,
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            with pytest.raises(ValueError):
                RelayClient(
                    base_url="http://primary-relay",
                    port=5000,
                    crypto_manager=mock_crypto_manager,
                    model_manager=mock_model_manager,
                )

    def test_cluster_only_rejects_localhost_primary_configuration(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Cluster-only mode should not silently accept localhost from config defaults."""

        config_values = {
            'relay.request_timeout': 20,
            'relay.cluster_only': True,
            'relay.server_url': 'http://localhost:5000',
            'relay.additional_servers': [],
            'relay.server_pool_secondary': [],
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            with pytest.raises(ValueError, match='At least one relay target must be provided'):
                RelayClient(
                    base_url="http://localhost",
                    port=5000,
                    crypto_manager=mock_crypto_manager,
                    model_manager=mock_model_manager,
                )

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_request_exception(self, mock_post, relay_client):
        """Test ping to relay with request exception."""
        # Setup mock to raise an exception
        mock_post.side_effect = requests.ConnectionError("Test connection error")

        # Call the method
        result = relay_client.ping_relay()

        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "Test connection error"
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_json_decode_error(self, mock_post, relay_client):
        """Test ping to relay with JSON decode error."""
        # Setup mock to return invalid JSON
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "{", 0)
        mock_post.return_value = mock_response

        # Call the method
        result = relay_client.ping_relay()

        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert "Invalid JSON" in result['error']
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_schema_validation_error(self, mock_post, relay_client):
        """Test ping to relay with schema validation error."""
        # Setup mock to return response that fails schema validation
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'invalid': 'response'}  # Missing required fields
        mock_post.return_value = mock_response

        # Call the method
        result = relay_client.ping_relay()

        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert "Invalid response format" in result['error']
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout

    @patch('utils.networking.relay_client.requests.post')
    def test_ping_relay_generic_exception(self, mock_post, relay_client):
        """Test ping to relay with generic exception."""
        # Setup mock to raise an exception
        mock_post.side_effect = Exception("Unexpected error")

        # Call the method
        result = relay_client.ping_relay()

        # Check the result
        assert 'error' in result
        assert 'next_ping_in_x_seconds' in result
        assert result['error'] == "Unexpected error"
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout


    @patch('utils.networking.relay_client.requests.post')
    def test_register_api_v1_compute_node_non_200_returns_error(self, mock_post, relay_client):
        response = MagicMock(status_code=503)
        response.headers = {'content-type': 'text/plain'}
        response.text = 'Service unavailable'
        response.json.side_effect = ValueError('not json')
        mock_post.return_value = response

        result = relay_client.register_api_v1_compute_node('http://relay-a.example')

        assert result['error'] == 'HTTP 503'
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
        assert result['http_status'] == 503
        assert result['relay_error_kind'] == 'http_status_no_json_body'
        assert result['relay_http_diagnostic']['path'] == '/api/v1/relay/servers/register'

    @patch('utils.networking.relay_client.requests.post')
    def test_register_api_v1_compute_node_403_html_logs_cloudflare_diagnostic(
        self, mock_post, relay_client, caplog
    ):
        relay_client._registration_token = 'super-secret-token'
        relay_client.crypto_manager.public_key_b64 = 'server-public-key-secret'
        response = MagicMock(status_code=403)
        response.headers = {
            'server': 'cloudflare',
            'cf-ray': '84abcd-SJC',
            'cf-cache-status': 'DYNAMIC',
            'content-type': 'text/html; charset=UTF-8',
        }
        response.text = (
            '<html>\n403 forbidden\r\nX-Relay-Server-Token: super-secret-token\x00 '
            'server_public_key=server-public-key-secret private_key=do-not-log</html>'
        )
        response.json.side_effect = ValueError('not json')
        mock_post.return_value = response

        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client.register_api_v1_compute_node('https://staging.token.place')

        diagnostic = result['relay_http_diagnostic']
        assert result['error'] == 'HTTP 403'
        assert result['relay_error_kind'] == 'cloudflare_pre_app_rejection'
        assert diagnostic['method'] == 'POST'
        assert diagnostic['path'] == '/api/v1/relay/servers/register'
        assert diagnostic['status_code'] == 403
        assert diagnostic['headers'] == {
            'server': 'cloudflare',
            'cf-ray': '84abcd-SJC',
            'cf-cache-status': 'DYNAMIC',
            'content-type': 'text/html; charset=UTF-8',
        }
        assert diagnostic['token_sent'] is True
        assert diagnostic['probable_pre_app_rejection'] is True
        assert '403 forbidden' in diagnostic['body_snippet']
        assert len(diagnostic['body_snippet']) <= relay_client_module._API_V1_BODY_SNIPPET_LIMIT + 3
        assert '\n' not in diagnostic['body_snippet']
        assert '\r' not in diagnostic['body_snippet']
        assert '\x00' not in diagnostic['body_snippet']
        http_log = next(
            record.getMessage()
            for record in caplog.records
            if 'api_v1.relay_http_error' in record.getMessage()
        )
        assert '\n' not in http_log
        assert '\r' not in http_log
        assert '\x00' not in http_log
        logs = caplog.text
        assert 'api_v1.relay_http_error' in logs
        assert 'api_v1.relay_pre_app_rejection' in logs
        assert 'cf-ray' in logs
        assert '84abcd-SJC' in logs
        for forbidden in (
            'super-secret-token',
            'server-public-key-secret',
            'do-not-log',
        ):
            assert forbidden not in logs
            assert forbidden not in json.dumps(result, sort_keys=True)

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_propagates_register_http_diagnostic(
        self, mock_post, relay_client
    ):
        relay_client._registration_token = 'super-secret-token'
        relay_client.crypto_manager.public_key_b64 = 'server-public-key-secret'
        response = MagicMock(status_code=403)
        response.headers = {
            'server': 'cloudflare',
            'cf-ray': '84abcd-SJC',
            'content-type': 'text/html; charset=UTF-8',
        }
        response.text = (
            '<html>403 forbidden X-Relay-Server-Token: super-secret-token '
            'server_public_key=server-public-key-secret</html>'
        )
        response.json.side_effect = ValueError('not json')
        mock_post.return_value = response

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['error'] == 'HTTP 403'
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
        assert result['http_status'] == 403
        assert result['relay_error_kind'] == 'cloudflare_pre_app_rejection'
        diagnostic = result['relay_http_diagnostic']
        assert diagnostic['path'] == '/api/v1/relay/servers/register'
        assert diagnostic['headers']['cf-ray'] == '84abcd-SJC'
        assert diagnostic['token_sent'] is True
        for forbidden in ('super-secret-token', 'server-public-key-secret'):
            assert forbidden not in json.dumps(result, sort_keys=True)

    @patch('utils.networking.relay_client.requests.post')
    def test_register_api_v1_compute_node_logs_control_plane_429(self, mock_post, relay_client, caplog):
        response = MagicMock(status_code=429)
        response.headers = {
            'content-type': 'application/json',
            'retry-after': '37',
        }
        response.json.return_value = {
            'error': {
                'code': 'rate_limit_exceeded',
                'message': 'Rate limit exceeded: 2 per 1 hour. Try again in 37 seconds.',
                'type': 'rate_limit_error',
            }
        }
        mock_post.return_value = response

        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client.register_api_v1_compute_node('https://staging.token.place')

        diagnostic = result['relay_http_diagnostic']
        assert result['error'] == 'HTTP 429'
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
        assert diagnostic['path'] == '/api/v1/relay/servers/register'
        assert diagnostic['route_class'] == 'compute_node_control_plane'
        assert diagnostic['retry_after'] == '37'
        assert diagnostic['headers']['retry-after'] == '37'
        assert 'relay_control_plane_rate_limited' in caplog.text
        assert 'retry_after=37' in caplog.text


    @patch('utils.networking.relay_client.requests.post')
    def test_register_api_v1_compute_node_401_json_logs_relay_error_safely(
        self, mock_post, relay_client, caplog
    ):
        relay_client._registration_token = 'super-secret-token'
        relay_client.crypto_manager.public_key_b64 = 'server-public-key-secret'
        response = MagicMock(status_code=401)
        response.headers = {
            'server': 'gunicorn',
            'content-type': 'application/json',
            'x-request-id': 'relay-request-123',
        }
        response.text = '{"error":"invalid relay registration token"}'
        response.json.return_value = {
            'error': 'invalid relay registration token',
            'token': 'super-secret-token',
            'server_public_key': 'server-public-key-secret',
        }
        mock_post.return_value = response

        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client.register_api_v1_compute_node('https://staging.token.place')

        assert result['error'] == 'HTTP 401'
        assert result['relay_error_kind'] == 'relay_json_error'
        assert result['relay_error'] == 'invalid relay registration token'
        diagnostic = result['relay_http_diagnostic']
        assert diagnostic['headers']['x-request-id'] == 'relay-request-123'
        assert diagnostic['token_sent'] is True
        assert diagnostic['probable_pre_app_rejection'] is False
        assert 'invalid relay registration token' in diagnostic['body_snippet']
        assert '"token":"[redacted]"' in diagnostic['body_snippet']
        logs = caplog.text
        assert 'api_v1.relay_http_error' in logs
        assert 'api_v1.relay_pre_app_rejection' not in logs
        for forbidden in ('super-secret-token', 'server-public-key-secret'):
            assert forbidden not in logs
            assert forbidden not in json.dumps(result, sort_keys=True)


    @patch('utils.networking.relay_client.requests.post')
    def test_register_api_v1_compute_node_redacts_nested_json_error_and_hyphen_keys(
        self, mock_post, relay_client, caplog
    ):
        relay_client._registration_token = 'super-secret-token'
        relay_client.crypto_manager.public_key_b64 = 'configured-public-key-secret'
        response = MagicMock(status_code=401)
        response.headers = {'server': 'gunicorn', 'content-type': 'application/json'}
        response.text = 'ignored because response.json returns a JSON object'
        response.json.return_value = {
            'error': {
                'message': 'bad super-secret-token configured-public-key-secret',
                'X-Relay-Server-Token': 'echoed-other-token',
                'private-key': 'private-key-secret',
                'server-public-key': 'server-public-key-secret',
            },
            'detail': {
                'token': 'super-secret-token',
                'message': 'bad super-secret-token configured-public-key-secret',
            },
        }
        mock_post.return_value = response

        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client.register_api_v1_compute_node('https://staging.token.place')

        bridge = _load_compute_node_bridge_module()
        summary = bridge._relay_response_summary(result)

        assert result['relay_error_kind'] == 'relay_json_error'
        assert result['relay_error'] == (
            '{"X-Relay-Server-Token":"[redacted]","message":"bad [redacted] [redacted]",'
            '"private-key":"[redacted]","server-public-key":"[redacted]"}'
        )
        body_snippet = result['relay_http_diagnostic']['body_snippet']
        assert '"X-Relay-Server-Token":"[redacted]"' in body_snippet
        assert '"private-key":"[redacted]"' in body_snippet
        assert '"server-public-key":"[redacted]"' in body_snippet
        assert '"token":"[redacted]"' in body_snippet
        rendered_result = json.dumps(result, sort_keys=True)
        for forbidden in (
            'super-secret-token',
            'configured-public-key-secret',
            'echoed-other-token',
            'server-public-key-secret',
            'private-key-secret',
        ):
            assert forbidden not in caplog.text
            assert forbidden not in rendered_result
            assert forbidden not in body_snippet
            assert forbidden not in result['relay_error']
            assert forbidden not in summary

    @patch('utils.networking.relay_client.requests.post')
    def test_register_api_v1_compute_node_redacts_json_error_known_secret_values(
        self, mock_post, relay_client, caplog
    ):
        relay_client._registration_token = 'super-secret-token'
        relay_client.crypto_manager.public_key_b64 = 'server-public-key-secret'
        response = MagicMock(status_code=401)
        response.headers = {'server': 'gunicorn', 'content-type': 'application/json'}
        response.text = (
            '{"error":"bad super-secret-token server-public-key-secret",'
            '"detail":{"message":"bad super-secret-token server-public-key-secret"}}'
        )
        response.json.return_value = {
            'error': 'bad super-secret-token server-public-key-secret',
            'detail': {
                'message': 'bad super-secret-token server-public-key-secret',
            },
        }
        mock_post.return_value = response

        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client.register_api_v1_compute_node('https://staging.token.place')

        bridge = _load_compute_node_bridge_module()
        summary = bridge._relay_response_summary(result)

        assert result['relay_error_kind'] == 'relay_json_error'
        assert result['relay_error'] == 'bad [redacted] [redacted]'
        assert (
            result['relay_http_diagnostic']['body_snippet']
            == '{"detail":{"message":"bad [redacted] [redacted]"},"error":"bad [redacted] [redacted]"}'
        )
        for rendered in (caplog.text, json.dumps(result, sort_keys=True), summary):
            assert 'super-secret-token' not in rendered
            assert 'server-public-key-secret' not in rendered

    def test_build_api_v1_url_avoids_double_api_v1_suffix(self):
        assert RelayClient._build_api_v1_url(
            "http://localhost:5000", "/relay/servers/register"
        ) == "http://localhost:5000/api/v1/relay/servers/register"
        assert RelayClient._build_api_v1_url(
            "https://relay.cloudflare.workers.dev/api/v1", "/relay/servers/register"
        ) == "https://relay.cloudflare.workers.dev/api/v1/relay/servers/register"

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_uses_api_v1_url_builder_for_prefixed_relay(self, mock_post):
        client = _standalone_relay_client()
        prefixed_url = 'http://localhost:5000/api/v1'
        client._relay_urls = [prefixed_url]
        client._api_v1_registered_relays.add(prefixed_url)
        client._api_v1_last_heartbeat_at[prefixed_url] = 1.0
        mock_post.return_value = MagicMock(status_code=200)

        assert client.unregister_from_relay() is True

        mock_post.assert_called_once_with(
            'http://localhost:5000/api/v1/relay/servers/unregister',
            json={'server_public_key': 'mock_public_key_b64'},
            timeout=15,
        )

    @pytest.mark.parametrize(
        "expected_wait, expected_timeout",
        [
            (9, 15.0),
            (10, 15.0),
            (15.5, 20.5),
            ("11", 16.0),
            ("bad", 15.0),
            (True, 15.0),
            (False, 15.0),
            (-1, 15.0),
            (float("nan"), 15.0),
            (float("inf"), 15.0),
        ],
    )
    def test_api_v1_poll_timeout_seconds_defensive(self, relay_client, expected_wait, expected_timeout):
        assert relay_client._api_v1_poll_timeout_seconds(expected_wait) == expected_timeout

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_uses_derived_poll_timeout(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 30}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_ok, poll_ok]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['next_ping_in_x_seconds'] == 12
        assert mock_post.call_args_list[1].kwargs['timeout'] == 37.5

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_falls_back_to_register_wait_without_poll_wait(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 12}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_ok, poll_ok]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['next_ping_in_x_seconds'] == 12
        assert mock_post.call_args_list[1].kwargs['timeout'] == 17.0

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_expected_poll_timeout_returns_no_work_without_reregister(
        self, mock_post, relay_client, monkeypatch
    ):
        clock_values = iter([1000.0, 1000.0, 1010.0, 1010.0])
        monkeypatch.setattr(
            relay_client_module.time,
            'monotonic',
            lambda: next(clock_values),
        )
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 10}
        mock_post.side_effect = [
            register_ok,
            requests.Timeout("Read timed out. (read timeout=15)"),
        ]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result == {
            'message': 'No requests available',
            'next_ping_in_x_seconds': 0,
            'poll_wait_seconds': 10,
        }
        assert 'http://localhost:5000' in relay_client._api_v1_registered_relays


    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_zero_poll_wait_timeout_is_failure(
        self, mock_post, relay_client, monkeypatch
    ):
        clock_values = iter([1000.0, 1000.0, 1000.1])
        monkeypatch.setattr(
            relay_client_module.time,
            'monotonic',
            lambda: next(clock_values),
        )
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 0}
        mock_post.side_effect = [
            register_ok,
            requests.Timeout("Read timed out. (read timeout=5)"),
        ]

        result = relay_client.poll_api_v1_encrypted_work()

        assert 'Read timed out' in result['error']
        assert result.get('message') != 'No requests available'
        assert result['next_ping_in_x_seconds'] == relay_client._request_timeout
        assert relay_client._api_v1_last_heartbeat_at == {}
        assert relay_client.api_v1_registration_fresh() is False


    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_no_work_hint_is_preserved(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available', 'next_ping_in_x_seconds': 0, 'poll_wait_seconds': 10}
        mock_post.side_effect = [register_ok, poll_ok]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['next_ping_in_x_seconds'] == 0
        assert result['poll_wait_seconds'] == 10

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_error_path_uses_register_backoff(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 14}
        poll_err = MagicMock(status_code=503)
        mock_post.side_effect = [register_ok, poll_err]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['error'] == 'HTTP 503'
        assert result['next_ping_in_x_seconds'] == 14

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_fails_over_and_uses_register_interval(self, mock_post, relay_client):
        relay_client._relay_urls = ('http://relay-a.example', 'http://relay-b.example')
        relay_client._active_relay_index = 0

        register_fail = MagicMock(status_code=500)
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 9}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_fail, register_ok, poll_ok]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['next_ping_in_x_seconds'] == 9
        assert relay_client._active_relay_index == 1
        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://relay-a.example/api/v1/relay/servers/register',
            'http://relay-b.example/api/v1/relay/servers/register',
            'http://relay-b.example/api/v1/relay/servers/poll',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_register_timeout_does_not_return_no_work(self, mock_post, relay_client):
        relay_client._relay_urls = ('http://relay-a.example', 'http://relay-b.example')
        relay_client._active_relay_index = 0

        register_timeout = requests.Timeout("Read timed out. (read timeout=15)")
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_timeout, register_ok, poll_ok]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['message'] == 'No requests available'
        assert result['next_ping_in_x_seconds'] == 9
        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://relay-a.example/api/v1/relay/servers/register',
            'http://relay-b.example/api/v1/relay/servers/register',
            'http://relay-b.example/api/v1/relay/servers/poll',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_poll_timeout_fails_over_to_backup(
        self, mock_post, relay_client
    ):
        relay_client._relay_urls = ('http://relay-a.example', 'http://relay-b.example')
        relay_client._active_relay_index = 0

        register_a = MagicMock(status_code=200)
        register_a.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        register_b = MagicMock(status_code=200)
        register_b.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [
            register_a,
            requests.Timeout("Read timed out. (read timeout=15)"),
            register_b,
            poll_ok,
        ]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['message'] == 'No requests available'
        assert result['next_ping_in_x_seconds'] == 12
        assert 'http://relay-a.example' not in relay_client._api_v1_registered_relays
        assert relay_client._active_relay_index == 1
        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://relay-a.example/api/v1/relay/servers/register',
            'http://relay-a.example/api/v1/relay/servers/poll',
            'http://relay-b.example/api/v1/relay/servers/register',
            'http://relay-b.example/api/v1/relay/servers/poll',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_does_not_register_every_poll(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_ok, poll_ok, poll_ok]

        relay_client.poll_api_v1_encrypted_work()
        relay_client.poll_api_v1_encrypted_work()

        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
            'http://localhost:5000/api/v1/relay/servers/poll',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_reregisters_after_unknown_node(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        poll_404 = MagicMock(status_code=404)
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_ok, poll_404, register_ok, poll_ok]

        first = relay_client.poll_api_v1_encrypted_work()

        assert first['error'] == 'HTTP 404'
        assert first['next_ping_in_x_seconds'] == 0
        assert first['relay_error_kind'] == 'http_status_no_json_body'
        assert relay_client.api_v1_registration_fresh('http://localhost:5000') is False
        assert 'http://localhost:5000' not in relay_client._api_v1_registered_relays
        assert 'http://localhost:5000' not in relay_client._api_v1_last_heartbeat_at
        assert 'http://localhost:5000' not in relay_client._api_v1_relay_wait_hints

        second = relay_client.poll_api_v1_encrypted_work()

        assert second['message'] == 'No requests available'
        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
        ]


    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_reregisters_before_cached_lease_expires(
        self, mock_post, relay_client, monkeypatch
    ):
        clock = {"now": 1000.0}
        monkeypatch.setattr(relay_client_module.time, "monotonic", lambda: clock["now"])
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 30, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available', 'next_ping_in_x_seconds': 0, 'poll_wait_seconds': 10}
        mock_post.side_effect = [register_ok, poll_ok, register_ok, poll_ok]

        first = relay_client.poll_api_v1_encrypted_work()
        assert first['message'] == 'No requests available'
        assert relay_client.api_v1_registration_fresh('http://localhost:5000') is True

        clock["now"] += 25.0
        second = relay_client.poll_api_v1_encrypted_work()

        assert second['message'] == 'No requests available'
        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_api_v1_registration_fresh_expires_after_lease_window(
        self, mock_post, relay_client, monkeypatch
    ):
        clock = {"now": 2000.0}
        monkeypatch.setattr(relay_client_module.time, "monotonic", lambda: clock["now"])
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 30, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available', 'next_ping_in_x_seconds': 0, 'poll_wait_seconds': 10}
        mock_post.side_effect = [register_ok, poll_ok]

        relay_client.poll_api_v1_encrypted_work()
        assert relay_client.api_v1_registration_fresh('http://localhost:5000') is True

        clock["now"] += 31.0
        assert relay_client.api_v1_registration_fresh('http://localhost:5000') is False

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_normalises_string_lease_hint(
        self, mock_post, relay_client
    ):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {
            'message': 'No requests available',
            'next_ping_in_x_seconds': '30',
            'poll_wait_seconds': '10',
        }
        mock_post.side_effect = [register_ok, poll_ok]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['next_ping_in_x_seconds'] == 30.0
        hints = relay_client._api_v1_relay_wait_hints['http://localhost:5000']
        assert hints['next_ping_in_x_seconds'] == 30.0
        assert hints['poll_wait_seconds'] == 10.0

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_cached_poll_wait_reused(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 60}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_ok, poll_ok, poll_ok]

        relay_client.poll_api_v1_encrypted_work()
        relay_client.poll_api_v1_encrypted_work()

        second_poll_call = mock_post.call_args_list[2]
        assert second_poll_call.args[0] == 'http://localhost:5000/api/v1/relay/servers/poll'
        assert second_poll_call.kwargs['timeout'] == relay_client._api_v1_poll_timeout_seconds(60)

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_reregisters_when_public_key_changes(self, mock_post, relay_client):
        register_first = MagicMock(status_code=200)
        register_first.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        register_second = MagicMock(status_code=200)
        register_second.json.return_value = {'next_ping_in_x_seconds': 9, 'poll_wait_seconds': 10}
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {'message': 'No requests available'}
        mock_post.side_effect = [register_first, poll_ok, register_second, poll_ok]

        relay_client.poll_api_v1_encrypted_work()
        relay_client.crypto_manager.public_key_b64 = 'new-server-public-key'
        relay_client.poll_api_v1_encrypted_work()

        called_urls = [call.args[0] for call in mock_post.call_args_list]
        assert called_urls == [
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
            'http://localhost:5000/api/v1/relay/servers/register',
            'http://localhost:5000/api/v1/relay/servers/poll',
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_non_dict_payload_returns_bounded_error(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 7}
        poll_ok_non_dict = MagicMock(status_code=200)
        poll_ok_non_dict.json.return_value = ['unexpected']
        mock_post.side_effect = [register_ok, poll_ok_non_dict]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result == {
            'error': 'Invalid response format: expected object payload',
            'next_ping_in_x_seconds': 7,
        }

    @patch('utils.networking.relay_client.requests.post')
    def test_poll_api_v1_encrypted_work_propagates_register_interval_on_poll_error(self, mock_post, relay_client):
        register_ok = MagicMock(status_code=200)
        register_ok.json.return_value = {'next_ping_in_x_seconds': 11}
        poll_fail = MagicMock(status_code=429)
        mock_post.side_effect = [register_ok, poll_fail]

        result = relay_client.poll_api_v1_encrypted_work()

        assert result['error'] == 'HTTP 429'
        assert result['next_ping_in_x_seconds'] == 11
        assert result['relay_error_kind'] == 'http_status_no_json_body'

    def test_process_client_request_missing_fields(self, relay_client):
        """Test processing a client request with missing fields."""
        # Setup request data with missing fields
        request_data = {
            'client_public_key': 'client_key',
            # Missing 'chat_history'
            'cipherkey': 'key',
            'iv': 'iv'
        }

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is False

    def test_process_client_request_decryption_failure(self, relay_client, mock_crypto_manager):
        """Test processing a client request with decryption failure."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()

        # Mock decryption failure
        mock_crypto_manager.decrypt_message.return_value = None

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is False

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_success(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager, mock_http_response):
        """Test successful processing of a client request."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()

        # Set up HTTP response
        mock_http_response.status_code = 200
        mock_post.return_value = mock_http_response

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is True

        # Verify mock calls
        mock_crypto_manager.decrypt_message.assert_called_once_with(request_data)
        mock_model_manager.llama_cpp_get_response.assert_called_once_with(
            mock_crypto_manager.decrypt_message.return_value
        )

        # Check the encryption and post to /source
        mock_crypto_manager.encrypt_message.assert_called_once_with(
            mock_model_manager.llama_cpp_get_response.return_value,
            base64.b64decode('Y2xpZW50X2tleV9iNjQ=')
        )

        expected_payload = {
            'client_public_key': 'Y2xpZW50X2tleV9iNjQ=',
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv'
        }
        mock_post.assert_called_once_with(
            'http://localhost:5000/source',
            json=expected_payload,
            timeout=relay_client._request_timeout
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_e2ee_success(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
        monkeypatch,
    ):
        """API v1 relay envelopes use desktop runtime model without importing api.v1."""

        original_import = builtins.__import__

        def fail_api_v1_models_import(name, *args, **kwargs):
            if name == 'api.v1.models':
                raise ModuleNotFoundError("No module named 'api'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, '__import__', fail_api_v1_models_import)
        request_data = TEST_VALID_RESPONSE.copy()
        decrypted_payload = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-123",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        mock_crypto_manager.decrypt_message.return_value = decrypted_payload
        mock_crypto_manager.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv'
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        result = relay_client.process_client_request(request_data)

        assert result is True
        mock_model_manager.llama_cpp_get_response.assert_not_called()
        mock_model_manager.runtime.create_chat_completion.assert_called_once()
        assert mock_model_manager.runtime.create_chat_completion.call_args.kwargs[
            "messages"
        ] == [{"role": "user", "content": "Hello"}]
        mock_crypto_manager.encrypt_message.assert_called_with(
            {
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "request_id": "req-123",
                "client_public_key": request_data["client_public_key"],
                "api_v1_response": {
                    "message": {
                        "role": "assistant",
                        "content": "The capital of France is Paris.",
                    },
                },
            },
            base64.b64decode(request_data["client_public_key"], validate=True),
        )
        mock_post.assert_called_once_with(
            'http://localhost:5000/api/v1/relay/responses',
            json={
                'client_public_key': request_data["client_public_key"],
                'request_id': 'req-123',
                'protocol': 'tokenplace_api_v1_relay_e2ee',
                'version': 1,
                'chat_history': 'encrypted_chat_history',
                'cipherkey': 'encrypted_key',
                'iv': 'encrypted_iv',
            },
            timeout=relay_client._request_timeout,
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_no_options_does_not_depend_on_api_imports(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
        monkeypatch,
    ):
        """Packaged desktop API v1 success must not require server-side api imports."""

        def fail_api_import(name, *args, **kwargs):
            if name == "api" or name.startswith("api."):
                raise ModuleNotFoundError("No module named 'api'")
            raise AssertionError(f"unexpected import_module call: {name}")

        monkeypatch.setattr(
            "utils.networking.relay_client.importlib.import_module",
            fail_api_import,
        )
        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-no-api-import",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "gpt-5-chat-latest",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        mock_model_manager.llama_cpp_get_response.assert_not_called()
        mock_model_manager.runtime.create_chat_completion.assert_called_once()
        assert mock_model_manager.runtime.create_chat_completion.call_args.kwargs[
            "messages"
        ] == [{"role": "user", "content": "Hello"}]
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["message"]["content"] == (
            "The capital of France is Paris."
        )
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == (
            "http://localhost:5000/api/v1/relay/responses"
        )

    @pytest.mark.parametrize("model_id", ["gpt-3.5-turbo", "gpt-5-chat-latest"])
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_local_aliases_use_packaged_llama_runtime(
        self,
        mock_post,
        model_id,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
        monkeypatch,
    ):
        """Local alias map preserves API v1 semantics without importing api.v1.models."""

        monkeypatch.setattr(
            "utils.networking.relay_client.importlib.import_module",
            MagicMock(side_effect=ModuleNotFoundError("No module named 'api'")),
        )
        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": f"req-alias-{model_id}",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": model_id,
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        mock_model_manager.llama_cpp_get_response.assert_not_called()
        mock_model_manager.runtime.create_chat_completion.assert_called_once()
        assert mock_model_manager.runtime.create_chat_completion.call_args.kwargs[
            "messages"
        ] == [{"role": "user", "content": "Hello"}]
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert "error" not in encrypted_envelope["api_v1_response"]

    def test_api_v1_assistant_message_accepts_non_empty_content(self):
        """Assistant messages with non-empty content remain valid API v1 output."""

        message = {"role": "assistant", "content": "Hello"}

        assert RelayClient._valid_api_v1_assistant_message(message) == message

    def test_api_v1_assistant_message_accepts_tool_calls_without_content(self):
        """Assistant tool-call messages remain valid even when content is empty."""

        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        }

        assert RelayClient._valid_api_v1_assistant_message(message) == message

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_alignment_adapter_is_rejected(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """The removed API v1 alignment adapter must not run on the local runtime."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/tmp/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-adapter",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct:alignment",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        mock_model_manager.llama_cpp_get_response.assert_not_called()
        mock_model_manager.runtime.create_chat_completion.assert_not_called()
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == "compute_node_model_unsupported"

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_normalises_multipart_content_blocks(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """OpenAI-style text content blocks are flattened before llama.cpp inference."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-multipart",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "First segment"},
                            {"type": "input_text", "text": "Second segment"},
                        ],
                    }
                ],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        mock_model_manager.llama_cpp_get_response.assert_not_called()
        assert mock_model_manager.runtime.create_chat_completion.call_args.kwargs[
            "messages"
        ] == [
            {
                "role": "user",
                "content": "First segment\n\nSecond segment",
            }
        ]

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_rejects_image_content_blocks(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """API v1 relay chat is text-only and must fail closed for images."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-image-content",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this."},
                            {"type": "input_image", "image": {"b64_json": "ZmFrZQ=="}},
                        ],
                    }
                ],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_invalid_request"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_e2ee_posts_response_to_polling_relay(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """API v1 responses should be submitted to the relay that supplied the work."""

        request_data = TEST_VALID_RESPONSE.copy()
        relay_client._last_api_v1_work_relay_url = 'https://relay-that-polled.example'
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-polled-relay",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        assert mock_post.call_args.args[0] == (
            'https://relay-that-polled.example/api/v1/relay/responses'
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_e2ee_rejects_mismatched_bound_key(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """API v1 relay envelopes with mismatched encrypted key bindings are rejected."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-123",
            "client_public_key": "different-client-key",
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {"temperature": 0.2},
            },
        }

        result = relay_client.process_client_request(request_data)

        assert result is False
        mock_model_manager.llama_cpp_get_response.assert_not_called()
        mock_post.assert_not_called()


    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_missing_runtime_posts_internal_error(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Missing desktop runtime init becomes a stable encrypted internal error."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-runtime-missing",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        mock_model_manager.get_llm_instance.return_value = None
        mock_crypto_manager.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv',
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        typed_result = relay_client.process_client_request_result(request_data)
        assert bool(typed_result) is True
        assert typed_result.submitted is True
        assert typed_result.inference_succeeded is False
        assert typed_result.safe_error_code == "compute_node_internal_error"
        assert typed_result.runtime_healthy is False
        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_internal_error"
        )
        assert encrypted_envelope["api_v1_response"]["error"]["message"] == (
            "Desktop runtime inference failed"
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_invalid_runtime_output_posts_encrypted_error(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Invalid desktop runtime output becomes an encrypted API v1 error response."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-invalid-output",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "SENTINEL-PLAINTEXT"}],
                "options": {},
            },
        }
        mock_model_manager.runtime.create_chat_completion.return_value = {
            "choices": [{"message": {"role": "assistant", "content": ""}}]
        }
        mock_crypto_manager.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv',
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_invalid_model_output"
        )
        relay_visible_payload = mock_post.call_args.kwargs["json"]
        assert "SENTINEL-PLAINTEXT" not in json.dumps(relay_visible_payload)
        assert relay_visible_payload["request_id"] == "req-invalid-output"
        assert relay_visible_payload["protocol"] == "tokenplace_api_v1_relay_e2ee"
        assert relay_visible_payload["version"] == 1

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_unsupported_model_posts_encrypted_error(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Unsupported requested models produce encrypted API v1 errors, not timeouts."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/tmp/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-unsupported-model",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_model_unsupported"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()
        assert mock_post.call_args.kwargs["json"]["request_id"] == (
            "req-unsupported-model"
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_rejects_wrong_llama_family_model(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """A configured Llama runtime must not satisfy arbitrary Llama-family IDs."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/tmp/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-wrong-llama",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-70b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_model_unsupported"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_uses_manager_model_support_hook(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Runtime-specific API v1 model support hooks are the source of truth."""

        class _Manager:
            use_mock_llm = False

            def supports_api_v1_model(self, model_id):
                self.checked_model_ids.append(model_id)
                return model_id == "custom-local-api-v1-model"

            def __init__(self):
                self.checked_model_ids = []
                self.runtime = MagicMock()
                self.runtime.create_chat_completion.return_value = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "custom ok",
                            }
                        }
                    ]
                }

            def get_llm_instance(self):
                return self.runtime

        request_data = TEST_VALID_RESPONSE.copy()
        manager = _Manager()
        relay_client.model_manager = manager
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-hook-model",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "custom-local-api-v1-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        assert manager.checked_model_ids == ["custom-local-api-v1-model"]
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["message"]["content"] == "custom ok"

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_invalid_role_posts_invalid_request(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """The desktop relay path must reject roles that API v1 rejects."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-invalid-role",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "developer", "content": "Do not accept me"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_invalid_request"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_invalid_content_posts_invalid_request(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Invalid content types should fail before reaching llama.cpp."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-invalid-content",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": None}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_invalid_request"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_rejects_options_without_direct_runtime(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Supported options are not silently dropped when only chat-history APIs exist."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.get_llm_instance = None
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-options-no-direct-runtime",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {"temperature": 0.2},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_model_unsupported"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_stream_false_without_direct_runtime_fails_closed(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Explicit stream:false follows omitted-stream option routing."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.get_llm_instance = None
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response
        observed_error_codes = []

        for request_id, options in (
            ("req-stream-omitted-no-direct-runtime", {}),
            ("req-stream-false-no-direct-runtime", {"stream": False}),
        ):
            mock_crypto_manager.reset_mock()
            mock_model_manager.llama_cpp_get_response.reset_mock()
            mock_crypto_manager.encrypt_message.return_value = {
                'chat_history': 'encrypted_chat_history',
                'cipherkey': 'encrypted_key',
                'iv': 'encrypted_iv'
            }
            mock_crypto_manager.decrypt_message.return_value = {
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "request_id": request_id,
                "client_public_key": request_data["client_public_key"],
                "api_v1_request": {
                    "model": "llama-3-8b-instruct",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "options": options,
                },
            }

            assert relay_client.process_client_request(request_data) is True

            encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
            observed_error_codes.append(
                encrypted_envelope["api_v1_response"]["error"]["code"]
            )
            mock_model_manager.llama_cpp_get_response.assert_not_called()

        assert observed_error_codes == [
            "compute_node_model_unsupported",
            "compute_node_model_unsupported",
        ]
        assert "compute_node_options_unsupported" not in observed_error_codes

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_rejects_streaming_option(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """API v1 relay inference must fail closed instead of streaming."""

        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-streaming-option",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {"stream": True},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_options_unsupported"
        )
        mock_model_manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_rejection_skips_runtime_then_valid_succeeds(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """A rejected API v1 request must not poison the next valid desktop request."""

        class _Runtime:
            def __init__(self):
                self.create_chat_completion = MagicMock(
                    return_value={
                        "choices": [
                            {"message": {"role": "assistant", "content": "Recovered"}}
                        ]
                    }
                )

        class _Manager:
            use_mock_llm = False
            api_model_id = "llama-3-8b-instruct"

            def __init__(self):
                self.runtime = _Runtime()
                self.get_llm_instance = MagicMock(return_value=self.runtime)
                self.llama_cpp_get_response = MagicMock(
                    side_effect=AssertionError("legacy runtime must not be used")
                )

        request_data = TEST_VALID_RESPONSE.copy()
        manager = _Manager()
        relay_client.model_manager = manager
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        rejected_payload = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-rejected-process",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "SENTINEL PROMPT"}],
                "options": {"response_format": {"type": "text"}},
            },
        }
        valid_payload = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-valid-process",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {"max_tokens": 8},
            },
        }

        mock_crypto_manager.decrypt_message.return_value = rejected_payload
        assert relay_client.process_client_request(request_data) is True
        rejected_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        rejected_error = rejected_envelope["api_v1_response"]["error"]
        assert rejected_error["code"] == "compute_node_options_unsupported"
        assert "SENTINEL" not in json.dumps(rejected_error)
        assert "response_format" in rejected_error["message"]
        manager.get_llm_instance.assert_not_called()
        manager.runtime.create_chat_completion.assert_not_called()
        manager.llama_cpp_get_response.assert_not_called()

        mock_crypto_manager.decrypt_message.return_value = valid_payload
        assert relay_client.process_client_request(request_data) is True
        valid_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert valid_envelope["api_v1_response"]["message"]["content"] == "Recovered"
        manager.get_llm_instance.assert_called_once()
        manager.runtime.create_chat_completion.assert_called_once()
        manager.llama_cpp_get_response.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_forwards_supported_generation_options(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Supported API v1 generation options must reach direct runtime completion."""

        class _Runtime:
            def __init__(self):
                self.calls = []

            def create_chat_completion(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Options forwarded",
                            }
                        }
                    ]
                }

        class _Manager:
            use_mock_llm = False
            api_model_id = "llama-3-8b-instruct"

            def __init__(self):
                self.runtime = _Runtime()

            def llama_cpp_get_response(self, _messages):
                raise AssertionError("options must not be silently dropped")

            def get_llm_instance(self):
                return self.runtime

        request_data = TEST_VALID_RESPONSE.copy()
        manager = _Manager()
        relay_client.model_manager = manager
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-supported-options",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {
                    "max_tokens": 32,
                    "temperature": 0.25,
                    "top_p": 0.8,
                    "stop": ["END"],
                },
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        assert len(manager.runtime.calls) == 1
        completion_kwargs = manager.runtime.calls[0]
        assert completion_kwargs["messages"] == [{"role": "user", "content": "Hello"}]
        assert completion_kwargs["max_tokens"] == 32
        assert completion_kwargs["temperature"] == 0.25
        assert completion_kwargs["top_p"] == 0.8
        assert completion_kwargs["stop"] == ["END"]
        assert completion_kwargs["stream"] is False
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["message"]["content"] == (
            "Options forwarded"
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_forwards_supported_options_with_defaults(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Supported API v1 options should pass through with manager defaults."""

        class _Config:
            def get(self, key, default):
                return {
                    "model.max_tokens": 64,
                    "model.temperature": 0.7,
                    "model.top_p": 0.9,
                    "model.stop_tokens": ["</s>"],
                }.get(key, default)

        class _Runtime:
            def __init__(self):
                self.calls = []

            def create_chat_completion(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Tool-aware response",
                            }
                        }
                    ]
                }

        class _Manager:
            use_mock_llm = False
            api_model_id = None
            model_id = None
            file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
            model_path = "/tmp/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"

            def __init__(self):
                self.config = _Config()
                self.runtime = _Runtime()

            def llama_cpp_get_response(self, _messages):
                raise AssertionError("direct runtime should receive supported options")

            def get_llm_instance(self):
                return self.runtime

        request_data = TEST_VALID_RESPONSE.copy()
        manager = _Manager()
        relay_client.model_manager = manager
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-openai-options",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {
                    "frequency_penalty": 0.1,
                    "presence_penalty": 0.2,
                    "seed": 7,
                    "temperature": 0.2,
                },
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        assert len(manager.runtime.calls) == 1
        completion_kwargs = manager.runtime.calls[0]
        assert completion_kwargs["messages"] == [{"role": "user", "content": "Hello"}]
        assert completion_kwargs["max_tokens"] == 64
        assert completion_kwargs["top_p"] == 0.9
        assert completion_kwargs["stop"] == ["</s>"]
        assert completion_kwargs["stream"] is False
        assert completion_kwargs["temperature"] == 0.2
        assert completion_kwargs["frequency_penalty"] == 0.1
        assert completion_kwargs["presence_penalty"] == 0.2
        assert completion_kwargs["seed"] == 7
        assert "response_format" not in completion_kwargs
        assert "tools" not in completion_kwargs
        assert "tool_choice" not in completion_kwargs
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["message"]["content"] == (
            "Tool-aware response"
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_explicit_false_stream_stays_non_streaming(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """API v1 direct runtime calls must stay non-streaming when stream is false."""

        class _Runtime:
            def __init__(self):
                self.calls = []

            def create_chat_completion(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Non-streaming response",
                            }
                        }
                    ]
                }

        class _Manager:
            use_mock_llm = False
            api_model_id = "llama-3-8b-instruct"

            def __init__(self):
                self.runtime = _Runtime()

            def get_llm_instance(self):
                return self.runtime

        request_data = TEST_VALID_RESPONSE.copy()
        manager = _Manager()
        relay_client.model_manager = manager
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-non-streaming-options",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {"temperature": 0.2, "stream": False},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        assert len(manager.runtime.calls) == 1
        completion_kwargs = manager.runtime.calls[0]
        assert completion_kwargs["stream"] is False
        assert completion_kwargs["temperature"] == 0.2
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["message"]["content"] == (
            "Non-streaming response"
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_rejects_mismatched_bound_client_key(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Requests are rejected when encrypted payload key binding mismatches relay key."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "chat_history": [{"role": "user", "content": "hello"}],
            "client_public_key": "different-client-key",
        }

        result = relay_client.process_client_request(request_data)

        assert result is False
        mock_post.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_accepts_matching_bound_client_key(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
        mock_http_response,
    ):
        """Requests with matching encrypted key binding are processed normally."""
        request_data = TEST_VALID_RESPONSE.copy()
        chat_history = [{"role": "user", "content": "hello"}]
        mock_crypto_manager.decrypt_message.return_value = {
            "chat_history": chat_history,
            "client_public_key": request_data["client_public_key"],
        }
        mock_http_response.status_code = 200
        mock_post.return_value = mock_http_response

        result = relay_client.process_client_request(request_data)

        assert result is True
        mock_model_manager.llama_cpp_get_response.assert_called_once_with(chat_history)

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_rejects_bound_payload_missing_chat_history(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Bound payloads without chat_history are rejected."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "client_public_key": request_data["client_public_key"],
        }

        result = relay_client.process_client_request(request_data)

        assert result is False
        mock_post.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_rejects_invalid_chat_history_shape(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Decrypted payloads with invalid chat message shape are rejected."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "chat_history": [{"role": "user", "content": 123}],
        }

        result = relay_client.process_client_request(request_data)

        assert result is False
        mock_post.assert_not_called()


    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_streaming_posts_to_stream_source(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
        mock_http_response,
    ):
        """Streaming relay requests should publish chunks to /stream/source."""
        request_data = TEST_VALID_RESPONSE.copy()
        request_data['stream'] = True
        request_data['stream_session_id'] = 'session-123'

        mock_http_response.status_code = 200
        mock_http_response.text = 'ok'
        mock_post.return_value = mock_http_response

        result = relay_client.process_client_request(request_data)

        assert result is True
        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.args[0] == 'http://localhost:5000/stream/source'
        assert call.kwargs['json']['session_id'] == 'session-123'
        assert call.kwargs['json']['final'] is True

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_streaming_uses_registration_token_header(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Streaming posts should include the relay registration token when configured."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.server_registration_token': 'alpha-token',
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url='http://localhost',
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = 'OK'
        mock_post.return_value = mock_response

        payload = TEST_VALID_RESPONSE.copy()
        payload['stream'] = True
        payload['stream_session_id'] = 'session-123'
        result = client.process_client_request(payload)

        assert result is True
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs['headers'] == {'X-Relay-Server-Token': 'alpha-token'}

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_uses_registration_token(
        self,
        mock_post,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Registration tokens should be propagated to the source endpoint."""

        config_values = {
            'relay.request_timeout': 15,
            'relay.server_registration_token': 'alpha-token',
        }

        with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
            mock_config = MagicMock()
            mock_config.is_production = False
            mock_config.get.side_effect = lambda key, default=None: config_values.get(key, default)
            mock_get_config.return_value = mock_config

            client = RelayClient(
                base_url="http://localhost",
                port=5000,
                crypto_manager=mock_crypto_manager,
                model_manager=mock_model_manager,
            )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_post.return_value = mock_response

        result = client.process_client_request(TEST_VALID_RESPONSE.copy())

        assert result is True
        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.kwargs['headers'] == {'X-Relay-Server-Token': 'alpha-token'}

    @patch('utils.networking.relay_client._validate_with_fallback', side_effect=ValueError("bad"))
    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_invalid_payload(self, mock_post, mock_validate, relay_client):
        """Handle schema validation error for outgoing payload."""
        request_data = TEST_VALID_RESPONSE.copy()
        result = relay_client.process_client_request(request_data)
        assert result is False
        mock_validate.assert_called()
        mock_post.assert_not_called()

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_source_error(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager, mock_http_response):
        """Test processing a client request with error from /source endpoint."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()

        # Mock error response from /source
        mock_http_response.status_code = 500
        mock_http_response.text = "Internal server error"
        mock_post.return_value = mock_http_response

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is False

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_empty_response(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager, mock_http_response):
        """Test processing a client request with empty response from /source."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()

        # Mock empty response from /source
        mock_http_response.status_code = 200
        mock_http_response.text = ""
        mock_post.return_value = mock_http_response

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is False

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_connection_error(self, mock_post, relay_client, mock_crypto_manager, mock_model_manager):
        """Test processing a client request with connection error."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()

        # Mock to raise a connection error
        mock_post.side_effect = requests.ConnectionError("Test connection error")

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is False

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_exception(self, mock_post, relay_client, mock_crypto_manager):
        """Test processing a client request with an exception."""
        # Setup
        request_data = TEST_VALID_RESPONSE.copy()

        # Mock to raise an exception
        mock_crypto_manager.decrypt_message.side_effect = Exception("Test exception")

        # Call the method
        result = relay_client.process_client_request(request_data)

        # Check the result
        assert result is False

        # Verify post was not called
        mock_post.assert_not_called()

    @pytest.mark.parametrize(
        'bad_wait_seconds',
        ['missing', None, 'soon', True, -1, math.nan, math.inf],
    )
    @patch('utils.networking.relay_client.RelayClient.poll_api_v1_encrypted_work')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_api_v1_encrypted_work_continuously_coerces_invalid_wait_and_keeps_polling(
        self,
        mock_sleep,
        mock_process,
        mock_poll,
        relay_client,
        bad_wait_seconds,
    ):
        """Malformed API v1 relay wait values should not stop future polling."""
        invalid_response = {'protocol': 'tokenplace_api_v1_relay_e2ee'}
        if bad_wait_seconds != 'missing':
            invalid_response['next_ping_in_x_seconds'] = bad_wait_seconds
        next_response = {
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'next_ping_in_x_seconds': 0.25,
        }
        def poll_then_stop():
            if mock_poll.call_count == 1:
                return invalid_response
            relay_client.stop()
            return next_response

        mock_poll.side_effect = poll_then_stop

        relay_client.poll_api_v1_encrypted_work_continuously()

        assert mock_poll.call_count == 2
        assert [call.args[0] for call in mock_process.call_args_list] == [
            invalid_response,
            next_response,
        ]
        assert [call.args[0] for call in mock_sleep.call_args_list] == [
            relay_client._request_timeout,
            0.25,
        ]
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.poll_api_v1_encrypted_work')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_api_v1_encrypted_work_continuously_survives_poll_exception(
        self,
        mock_sleep,
        mock_process,
        mock_poll,
        relay_client,
    ):
        """Unexpected API v1 poll errors should sleep and keep the daemon alive."""
        next_response = {
            'protocol': 'tokenplace_api_v1_relay_e2ee',
            'next_ping_in_x_seconds': 0.5,
        }
        mock_poll.side_effect = [RuntimeError('temporary relay failure'), next_response]

        def stop_after_second_sleep(seconds):
            if mock_sleep.call_count >= 2:
                relay_client.stop()
            return None

        mock_sleep.side_effect = stop_after_second_sleep

        relay_client.start()
        relay_client.poll_api_v1_encrypted_work_continuously()

        assert mock_poll.call_count == 2
        mock_process.assert_called_once_with(next_response)
        assert [call.args[0] for call in mock_sleep.call_args_list] == [
            relay_client._request_timeout,
            0.5,
        ]
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client._max_poll_failures_before_stop', return_value=2)
    @patch('utils.networking.relay_client.RelayClient.poll_api_v1_encrypted_work', return_value=False)
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_api_v1_encrypted_work_continuously_stops_after_invalid_responses(
        self,
        mock_sleep,
        mock_poll,
        _mock_max_failures,
        relay_client,
    ):
        relay_client.start()
        relay_client.poll_api_v1_encrypted_work_continuously()

        assert mock_poll.call_count == 2
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(relay_client._request_timeout)
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client._max_poll_failures_before_stop', return_value=2)
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.RelayClient.poll_api_v1_encrypted_work')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_api_v1_encrypted_work_continuously_stops_after_repeated_error_responses(
        self,
        mock_sleep,
        mock_poll,
        mock_process,
        _mock_max_failures,
        relay_client,
    ):
        mock_poll.side_effect = [
            {"error": "HTTP 503", "next_ping_in_x_seconds": 0},
            {"error": "HTTP 503", "next_ping_in_x_seconds": 0},
        ]

        relay_client.start()
        relay_client.poll_api_v1_encrypted_work_continuously()

        assert mock_poll.call_count == 2
        mock_process.assert_not_called()
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(0.0)
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_client_request(self, mock_sleep, mock_process, mock_ping, relay_client):
        """Test the continuous polling with a client request."""
        # Setup to return a client request on first call
        mock_ping.side_effect = [TEST_VALID_RESPONSE]

        # Start polling
        relay_client.start()

        # Set up a callback that will stop polling after processing
        def stop_after_processing(*args, **kwargs):
            relay_client.stop()
            return True

        mock_process.side_effect = stop_after_processing

        # Call the method
        relay_client.poll_relay_continuously()

        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_process.assert_called_once_with(TEST_VALID_RESPONSE)
        mock_sleep.assert_called_once_with(5)  # Direct check of sleep call

        # Verify that polling was stopped
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.RelayClient.process_client_request')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_no_client_request(self, mock_sleep, mock_process, mock_ping, relay_client):
        """Test the continuous polling without a client request."""
        # Setup mock ping to return response without client request
        mock_ping.side_effect = [TEST_NO_REQUEST_RESPONSE]

        # Start polling
        relay_client.start()

        # Set up a callback to stop after sleep
        def stop_after_sleep(seconds):
            relay_client.stop()
            return None

        mock_sleep.side_effect = stop_after_sleep

        # Call the method
        relay_client.poll_relay_continuously()

        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_process.assert_not_called()
        mock_sleep.assert_called_once_with(5)  # Direct check of sleep call

        # Verify that polling was stopped
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_error(self, mock_sleep, mock_ping, relay_client):
        """Test the continuous polling with an error in the response."""
        # Setup mock ping to return an error response
        mock_ping.side_effect = [TEST_ERROR_RESPONSE]

        # Start polling
        relay_client.start()

        # Set up a callback to stop after sleep
        def stop_after_sleep(seconds):
            relay_client.stop()
            return None

        mock_sleep.side_effect = stop_after_sleep

        # Call the method
        relay_client.poll_relay_continuously()

        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_sleep.assert_called_once_with(10)  # Direct check of sleep call

        # Verify that polling was stopped
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_with_invalid_response(self, mock_sleep, mock_ping, relay_client):
        """Test polling with an invalid response (missing required fields)."""
        # Setup mock ping to return an invalid response
        mock_ping.side_effect = [{'invalid': 'response'}]  # Missing next_ping_in_x_seconds

        # Start polling
        relay_client.start()

        # Set up a callback to stop after sleep
        def stop_after_sleep(seconds):
            relay_client.stop()
            return None

        mock_sleep.side_effect = stop_after_sleep

        # Call the method
        relay_client.poll_relay_continuously()

        # Verify mock calls
        assert mock_ping.call_count == 1
        mock_sleep.assert_called_once_with(relay_client._request_timeout)  # Direct check of sleep call

        # Verify that polling was stopped
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_stops_after_repeated_error_response(
        self,
        mock_sleep,
        mock_ping,
        relay_client,
        monkeypatch,
    ):
        """Error-shaped responses should respect the consecutive failure cap."""
        monkeypatch.setenv("TOKENPLACE_MAX_POLL_FAILURES", "2")
        mock_ping.side_effect = [TEST_ERROR_RESPONSE, TEST_ERROR_RESPONSE]

        relay_client.start()
        relay_client.poll_relay_continuously()

        assert mock_ping.call_count == 2
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(10)
        assert relay_client.stop_polling is True

    @patch('utils.networking.relay_client.RelayClient.ping_relay')
    @patch('utils.networking.relay_client.time.sleep')
    def test_poll_relay_continuously_stops_after_repeated_invalid_response(
        self,
        mock_sleep,
        mock_ping,
        relay_client,
        monkeypatch,
    ):
        """Invalid sink payloads should respect the consecutive failure cap."""
        monkeypatch.setenv("TOKENPLACE_MAX_POLL_FAILURES", "2")
        mock_ping.side_effect = [{'invalid': 'response'}, {'invalid': 'response'}]

        relay_client.start()
        relay_client.poll_relay_continuously()

        assert mock_ping.call_count == 2
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(relay_client._request_timeout)
        assert relay_client.stop_polling is True


def test_poll_api_v1_encrypted_work_continuously_sleeps_no_work_hint(monkeypatch):
    client = RelayClient('http://localhost', 5000, MagicMock(public_key_b64='k'), MagicMock())
    client.stop_polling = False
    calls = {'n': 0}

    def fake_poll():
        calls['n'] += 1
        if calls['n'] == 1:
            return {'message': 'No requests available', 'next_ping_in_x_seconds': 0.25, 'poll_wait_seconds': 10}
        client.stop_polling = True
        return {'error': 'stop', 'next_ping_in_x_seconds': 0}

    sleeps = []
    monkeypatch.setattr(client, 'poll_api_v1_encrypted_work', fake_poll)
    monkeypatch.setattr(relay_client_module.time, 'sleep', lambda secs: sleeps.append(secs))

    client.poll_api_v1_encrypted_work_continuously()
    assert 0.25 in sleeps


def _standalone_relay_client():
    crypto = MagicMock()
    crypto.public_key_b64 = 'mock_public_key_b64'
    model = MagicMock()
    config = MagicMock()
    config.is_production = False
    config.get.side_effect = lambda key, default=None: {'relay.request_timeout': 15}.get(key, default)
    with patch('utils.networking.relay_client.get_config_lazy', return_value=config):
        return RelayClient('http://localhost', 5000, crypto, model)


@patch('utils.networking.relay_client.requests.post')
def test_unregister_from_relay_falls_back_to_legacy_unregister_on_404(mock_post):
    client = _standalone_relay_client()
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = 123.0
    api_v1_missing = MagicMock(status_code=404)
    legacy_success = MagicMock(status_code=200)
    mock_post.side_effect = [api_v1_missing, legacy_success]

    assert client.unregister_from_relay() is True

    requested_urls = [call.args[0] for call in mock_post.call_args_list]
    assert requested_urls == [
        'http://localhost:5000/api/v1/relay/servers/unregister',
        'http://localhost:5000/unregister',
    ]
    assert client._api_v1_registered_relays == set()


@patch('utils.networking.relay_client.requests.post')
def test_unregister_from_relay_fallback_strips_api_v1_suffix_for_legacy_unregister(mock_post):
    client = _standalone_relay_client()
    prefixed_url = 'http://localhost:5000/api/v1'
    client._relay_urls = [prefixed_url]
    client._api_v1_registered_relays.add(prefixed_url)
    client._api_v1_last_heartbeat_at[prefixed_url] = 123.0
    api_v1_missing = MagicMock(status_code=404)
    legacy_success = MagicMock(status_code=200)
    mock_post.side_effect = [api_v1_missing, legacy_success]

    assert client.unregister_from_relay() is True

    requested_urls = [call.args[0] for call in mock_post.call_args_list]
    assert requested_urls == [
        'http://localhost:5000/api/v1/relay/servers/unregister',
        'http://localhost:5000/unregister',
    ]


@patch('utils.networking.relay_client.requests.post')
def test_unregister_from_relay_is_idempotent_and_clears_api_v1_registration(mock_post):
    client = _standalone_relay_client()
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = 123.0
    client._api_v1_relay_wait_hints = {'http://localhost:5000': {'next_ping_in_x_seconds': 30}}
    mock_post.return_value = MagicMock(status_code=200)

    assert client.unregister_from_relay() is True
    assert client.unregister_from_relay() is True

    mock_post.assert_called_once_with(
        'http://localhost:5000/api/v1/relay/servers/unregister',
        json={'server_public_key': 'mock_public_key_b64'},
        timeout=15,
    )
    assert client._api_v1_registered_relays == set()
    assert client._api_v1_last_heartbeat_at == {}
    assert client._api_v1_relay_wait_hints == {}


@patch('utils.networking.relay_client.requests.post')
def test_unregister_from_relay_rechecks_registration_after_previous_empty_skip(mock_post):
    client = _standalone_relay_client()
    client._unregister_attempted = True
    client._unregister_complete = True
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = 123.0
    mock_post.return_value = MagicMock(status_code=200)

    assert client.unregister_from_relay() is True

    mock_post.assert_called_once_with(
        'http://localhost:5000/api/v1/relay/servers/unregister',
        json={'server_public_key': 'mock_public_key_b64'},
        timeout=15,
    )
    assert client._api_v1_registered_relays == set()


@patch('utils.networking.relay_client.requests.post')
def test_unregister_from_relay_logs_control_plane_429_diagnostic(mock_post, caplog):
    client = _standalone_relay_client()
    client._registration_token = 'super-secret-token'
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = 123.0

    response = MagicMock(status_code=429)
    response.headers = {
        'content-type': 'application/json',
        'retry-after': '41',
    }
    response.json.return_value = {
        'error': {
            'code': 'rate_limit_exceeded',
            'message': 'Rate limit exceeded. Try again in 41 seconds.',
            'type': 'rate_limit_error',
        }
    }
    mock_post.return_value = response

    with caplog.at_level('ERROR', logger='relay_client'):
        result = client.unregister_from_relay()

    assert result is False
    assert client._unregister_complete is False
    assert 'relay_control_plane_rate_limited' in caplog.text
    assert 'path=/api/v1/relay/servers/unregister' in caplog.text
    assert 'retry_after=41' in caplog.text
    assert 'super-secret-token' not in caplog.text


@patch('utils.networking.relay_client.requests.post')
def test_start_after_unregister_clears_stale_registration_and_polls_cleanly(mock_post):
    client = _standalone_relay_client()
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = 123.0
    client._api_v1_relay_wait_hints = {
        'http://localhost:5000': {
            'next_ping_in_x_seconds': 30,
            'poll_wait_seconds': 10,
            'server_public_key': 'mock_public_key_b64',
        }
    }
    client.stop()
    client._unregister_complete = True

    register_ok = MagicMock(status_code=200)
    register_ok.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 0}
    poll_ok = MagicMock(status_code=200)
    poll_ok.json.return_value = {'message': 'No requests available'}
    mock_post.side_effect = [register_ok, poll_ok]

    client.start()
    result = client.poll_api_v1_encrypted_work()

    requested_urls = [call.args[0] for call in mock_post.call_args_list]
    assert requested_urls == [
        'http://localhost:5000/api/v1/relay/servers/register',
        'http://localhost:5000/api/v1/relay/servers/poll',
    ]
    assert result['next_ping_in_x_seconds'] == 12
    assert client.api_v1_registration_fresh('http://localhost:5000') is True


@patch('utils.networking.relay_client.requests.post')
def test_poll_api_v1_encrypted_work_stop_prevents_register_and_poll(mock_post):
    client = _standalone_relay_client()
    client.stop()

    result = client.poll_api_v1_encrypted_work()

    assert result == {
        'error': 'Relay polling stopped',
        'next_ping_in_x_seconds': 0,
        'poll_wait_seconds': 0,
    }
    mock_post.assert_not_called()


@patch('utils.networking.relay_client.requests.post')
def test_poll_api_v1_encrypted_work_stop_after_register_retries_unregister(mock_post):
    client = _standalone_relay_client()
    client.start()
    client._unregister_attempted = True
    client._unregister_complete = True

    register_response = MagicMock(status_code=200)
    register_response.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 0}
    unregister_response = MagicMock(status_code=200)

    def fake_post(url, *args, **kwargs):
        if url.endswith('/relay/servers/register'):
            client.stop()
            return register_response
        if url.endswith('/api/v1/relay/servers/unregister'):
            return unregister_response
        raise AssertionError(f'Unexpected relay request: {url}')

    mock_post.side_effect = fake_post

    result = client.poll_api_v1_encrypted_work()

    assert result == {
        'error': 'Relay polling stopped',
        'next_ping_in_x_seconds': 0,
        'poll_wait_seconds': 0,
    }
    requested_urls = [call.args[0] for call in mock_post.call_args_list]
    assert requested_urls == [
        'http://localhost:5000/api/v1/relay/servers/register',
        'http://localhost:5000/api/v1/relay/servers/unregister',
    ]
    assert client._api_v1_registered_relays == set()
    assert client._api_v1_last_heartbeat_at == {}
    assert client._unregister_complete is True


def test_poll_api_v1_encrypted_work_continuously_clears_previous_stop_request(monkeypatch):
    client = _standalone_relay_client()
    client.stop()
    client._unregister_attempted = True
    observed_stop_flags = []

    def fake_poll():
        observed_stop_flags.append(client._polling_stopped_by_request)
        client.stop()
        return {'message': 'No requests available', 'next_ping_in_x_seconds': 0}

    monkeypatch.setattr(client, 'poll_api_v1_encrypted_work', fake_poll)
    monkeypatch.setattr(relay_client_module.time, 'sleep', lambda _seconds: None)

    client.poll_api_v1_encrypted_work_continuously()

    assert observed_stop_flags == [False]
    assert client.stop_polling is True
    assert client._polling_stopped_by_request is True
    assert client._unregister_attempted is False


@patch('utils.networking.relay_client.requests.post')
def test_start_after_stop_allows_api_v1_registration_again(mock_post):
    client = _standalone_relay_client()
    client.stop()
    client.start()
    register_ok = MagicMock(status_code=200)
    register_ok.json.return_value = {'next_ping_in_x_seconds': 12, 'poll_wait_seconds': 0}
    poll_ok = MagicMock(status_code=200)
    poll_ok.json.return_value = {'message': 'No requests available'}
    mock_post.side_effect = [register_ok, poll_ok]

    result = client.poll_api_v1_encrypted_work()

    assert result['next_ping_in_x_seconds'] == 12
    assert mock_post.call_count == 2


def _api_v1_validation_client(model_manager=None):
    crypto = MagicMock()
    crypto.public_key_b64 = TEST_VALID_RESPONSE["client_public_key"]
    with patch('utils.networking.relay_client.get_config_lazy') as mock_get_config:
        mock_config = MagicMock()
        mock_config.is_production = False
        mock_config.get.side_effect = lambda key, default: {
            'relay.request_timeout': 15,
            'relay.cluster_only': True,
        }.get(key, default)
        mock_get_config.return_value = mock_config
        return RelayClient(
            base_url='http://localhost',
            port=5000,
            crypto_manager=crypto,
            model_manager=model_manager or MagicMock(),
            include_configured_servers=False,
        )


class _ApiV1RuntimeManager:
    def __init__(self):
        self.runtime = MagicMock()
        self.runtime.create_chat_completion.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        self.use_mock_llm = True
        self.worker_health = "healthy"
        self.recovery_count = 0

    def get_llm_instance(self):
        return self.runtime


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, {"stream": False}),
        ({"max_tokens": 1}, {"max_tokens": 1}),
        ({"max_tokens": 8192}, {"max_tokens": 8192}),
        ({"temperature": 0}, {"temperature": 0.0}),
        ({"temperature": 2.0}, {"temperature": 2.0}),
        ({"top_p": 0}, {"top_p": 0.0}),
        ({"top_p": 1.0}, {"top_p": 1.0}),
        ({"frequency_penalty": -2}, {"frequency_penalty": -2.0}),
        ({"presence_penalty": 2.0}, {"presence_penalty": 2.0}),
        ({"seed": 0}, {"seed": 0}),
        ({"seed": 2**32 - 1}, {"seed": 2**32 - 1}),
        ({"stop": "END"}, {"stop": "END"}),
        ({"stop": ["END", "STOP"]}, {"stop": ["END", "STOP"]}),
        ({"stream": False}, {"stream": False}),
    ],
)
def test_api_v1_supported_option_boundaries_are_normalized(options, expected):
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-valid-options",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options=options,
    )

    assert "error" not in envelope["api_v1_response"]
    kwargs = manager.runtime.create_chat_completion.call_args.kwargs
    for key, value in expected.items():
        assert kwargs[key] == value
    assert kwargs["stream"] is False


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"max_tokens": True}, "compute_node_invalid_request"),
        ({"max_tokens": 0}, "compute_node_invalid_request"),
        ({"max_tokens": 8193}, "compute_node_invalid_request"),
        ({"temperature": False}, "compute_node_invalid_request"),
        ({"temperature": float("nan")}, "compute_node_invalid_request"),
        ({"temperature": float("inf")}, "compute_node_invalid_request"),
        ({"temperature": 10**10000}, "compute_node_invalid_request"),
        ({"temperature": 2.01}, "compute_node_invalid_request"),
        ({"top_p": -0.01}, "compute_node_invalid_request"),
        ({"frequency_penalty": -2.01}, "compute_node_invalid_request"),
        ({"presence_penalty": 2.01}, "compute_node_invalid_request"),
        ({"seed": -1}, "compute_node_invalid_request"),
        ({"seed": 1.5}, "compute_node_invalid_request"),
        ({"stop": ["x"] * 17}, "compute_node_invalid_request"),
        ({"stop": [True]}, "compute_node_invalid_request"),
        ({"stop": [""]}, "compute_node_invalid_request"),
        ({"stop": ["valid", ""]}, "compute_node_invalid_request"),
        ({"stop": ["", "valid"]}, "compute_node_invalid_request"),
        ({"stop": ""}, "compute_node_invalid_request"),
        ({"stop": "x" * 257}, "compute_node_invalid_request"),
        ({"stream": True}, "compute_node_options_unsupported"),
        ({"response_format": {"type": "text"}}, "compute_node_options_unsupported"),
        ({"tools": []}, "compute_node_options_unsupported"),
        ({"tool_choice": "none"}, "compute_node_options_unsupported"),
        ({"tool_choice": "auto"}, "compute_node_options_unsupported"),
        ({"response_format": {"type": "json_object"}}, "compute_node_options_unsupported"),
        ({"logprobs": True}, "compute_node_options_unsupported"),
        ({"unknown_option": "value"}, "compute_node_options_unsupported"),
        ({1: "value"}, "compute_node_invalid_request"),
    ],
)
def test_api_v1_invalid_and_unsupported_options_do_not_call_worker(options, code):
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    before = (manager.worker_health, manager.recovery_count)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-invalid-options",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "SENTINEL PROMPT"}],
        options=options,
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == code
    assert "SENTINEL" not in json.dumps(error)
    if code == "compute_node_invalid_request":
        assert "invalid for the desktop runtime" in error["message"]
        assert "unsupported" not in error["message"]
    else:
        assert "unsupported by the desktop runtime" in error["message"]
    manager.runtime.create_chat_completion.assert_not_called()
    assert (manager.worker_health, manager.recovery_count) == before


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "function", "content": "x"}],
        [{"role": "tool", "content": "hello"}],
        [{"role": "user"}],
        [{"role": "user", "content": "x", "tool_calls": []}],
        [{"role": "user", "content": "x" * (RelayClient._API_V1_MAX_MESSAGE_CONTENT_CHARS + 1)}],
        [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}],
        [{"role": "user", "content": [{"type": "text", "text": ""}]}],
        [{"role": "user", "content": [{"type": "text", "text": "x", "extra": "no"}]}],
        [{"role": "user", "content": "x"}] * (RelayClient._API_V1_MAX_MESSAGES + 1),
        [
            {"role": "user", "content": "x" * RelayClient._API_V1_MAX_MESSAGE_CONTENT_CHARS}
        ]
        * 5,
    ],
)
def test_api_v1_invalid_messages_are_rejected_before_worker(messages):
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    before = (manager.worker_health, manager.recovery_count)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-invalid-messages",
        model_id="llama-3-8b-instruct",
        messages=messages,
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_request"
    manager.runtime.create_chat_completion.assert_not_called()
    assert (manager.worker_health, manager.recovery_count) == before


def test_api_v1_unsupported_option_error_message_caps_attacker_controlled_names():
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    options = {f"unknown_option_{index}": "value" for index in range(20)}

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-too-many-options",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options=options,
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert "and 15 more option(s)" in error["message"]
    assert "unknown_option_19" not in error["message"]
    manager.runtime.create_chat_completion.assert_not_called()


def test_api_v1_valid_request_succeeds_immediately_after_rejected_request():
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)

    rejected = client._generate_api_v1_response_with_runtime_model(
        request_id="req-rejected",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "poison"}],
        options={"temperature": float("nan")},
    )
    accepted = client._generate_api_v1_response_with_runtime_model(
        request_id="req-accepted",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={},
    )

    assert rejected["api_v1_response"]["error"]["code"] == "compute_node_invalid_request"
    assert accepted["api_v1_response"]["message"]["content"] == "ok"
    manager.runtime.create_chat_completion.assert_called_once()
