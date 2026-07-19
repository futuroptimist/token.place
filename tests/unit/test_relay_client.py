"""
Unit tests for the relay client module.
"""
import base64
import builtins
import json
import math
import pytest
import sys
import threading
import time
import requests
import jsonschema
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.networking import relay_client as relay_client_module
from utils.networking.relay_client import RelayClient, MESSAGE_SCHEMA, RELAY_RESPONSE_SCHEMA


def test_api_v1_models_module_import_failure_does_not_capture_worker_diagnostics(monkeypatch):
    """Module import probing is separate from request-scoped worker diagnostics."""

    class ImportFailureWithDiagnostics(RuntimeError):
        diagnostics = {"reason": "runtime_chat_template_metadata_missing"}

    def fail_import(name):
        assert name == "api.v1.models"
        raise ImportFailureWithDiagnostics("api v1 models unavailable")

    monkeypatch.setattr(relay_client_module.importlib, "import_module", fail_import)

    assert RelayClient._api_v1_models_module() is None


def test_worker_diagnostic_sanitizer_preserves_tokenization_string_enums():
    safe = relay_client_module._safe_worker_diagnostics(
        {
            "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
            "plain_completion_prompt_tokenization_method": "llama.tokenize",
            "plain_completion_prompt_tokenization_attempted": True,
            "plain_completion_prompt_tokenization_special": True,
            "plain_completion_prompt_token_count": 3,
            "plain_completion_reset_after_failure_count": 2,
            "content": "SECRET prompt text",
            "rendered_prompt": "SECRET rendered prompt",
            "token_ids": [1, 2, 3],
            "assistant_output": "SECRET output",
            "key": "SECRET key",
            "tool_args": {"secret": True},
            "ciphertext": "SECRET ciphertext",
        }
    )

    assert safe == {
        "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
        "plain_completion_prompt_tokenization_method": "llama.tokenize",
        "plain_completion_prompt_tokenization_attempted": True,
        "plain_completion_prompt_tokenization_special": True,
        "plain_completion_prompt_token_count": 3,
        "plain_completion_reset_after_failure_count": 2,
    }
    assert "SECRET" not in json.dumps(safe)


@pytest.mark.parametrize(
    "category",
    [
        "context_window_exceeded",
        "context_length_exceeded",
        "token_overflow",
    ],
)
def test_worker_diagnostic_sanitizer_preserves_tokenization_length_categories(category):
    safe = relay_client_module._safe_worker_diagnostics(
        {
            "plain_completion_prompt_tokenization_error_category": category,
            "plain_completion_prompt_tokenization_method": "llama.tokenize",
            "plain_completion_prompt_tokenization_attempted": True,
            "rendered_prompt": "SECRET rendered prompt",
            "token_ids": [1, 2, 3],
        }
    )

    assert safe == {
        "plain_completion_prompt_tokenization_error_category": category,
        "plain_completion_prompt_tokenization_method": "llama.tokenize",
        "plain_completion_prompt_tokenization_attempted": True,
    }
    assert "SECRET" not in json.dumps(safe)

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


def _api_v1_decrypted_payload(*, request_id="req-routing", client_public_key=TEST_VALID_RESPONSE["client_public_key"], routing_marker=...):
    api_v1_request = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "Hello"}],
        "options": {},
    }
    if routing_marker is not ...:
        api_v1_request["routing"] = routing_marker
    return {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": request_id,
        "client_public_key": client_public_key,
        "api_v1_request": api_v1_request,
    }


def test_extract_api_v1_request_payload_defaults_missing_routing_to_8k_fast():
    payload = relay_client_module._extract_api_v1_request_payload(
        _api_v1_decrypted_payload(routing_marker=...),
        TEST_VALID_RESPONSE["client_public_key"],
    )

    assert payload is not None
    assert payload["routing"] == {"context_tier": "8k-fast"}


def test_extract_api_v1_request_payload_accepts_valid_64k_routing():
    payload = relay_client_module._extract_api_v1_request_payload(
        _api_v1_decrypted_payload(routing_marker={"context_tier": "64k-full"}),
        TEST_VALID_RESPONSE["client_public_key"],
    )

    assert payload is not None
    assert payload["routing"] == {"context_tier": "64k-full"}


def test_extract_api_v1_request_payload_rejects_malformed_decrypted_routing():
    payload = relay_client_module._extract_api_v1_request_payload(
        _api_v1_decrypted_payload(routing_marker="64k-full"),
        TEST_VALID_RESPONSE["client_public_key"],
    )

    assert payload is None


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
        mock.runtime.apply_chat_template.side_effect = (
            lambda messages, tokenize=False, add_generation_prompt=True: "".join(
                f"<{message['role']}>{message['content']}" for message in messages
            ) + ("<assistant>" if add_generation_prompt else "")
        )
        mock.runtime.tokenize.side_effect = (
            lambda payload, _add_bos=False: list(range(len(payload)))
        )
        mock.get_llm_instance.return_value = mock.runtime
        mock.create_chat_completion_with_recovery = None
        mock.use_mock_llm = True
        mock.context_tier = "8k-fast"
        mock.context_window_tokens = 8192
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


    def test_stopped_heartbeat_response_does_not_resurrect_after_unregister(self, relay_client):
        """An in-flight heartbeat must not repopulate local registration after Stop/unregister."""

        relay_client._api_v1_registered_relays.add(relay_client.relay_url)
        relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = 0.0
        relay_client._api_v1_relay_wait_hints[relay_client.relay_url] = {
            "next_ping_in_x_seconds": 1,
            "poll_wait_seconds": 1,
            "server_public_key": relay_client.crypto_manager.public_key_b64,
        }

        heartbeat_entered = threading.Event()
        allow_heartbeat_return = threading.Event()
        network_events = []

        def delayed_register(candidate_url):
            assert candidate_url == relay_client.relay_url
            network_events.append("register_started")
            heartbeat_entered.set()
            assert allow_heartbeat_return.wait(timeout=2)
            return {"next_ping_in_x_seconds": 120, "poll_wait_seconds": 1}

        relay_client.register_api_v1_compute_node = delayed_register
        with patch('utils.networking.relay_client.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            def unregister_side_effect(*args, **kwargs):
                network_events.append("unregister_post")
                return mock_response
            mock_post.side_effect = unregister_side_effect
            heartbeat_thread = threading.Thread(target=relay_client._api_v1_heartbeat_worker)
            heartbeat_thread.start()
            assert heartbeat_entered.wait(timeout=2)

            relay_client.stop()
            assert relay_client.unregister_from_relay() is True
            allow_heartbeat_return.set()
            heartbeat_thread.join(timeout=2)

            assert not heartbeat_thread.is_alive()
            assert relay_client._api_v1_registered_relays == set()
            assert relay_client._api_v1_last_heartbeat_at == {}
            assert relay_client._api_v1_relay_wait_hints == {}
            assert network_events[-1] == "unregister_post"
            assert network_events == ["register_started", "unregister_post"]

    def test_unregister_from_relay_reports_partial_result_with_shared_deadline(
        self,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Partial multi-relay unregister should still remove healthy targets within deadline."""

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

        requested = []

        def side_effect(url, **kwargs):
            requested.append(url)
            response = MagicMock()
            if url.startswith(primary):
                response.status_code = 200
                return response
            raise requests.Timeout("relay timeout")

        with patch('utils.networking.relay_client.requests.post', side_effect=side_effect):
            started = time.monotonic()
            result = client.unregister_from_relay(shutdown_deadline=time.monotonic() + 0.2)
            elapsed = time.monotonic() - started

        assert result is False
        assert elapsed < 1.0
        assert requested == [
            f'{primary}/api/v1/relay/servers/unregister',
            f'{backup}/api/v1/relay/servers/unregister',
        ]
        assert client._api_v1_registered_relays == {backup}
        assert primary not in client._api_v1_last_heartbeat_at
        assert backup in client._api_v1_last_heartbeat_at

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
    def test_register_api_v1_compute_node_captures_control_credential(self, mock_post, relay_client):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            'next_ping_in_x_seconds': 30,
            'control_credential': 'relay-a-owner-secret',
        }
        mock_post.return_value = response

        result = relay_client.register_api_v1_compute_node('http://relay-a.example')

        assert result['control_credential'] == 'relay-a-owner-secret'
        assert relay_client._api_v1_control_credentials_by_relay == {
            'http://relay-a.example': 'relay-a-owner-secret'
        }

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_sends_matching_control_credential_per_relay(self, mock_post, relay_client):
        relay_client._relay_urls = ('http://relay-a.example', 'http://relay-b.example')
        relay_client._api_v1_registered_relays.update(relay_client._relay_urls)
        relay_client._api_v1_last_heartbeat_at.update({
            'http://relay-a.example': 1.0,
            'http://relay-b.example': 2.0,
        })
        relay_client._api_v1_control_credentials_by_relay.update({
            'http://relay-a.example': 'credential-a',
            'http://relay-b.example': 'credential-b',
        })
        mock_post.side_effect = [MagicMock(status_code=200), MagicMock(status_code=200)]

        assert relay_client.unregister_from_relay() is True

        assert [call.kwargs['json'] for call in mock_post.call_args_list] == [
            {'server_public_key': 'mock_public_key_b64', 'control_credential': 'credential-a'},
            {'server_public_key': 'mock_public_key_b64', 'control_credential': 'credential-b'},
        ]
        assert relay_client._api_v1_control_credentials_by_relay == {}

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_retains_failed_control_credential_for_retry(self, mock_post, relay_client):
        relay_client._api_v1_registered_relays.add(relay_client.relay_url)
        relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = 1.0
        relay_client._api_v1_control_credentials_by_relay[relay_client.relay_url] = 'retry-secret'
        mock_post.side_effect = [MagicMock(status_code=503), MagicMock(status_code=200)]

        assert relay_client.unregister_from_relay() is False
        assert relay_client._api_v1_control_credentials_by_relay[relay_client.relay_url] == 'retry-secret'
        assert relay_client.unregister_from_relay() is True
        assert relay_client._api_v1_control_credentials_by_relay == {}

    @patch('utils.networking.relay_client.requests.post')
    def test_unregister_from_relay_legacy_fallback_uses_control_credential(self, mock_post, relay_client):
        relay_client._api_v1_registered_relays.add(relay_client.relay_url)
        relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = 1.0
        relay_client._api_v1_control_credentials_by_relay[relay_client.relay_url] = 'fallback-secret'
        mock_post.side_effect = [MagicMock(status_code=404), MagicMock(status_code=200)]

        assert relay_client.unregister_from_relay() is True

        assert [call.args[0] for call in mock_post.call_args_list] == [
            'http://localhost:5000/api/v1/relay/servers/unregister',
            'http://localhost:5000/unregister',
        ]
        assert all(call.kwargs['json']['control_credential'] == 'fallback-secret' for call in mock_post.call_args_list)

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
    def test_register_api_v1_compute_node_sends_context_capabilities(self, mock_post, relay_client):
        response = MagicMock(status_code=200)
        response.json.return_value = {'next_ping_in_x_seconds': 30, 'poll_wait_seconds': 0}
        mock_post.return_value = response
        relay_client.model_manager.api_model_id = "qwen3-8b-instruct"
        relay_client.model_manager.model_id = None
        relay_client.model_manager.file_name = "Qwen3-8B-Q4_K_M.gguf"
        relay_client.model_manager.model_path = "/models/Qwen3-8B-Q4_K_M.gguf"
        relay_client.model_manager.context_tier = "64k-full"
        relay_client.model_manager.context_window_tokens = 65536
        relay_client.model_manager.last_compute_diagnostics = {"backend_used": "cuda"}

        result = relay_client.register_api_v1_compute_node('http://relay-a.example')

        assert result['next_ping_in_x_seconds'] == 30
        payload = mock_post.call_args.kwargs["json"]
        assert payload["server_public_key"] == "mock_public_key_b64"
        assert payload["capabilities"]["active_context_tier"] == "64k-full"
        assert payload["capabilities"]["maximum_total_context_tokens"] == 65536
        assert payload["capabilities"]["max_concurrency"] == 1
        assert payload["capabilities"]["backend_class"] == "cuda"
        assert payload["capabilities"]["supported_model_ids"] == ["qwen3-8b-instruct"]

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
    def test_api_v1_non_200_diagnostic_redacts_base_path_control_credential(
        self, mock_post, relay_client, caplog
    ):
        credential = 'relay-control-secret-for-api-v1-base-path'
        relay_client._api_v1_control_credentials_by_relay[
            'https://relay.example/api/v1'
        ] = credential
        response = MagicMock(status_code=403)
        response.headers = {'server': 'gunicorn', 'content-type': 'application/json'}
        response.json.return_value = {
            'error': f'bad owner credential {credential}',
            'relayControlCredential': credential,
            'controlCredential': credential,
            'detail': {'message': f'unlabelled echo {credential}'},
        }
        mock_post.return_value = response

        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client._api_v1_http_error_result(
                response,
                method='POST',
                url='https://relay.example/api/v1/relay/servers/unregister',
                token_sent=False,
                next_ping_in_x_seconds=relay_client._request_timeout,
            )

        rendered = json.dumps(result, sort_keys=True)
        assert credential not in rendered
        assert credential not in caplog.text
        assert 'bad owner credential [redacted]' in result['relay_error']
        body_snippet = result['relay_http_diagnostic']['body_snippet']
        assert '"relayControlCredential":"[redacted]"' in body_snippet
        assert '"controlCredential":"[redacted]"' in body_snippet

    def test_api_v1_control_credential_lookup_uses_slash_delimited_longest_prefix(self, relay_client):
        relay_client._api_v1_control_credentials_by_relay.update({
            'https://relay.example': 'host-secret',
            'https://relay.example/api/v1': 'base-path-secret',
            'https://relay.example/api/v10': 'wrong-path-secret',
            'https://relay.example.evil/api/v1': 'evil-secret',
        })

        assert relay_client._api_v1_control_credential_for_request_url(
            'https://relay.example/api/v1/relay/servers/control'
        ) == 'base-path-secret'
        assert relay_client._api_v1_control_credential_for_request_url(
            'https://relay.example/api/v10/relay/servers/control'
        ) == 'wrong-path-secret'
        assert relay_client._api_v1_control_credential_for_request_url(
            'https://relay.example.evil/api/v1/relay/servers/control'
        ) == 'evil-secret'
        assert relay_client._api_v1_control_credential_for_request_url(
            'https://relay.example.evilish/api/v1/relay/servers/control'
        ) == ''

    def test_api_v1_control_credential_lookup_and_mutation_use_map_lock(
        self, relay_client, caplog
    ):
        class GuardedCredentialMap(dict):
            def __init__(self, guard, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._guard = guard
                self.items_called = threading.Event()

            def items(self):
                assert self._guard.locked()
                self.items_called.set()
                return super().items()

            def __setitem__(self, key, value):
                assert self._guard.locked()
                return super().__setitem__(key, value)

            def pop(self, key, default=None):
                assert self._guard.locked()
                return super().pop(key, default)

            def clear(self):
                assert self._guard.locked()
                return super().clear()

        credential = 'guarded-base-path-control-secret'
        guarded = GuardedCredentialMap(
            relay_client._api_v1_control_credentials_lock,
            {'https://relay.example/api/v1': credential},
        )
        relay_client._api_v1_control_credentials_by_relay = guarded

        assert relay_client._api_v1_control_credential_for_request_url(
            'https://relay.example/api/v1/relay/servers/control'
        ) == credential
        assert guarded.items_called.is_set()

        relay_client._store_api_v1_control_credential(
            'https://relay.example/api/v1/blue', 'rotated-secret'
        )
        assert relay_client._api_v1_control_credential_for_relay(
            'https://relay.example/api/v1/blue'
        ) == 'rotated-secret'
        relay_client._pop_api_v1_control_credential('https://relay.example/api/v1/blue')
        assert relay_client._api_v1_control_credential_for_relay(
            'https://relay.example/api/v1/blue'
        ) == ''

        response = MagicMock(status_code=403)
        response.headers = {'content-type': 'text/plain'}
        response.text = f'credential echoed without label {credential}'
        with caplog.at_level('ERROR', logger='relay_client'):
            result = relay_client._api_v1_http_error_result(
                response,
                method='POST',
                url='https://relay.example/api/v1/relay/servers/control',
                token_sent=False,
                next_ping_in_x_seconds=relay_client._request_timeout,
            )

        assert credential not in json.dumps(result, sort_keys=True)
        assert credential not in caplog.text

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

        assert result['error'] == 'Timeout'
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
                "routing": {"context_tier": "8k-fast"},
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
        posted_payload = mock_post.call_args.kwargs["json"]
        posted_json = json.dumps(posted_payload)
        for forbidden in (
            "api_v1_response",
            "messages",
            "prompt",
            "model",
            "routing",
            "context_tier",
            "llama-3-8b-instruct",
            "8k-fast",
            "Hello",
        ):
            assert forbidden not in posted_json

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
        mock_model_manager.api_model_id = "qwen3-8b-instruct"
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Qwen3-8B-Q4_K_M.gguf"
        mock_model_manager.model_path = "/models/Qwen3-8B-Q4_K_M.gguf"
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
    def test_process_client_request_api_v1_aliases_use_active_qwen_runtime(
        self,
        mock_post,
        model_id,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
        monkeypatch,
    ):
        """Profile-derived alias map preserves API v1 semantics for an active Qwen runtime."""

        monkeypatch.setattr(
            "utils.networking.relay_client.importlib.import_module",
            MagicMock(side_effect=ModuleNotFoundError("No module named 'api'")),
        )
        request_data = TEST_VALID_RESPONSE.copy()
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = "qwen3-8b-instruct"
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Qwen3-8B-Q4_K_M.gguf"
        mock_model_manager.model_path = "/models/Qwen3-8B-Q4_K_M.gguf"
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
    def test_process_client_request_api_v1_request_scoped_inference_error_keeps_runtime_healthy(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Request-scoped llama.cpp failures submit errors without marking runtime unhealthy."""
        from utils.llm.model_manager import LlamaCppInferenceRequestError

        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-bad-prompt",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        mock_model_manager.create_chat_completion_with_recovery = MagicMock(
            side_effect=LlamaCppInferenceRequestError("bad request")
        )
        mock_crypto_manager.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv',
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        typed_result = relay_client.process_client_request_result(request_data)

        assert typed_result.submitted is True
        assert typed_result.inference_succeeded is False
        assert typed_result.safe_error_code == "compute_node_internal_error"
        assert typed_result.runtime_healthy is True
        assert typed_result.recovery_attempted is False
        assert typed_result.recovery_succeeded is False
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["error"]["code"] == (
            "compute_node_internal_error"
        )

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_exhausted_replacement_reports_recovery_failed(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        """Exhausted worker replacement is submitted as an encrypted error with recovery metadata."""
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-worker-dead",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        mock_model_manager.create_chat_completion_with_recovery = MagicMock(
            side_effect=RuntimeError(
                "LLM runtime replacement failed after one restart attempt"
            )
        )
        mock_crypto_manager.encrypt_message.return_value = {
            'chat_history': 'encrypted_chat_history',
            'cipherkey': 'encrypted_key',
            'iv': 'encrypted_iv',
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        typed_result = relay_client.process_client_request_result(request_data)

        assert typed_result.submitted is True
        assert typed_result.inference_succeeded is False
        assert typed_result.safe_error_code == "compute_node_internal_error"
        assert typed_result.runtime_healthy is False
        assert typed_result.recovery_attempted is True
        assert typed_result.recovery_succeeded is False

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

    def test_api_v1_supported_model_ids_ignores_none_model_path(self, relay_client, mock_model_manager):
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = None
        mock_model_manager.model_path = None

        supported_model_ids = relay_client._api_v1_supported_model_ids()

        assert "none" not in supported_model_ids


    def test_api_v1_supported_model_ids_stale_llama_runtime_does_not_advertise_qwen(
        self, relay_client, mock_model_manager
    ):
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/tmp/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"

        supported_model_ids = relay_client._api_v1_supported_model_ids()

        assert "qwen3-8b-instruct" not in supported_model_ids

    def test_api_v1_supported_model_ids_respects_manager_support_hook(self, relay_client):
        class _Manager:
            api_model_id = "custom-local-api-v1-model"
            model_id = None
            file_name = None
            model_path = None

            def supports_api_v1_model(self, model_id):
                return model_id == "custom-local-api-v1-model"

        relay_client.model_manager = _Manager()

        assert relay_client._api_v1_supported_model_ids() == ["custom-local-api-v1-model"]

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_rejects_unsatisfied_context_tier(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        mock_model_manager.context_tier = "8k-fast"
        mock_model_manager.context_window_tokens = 8192
        mock_model_manager.config.get.side_effect = lambda key, default: {
            "model.max_tokens": 512,
            "model.temperature": 0.7,
            "model.top_p": 0.9,
            "model.stop_tokens": [],
        }.get(key, default)
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-context-tier",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
                "routing": {"context_tier": "64k-full"},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        mock_model_manager.runtime.apply_chat_template.side_effect = (
            lambda messages, tokenize=False, add_generation_prompt=True: "<s><user>Hello<assistant>"
        )
        mock_model_manager.runtime.tokenize.side_effect = (
            lambda payload, _add_bos=False: list(range(len(payload)))
        )

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        error = encrypted_envelope["api_v1_response"]["error"]
        assert error["code"] == "compute_node_context_tier_unsupported"
        assert error["active_context_tier"] == "8k-fast"
        assert error["requested_context_tier"] == "64k-full"
        assert error["configured_context_tokens"] == 8192
        assert error["prompt_tokens"] == len(b"<s><user>Hello<assistant>")
        assert error["requested_output_tokens"] == 512
        assert error["required_total_tokens"] == error["prompt_tokens"] + 512
        assert error["retryable"] is False
        assert "recommended_context_tier" not in error

    @patch('utils.networking.relay_client.requests.post')
    def test_process_client_request_api_v1_strips_routing_context_tier(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        mock_model_manager.context_tier = "64k-full"
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-context-tier-space",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
                "routing": {"context_tier": "64k-full "},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert "error" not in encrypted_envelope["api_v1_response"]
        assert encrypted_envelope["api_v1_response"]["message"]["role"] == "assistant"

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
    def test_process_client_request_api_v1_stale_llama_runtime_rejects_qwen(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
        mock_model_manager,
    ):
        mock_model_manager.use_mock_llm = False
        mock_model_manager.api_model_id = None
        mock_model_manager.model_id = None
        mock_model_manager.file_name = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        mock_model_manager.model_path = "/tmp/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        request_data = TEST_VALID_RESPONSE.copy()
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-stale-llama-qwen",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "qwen3-8b-instruct",
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
                self.runtime.apply_chat_template.side_effect = (
                    lambda messages, tokenize=False, add_generation_prompt=True: "".join(
                        f"<{message['role']}>{message['content']}" for message in messages
                    ) + ("<assistant>" if add_generation_prompt else "")
                )
                self.runtime.tokenize.side_effect = (
                    lambda payload, _add_bos=False: list(range(len(payload)))
                )

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
    def test_process_client_request_api_v1_resolves_alias_before_manager_support_hook(
        self,
        mock_post,
        relay_client,
        mock_crypto_manager,
    ):
        """Qwen runtimes should accept relay-scheduled compatibility alias payloads."""

        class _Manager:
            use_mock_llm = False

            def supports_api_v1_model(self, model_id):
                self.checked_model_ids.append(model_id)
                return model_id == "qwen3-8b-instruct"

            def __init__(self):
                self.checked_model_ids = []
                self.runtime = MagicMock()
                self.runtime.create_chat_completion.return_value = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "alias ok",
                            }
                        }
                    ]
                }
                self.runtime.apply_chat_template.side_effect = (
                    lambda messages, tokenize=False, add_generation_prompt=True: "".join(
                        f"<{message['role']}>{message['content']}" for message in messages
                    ) + ("<assistant>" if add_generation_prompt else "")
                )
                self.runtime.tokenize.side_effect = (
                    lambda payload, _add_bos=False: list(range(len(payload)))
                )

            def get_llm_instance(self):
                return self.runtime

        request_data = TEST_VALID_RESPONSE.copy()
        manager = _Manager()
        relay_client.model_manager = manager
        mock_crypto_manager.decrypt_message.return_value = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": "req-alias-model",
            "client_public_key": request_data["client_public_key"],
            "api_v1_request": {
                "model": "llama-3.1-8b-instruct",
                "messages": [{"role": "user", "content": "Hello"}],
                "options": {},
            },
        }
        source_response = MagicMock()
        source_response.status_code = 200
        mock_post.return_value = source_response

        assert relay_client.process_client_request(request_data) is True

        assert "qwen3-8b-instruct" in manager.checked_model_ids
        encrypted_envelope = mock_crypto_manager.encrypt_message.call_args.args[0]
        assert encrypted_envelope["api_v1_response"]["message"]["content"] == "alias ok"

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
                self.apply_chat_template = (
                    lambda messages, tokenize=False, add_generation_prompt=True: "".join(
                        f"<{message['role']}>{message['content']}" for message in messages
                    ) + ("<assistant>" if add_generation_prompt else "")
                )
                self.tokenize = lambda payload, _add_bos=False: list(range(len(payload)))

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

            def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
                return "".join(
                    f"<{message['role']}>{message['content']}" for message in messages
                ) + ("<assistant>" if add_generation_prompt else "")

            def tokenize(self, payload, _add_bos=False):
                return list(range(len(payload)))

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

            def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
                return "".join(
                    f"<{message['role']}>{message['content']}" for message in messages
                ) + ("<assistant>" if add_generation_prompt else "")

            def tokenize(self, payload, _add_bos=False):
                return list(range(len(payload)))

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
            api_model_id = "qwen3-8b-instruct"
            model_id = None
            file_name = "Qwen3-8B-Q4_K_M.gguf"
            model_path = "/tmp/Qwen3-8B-Q4_K_M.gguf"

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
                "model": "qwen3-8b-instruct",
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

            def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
                return "".join(
                    f"<{message['role']}>{message['content']}" for message in messages
                ) + ("<assistant>" if add_generation_prompt else "")

            def tokenize(self, payload, _add_bos=False):
                return list(range(len(payload)))

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
    ]
    assert client._api_v1_registered_relays == {'http://localhost:5000'}
    assert client._unregister_complete is False


@patch('utils.networking.relay_client.requests.post')
def test_poll_exception_after_shutdown_latch_preserves_registration_for_unregister(mock_post):
    client = _standalone_relay_client()
    relay_url = 'http://localhost:5000'
    client.start()
    client._api_v1_registered_relays.add(relay_url)
    client._api_v1_last_heartbeat_at[relay_url] = time.monotonic()
    client._api_v1_relay_wait_hints[relay_url] = {
        'next_ping_in_x_seconds': 30,
        'poll_wait_seconds': 10,
        'server_public_key': client.crypto_manager.public_key_b64,
    }
    client._active_relay_index = 0
    client._last_api_v1_work_relay_url = relay_url

    poll_started = threading.Event()
    release_poll = threading.Event()
    poll_result = {}

    unregister_response = MagicMock(status_code=200)

    def fake_post(url, *args, **kwargs):
        if url.endswith('/relay/servers/poll'):
            poll_started.set()
            assert release_poll.wait(timeout=2)
            raise requests.ConnectionError("poll failed after stop latch")
        if url.endswith('/relay/servers/unregister'):
            return unregister_response
        raise AssertionError(f'Unexpected relay request: {url}')

    mock_post.side_effect = fake_post

    poll_thread = threading.Thread(
        target=lambda: poll_result.update(client.poll_api_v1_encrypted_work()),
        daemon=True,
    )
    poll_thread.start()
    assert poll_started.wait(timeout=2)

    client._api_v1_latch_shutdown()
    release_poll.set()
    poll_thread.join(timeout=2)
    assert not poll_thread.is_alive()

    assert poll_result == {
        'error': 'Relay polling stopped',
        'next_ping_in_x_seconds': 0,
        'poll_wait_seconds': 0,
    }
    assert client._api_v1_registered_relays == {relay_url}
    assert set(client._api_v1_last_heartbeat_at) == {relay_url}
    assert client._api_v1_relay_wait_hints == {
        relay_url: {
            'next_ping_in_x_seconds': 30,
            'poll_wait_seconds': 10,
            'server_public_key': client.crypto_manager.public_key_b64,
        }
    }
    assert client._active_relay_index == 0
    assert client._last_api_v1_work_relay_url == relay_url

    assert client.unregister_from_relay() is True

    requested_urls = [call.args[0] for call in mock_post.call_args_list]
    assert requested_urls == [
        'http://localhost:5000/api/v1/relay/servers/poll',
        'http://localhost:5000/api/v1/relay/servers/unregister',
    ]
    assert requested_urls[-1].endswith('/api/v1/relay/servers/unregister')
    assert client._api_v1_registered_relays == set()
    assert client._api_v1_last_heartbeat_at == {}
    assert client._api_v1_relay_wait_hints == {}


@patch('utils.networking.relay_client.requests.post')
def test_long_poll_timeout_after_shutdown_latch_does_not_mutate_heartbeat_state(mock_post):
    client = _standalone_relay_client()
    relay_url = 'http://localhost:5000'
    client.start()
    client._api_v1_registered_relays.add(relay_url)
    client._api_v1_last_heartbeat_at[relay_url] = time.monotonic()
    client._api_v1_relay_wait_hints[relay_url] = {
        'next_ping_in_x_seconds': 30,
        'poll_wait_seconds': 0.1,
        'server_public_key': client.crypto_manager.public_key_b64,
    }

    unregister_response = MagicMock(status_code=200)

    def fake_post(url, *args, **kwargs):
        if url.endswith('/relay/servers/poll'):
            client._api_v1_latch_shutdown()
            raise requests.Timeout("Read timed out after stop latch")
        if url.endswith('/relay/servers/unregister'):
            return unregister_response
        raise AssertionError(f'Unexpected relay request: {url}')

    mock_post.side_effect = fake_post

    result = client.poll_api_v1_encrypted_work()

    assert result == {
        'error': 'Relay polling stopped',
        'next_ping_in_x_seconds': 0,
        'poll_wait_seconds': 0,
    }
    assert client._api_v1_registered_relays == {relay_url}
    assert set(client._api_v1_last_heartbeat_at) == {relay_url}
    assert client._api_v1_relay_wait_hints == {
        relay_url: {
            'next_ping_in_x_seconds': 30,
            'poll_wait_seconds': 0.1,
            'server_public_key': client.crypto_manager.public_key_b64,
        }
    }

    assert client.unregister_from_relay() is True

    requested_urls = [call.args[0] for call in mock_post.call_args_list]
    assert requested_urls == [
        'http://localhost:5000/api/v1/relay/servers/poll',
        'http://localhost:5000/api/v1/relay/servers/unregister',
    ]
    assert client._api_v1_registered_relays == set()


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
        self.runtime.create_chat_completion_from_rendered_prompt.side_effect = (
            lambda messages, **kwargs: self.runtime.create_chat_completion(
                messages=messages, **kwargs
            )
        )
        self.runtime.apply_chat_template.side_effect = (
            lambda messages, tokenize=False, add_generation_prompt=True, **kwargs: "".join(
                f"<{message['role']}>{message['content']}" for message in messages
            ) + ("<assistant>" if add_generation_prompt else "")
        )
        self.runtime.tokenize.side_effect = (
            lambda payload, _add_bos=False: list(range(len(payload)))
        )
        self.use_mock_llm = True
        self.worker_health = "healthy"
        self.recovery_count = 0

    def get_llm_instance(self):
        return self.runtime



def test_api_v1_qwen_generation_uses_render_then_complete_not_chat_completion():
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled", "profile_id": "qwen3-8b-q4-k-m"}
    manager.context_tier = "64k-full"
    manager.context_window_tokens = 65536
    manager.runtime.create_chat_completion.side_effect = AssertionError("chat path must not be used for qwen")
    render_complete = MagicMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "ok"}}]
    })
    manager.runtime.create_chat_completion_from_rendered_prompt = render_complete
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-render-complete",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"max_tokens": 64, "stream": False},
        requested_context_tier="64k-full",
    )

    assert envelope["api_v1_response"]["message"] == {"role": "assistant", "content": "ok"}
    manager.runtime.create_chat_completion.assert_not_called()
    kwargs = manager.runtime.create_chat_completion_from_rendered_prompt.call_args.kwargs
    assert kwargs == {
        "max_tokens": 64,
        "token_place_provider": "qwen",
        "enable_thinking": False,
    }
    messages = manager.runtime.create_chat_completion_from_rendered_prompt.call_args.args[0]
    assert messages[-1]["content"] == "hi"





@pytest.mark.parametrize("context_tier", ["8k-fast", "64k-full"])
def test_api_v1_qwen_missing_render_complete_bridge_fails_closed_without_chat_fallback(context_tier):
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "profile_id": "qwen3-8b-q4-k-m",
    }
    manager.context_tier = context_tier
    manager.context_window_tokens = 65536 if context_tier == "64k-full" else 8192
    manager.runtime.create_chat_completion.side_effect = AssertionError("chat path must not be used for qwen")
    manager.runtime.create_chat_completion_from_rendered_prompt = None
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id=f"req-qwen-missing-render-{context_tier}",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"max_tokens": 64, "stream": False},
        requested_context_tier=context_tier,
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_runtime_unavailable"
    assert error["internal_reason"] == "qwen_render_complete_bridge_unavailable"
    manager.runtime.create_chat_completion.assert_not_called()

def test_api_v1_qwen_render_then_complete_rejects_unproven_seed_option():
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion_from_rendered_prompt = MagicMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "ok"}}]
    })
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-seed",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"max_tokens": 64, "seed": 7},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert error["rejected_option"] == "seed"
    manager.runtime.create_chat_completion_from_rendered_prompt.assert_not_called()


def test_api_v1_qwen_render_then_complete_fails_closed_when_enable_thinking_rejected():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion_from_rendered_prompt = MagicMock(side_effect=[
        LlamaCppInferenceRequestError(
            "unexpected keyword argument 'enable_thinking'",
            diagnostics={
                "code": "compute_node_options_unsupported",
                "reason": "unsupported_generation_option",
                "rejected_option": "enable_thinking",
                "generation_exception_category": "unsupported_generation_kwarg",
                "method": "create_chat_completion_from_rendered_prompt",
            },
        ),
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-retry-rejected-option",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"max_tokens": 64},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] in {"compute_node_internal_error", "compute_node_runtime_unavailable"}
    assert error["internal_reason"] == "runtime_qwen_non_thinking_hard_switch_unavailable"
    calls = manager.runtime.create_chat_completion_from_rendered_prompt.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["enable_thinking"] is False
    assert "enable_thinking" not in client._api_v1_generation_kwargs_filtered



def test_api_v1_qwen_render_then_complete_does_not_send_unproven_top_k():
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    render_complete = MagicMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    })

    completion, diagnostics = client._api_v1_create_completion_from_rendered_prompt_filtered(
        render_complete,
        messages=[{"role": "user", "content": "hi"}],
        safe_options={"max_tokens": 64, "top_k": 20},
        model_profile={"provider": "qwen", "thinking_mode": "disabled"},
        client_option_names=set(),
    )

    assert completion["choices"][0]["message"]["content"] == "ok"
    render_complete.assert_called_once()
    assert render_complete.call_args.kwargs == {
        "max_tokens": 64,
        "token_place_provider": "qwen",
        "enable_thinking": False,
    }
    assert diagnostics["attempted_generation_kwargs"] == ["enable_thinking", "max_tokens", "token_place_provider"]


def test_api_v1_qwen_render_then_complete_empty_think_wrapper_is_cleaned():
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion_from_rendered_prompt = MagicMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "<think>\n\n</think>\n\nok<|im_end|>"}}]
    })
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-clean",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"max_tokens": 64},
    )

    assert envelope["api_v1_response"]["message"]["content"] == "ok"


def test_api_v1_qwen_render_then_complete_rejects_non_empty_thinking():
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion_from_rendered_prompt = MagicMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "<think>reasoning</think>\nok"}}]
    })
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-leak",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"max_tokens": 64},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, {"stream": False}),
        ({"max_tokens": 1}, {"max_tokens": 1}),
        ({"max_tokens": 8000}, {"max_tokens": 8000}),
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
        [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}],
        [{"role": "user", "content": [{"type": "text", "text": ""}]}],
        [{"role": "user", "content": [{"type": "text", "text": "x", "extra": "no"}]}],
        [{"role": "user", "content": "x"}] * (RelayClient._API_V1_MAX_MESSAGES + 1),
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


def test_api_v1_large_messages_reach_structural_validation_boundaries():
    limit = RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES

    for length in (32769, 65536, 131073, limit):
        messages = [{"role": "user", "content": "x" * length}]
        result = RelayClient._validate_api_v1_chat_messages(messages)
        assert result.valid is True
        assert RelayClient._messages_are_valid_api_v1_chat(messages) is True
        assert result.total_content_chars == length
        assert result.total_content_utf8_bytes == length

    too_large = RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": "x" * (limit + 1)}]
    )
    assert too_large.valid is False
    assert too_large.code == "compute_node_request_too_large"
    assert too_large.total_content_utf8_bytes == limit + 1
    assert RelayClient._messages_are_valid_api_v1_chat(
        [{"role": "user", "content": "x" * (limit + 1)}]
    ) is False


def test_api_v1_abuse_ceiling_uses_utf8_bytes_for_blocks_and_unicode():
    limit = RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES

    exact_blocks = RelayClient._validate_api_v1_chat_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a" * (limit // 2)},
                    {"type": "input_text", "text": "b" * (limit - (limit // 2))},
                ],
            }
        ]
    )
    assert exact_blocks.valid is True
    assert exact_blocks.total_content_utf8_bytes == limit

    one_over_blocks = RelayClient._validate_api_v1_chat_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a" * (limit // 2)},
                    {"type": "input_text", "text": "b" * (limit - (limit // 2) + 1)},
                ],
            }
        ]
    )
    assert one_over_blocks.code == "compute_node_request_too_large"
    assert one_over_blocks.total_content_utf8_bytes == limit + 1

    multibyte = "雪" * ((limit // len("雪".encode("utf-8"))) + 1)
    unicode_result = RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": multibyte}]
    )
    assert unicode_result.code == "compute_node_request_too_large"
    assert unicode_result.total_content_chars < limit
    assert unicode_result.total_content_utf8_bytes > limit


def test_api_v1_text_blocks_use_aggregate_limit_without_lower_message_cap():
    limit = RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES
    valid_blocks = [
        {"type": "text", "text": "a" * 20000},
        {"type": "input_text", "text": "b" * 20000},
    ]

    result = RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": valid_blocks}]
    )
    assert result.valid is True
    assert result.total_content_chars == 40000

    assert RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": [{"type": "text", "text": "x"}] * 33}]
    ).code == "compute_node_invalid_request"
    assert RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]
    ).code == "compute_node_invalid_request"
    assert RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": [{"type": "unknown", "text": "x"}]}]
    ).code == "compute_node_invalid_request"

    too_large = RelayClient._validate_api_v1_chat_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x" * limit},
                    {"type": "text", "text": "y"},
                ],
            }
        ]
    )
    assert too_large.valid is False
    assert too_large.code == "compute_node_request_too_large"


def test_api_v1_rejects_unpaired_surrogates_before_utf8_byte_counting():
    result = RelayClient._validate_api_v1_chat_messages(
        [{"role": "user", "content": "bad surrogate: \ud800"}]
    )
    assert result.valid is False
    assert result.code == "compute_node_invalid_request"
    assert result.reason == "invalid_content"

    block_result = RelayClient._validate_api_v1_chat_messages(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "bad surrogate: \ud800"}],
            }
        ]
    )
    assert block_result.valid is False
    assert block_result.code == "compute_node_invalid_request"
    assert block_result.reason == "invalid_content"

    input_text_block_result = RelayClient._validate_api_v1_chat_messages(
        [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "bad surrogate: \ud800"}],
            }
        ]
    )
    assert input_text_block_result.valid is False
    assert input_text_block_result.code == "compute_node_invalid_request"
    assert input_text_block_result.reason == "invalid_content"


def test_api_v1_unpaired_surrogate_full_path_fails_closed_without_plaintext(caplog):
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    sentinel = "PRIVATE_SURROGATE_SENTINEL"

    with caplog.at_level("ERROR", logger="relay_client"):
        envelope = client._generate_api_v1_response_with_runtime_model(
            request_id="req-invalid-surrogate",
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": sentinel + "\ud800"}],
            options={},
        )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_request"
    assert error["message"] == "Invalid chat message format"
    assert sentinel not in json.dumps(error)
    assert sentinel not in caplog.text
    assert "invalid_content" in caplog.text
    manager.runtime.render_and_tokenize_chat.assert_not_called()
    manager.runtime.apply_chat_template.assert_not_called()
    manager.runtime.tokenize.assert_not_called()
    manager.runtime.create_chat_completion.assert_not_called()


def test_api_v1_oversize_request_returns_specific_safe_error_and_logs_counts(caplog):
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    distinctive_text = "DISTINCTIVE_PRIVATE_PROMPT_TEXT"

    with caplog.at_level("ERROR", logger="relay_client"):
        envelope = client._generate_api_v1_response_with_runtime_model(
            request_id="req-too-large",
            model_id="llama-3-8b-instruct",
            messages=[
                {
                    "role": "user",
                    "content": distinctive_text
                    + ("x" * RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES),
                }
            ],
            options={},
        )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_request_too_large"
    assert error["type"] == "validation_error"
    assert error["message_count"] == 1
    assert error["maximum_total_content_chars"] == RelayClient._API_V1_MAX_TOTAL_REQUEST_CHARS
    assert error["total_content_utf8_bytes"] > RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES
    assert error["maximum_total_content_utf8_bytes"] == RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES
    assert error["retryable"] is False
    assert distinctive_text not in json.dumps(error)
    assert distinctive_text not in caplog.text
    assert "aggregate_content_too_large" in caplog.text
    manager.runtime.render_and_tokenize_chat.assert_not_called()
    manager.runtime.apply_chat_template.assert_not_called()
    manager.runtime.tokenize.assert_not_called()
    manager.runtime.create_chat_completion.assert_not_called()


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


class _AdmissionConfig:
    def __init__(self, max_tokens=16):
        self.max_tokens = max_tokens

    def get(self, key, default):
        return {
            "model.max_tokens": self.max_tokens,
            "model.temperature": 0.7,
            "model.top_p": 0.9,
            "model.stop_tokens": [],
        }.get(key, default)


class _AdmissionRuntime:
    def __init__(self):
        self.calls = []

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
        assert tokenize is False
        if 'enable_thinking' in kwargs:
            assert kwargs['enable_thinking'] is False, (
                f"Expected enable_thinking=False, got {kwargs['enable_thinking']}"
            )
        rendered = "<s>"
        for message in messages:
            content = message["content"]
            if isinstance(content, list):
                content = "".join(block["text"] for block in content)
            rendered += f"<{message['role']}>" + content
        if add_generation_prompt:
            rendered += "<assistant>"
        return rendered

    def tokenize(self, payload, *args):
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        token_count = len(payload)
        if "雪" in payload:
            token_count = 20 + ((len(payload) - 20) * 2)
        return list(range(token_count))

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def create_chat_completion_from_rendered_prompt(self, messages, **kwargs):
        return self.create_chat_completion(messages=messages, **kwargs)


class _AdmissionManager:
    api_model_id = "llama-3-8b-instruct"
    use_mock_llm = False

    def __init__(self, tier="8k-fast", window=8192, default_max_tokens=16):
        self.context_tier = tier
        self.context_window_tokens = window
        self.config = _AdmissionConfig(default_max_tokens)
        self.runtime = _AdmissionRuntime()
        self.worker_restart_count = 0

    def get_llm_instance(self):
        return self.runtime


def _admission_envelope(client, manager, content, *, options=None, requested_tier=None):
    client.model_manager = manager
    return client._generate_api_v1_response_with_runtime_model(
        request_id="req-admission",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": content}],
        options=options or {},
        requested_context_tier=requested_tier or manager.context_tier,
    )


def _large_natural_language_payload(target_bytes=248 * 1024):
    sentence = (
        b"A calm deterministic paragraph describes relay-safe token admission, "
        b"context windows, and neutral validation behavior for repeatable tests. "
    )
    repeats = (target_bytes // len(sentence)) + 1
    payload_bytes = (sentence * repeats)[:target_bytes]
    payload = payload_bytes.decode("ascii")
    assert len(payload_bytes) == target_bytes
    assert 240 * 1024 <= len(payload.encode("utf-8")) <= 260 * 1024
    assert len(payload) > RelayClient._API_V1_MAX_TOTAL_REQUEST_CHARS
    return payload


class _QwenLikeAdmissionRuntime(_AdmissionRuntime):
    def __init__(self, prompt_tokens):
        super().__init__()
        self.prompt_tokens = prompt_tokens
        self.render_and_tokenize_calls = []

    def render_and_tokenize_chat(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
        self.render_and_tokenize_calls.append(
            {
                "messages": messages,
                "tokenize": tokenize,
                "add_generation_prompt": add_generation_prompt,
                "kwargs": kwargs,
            }
        )
        return {"prompt_tokens": self.prompt_tokens}


class _QwenLikeAdmissionManager(_AdmissionManager):
    api_model_id = "qwen3-8b-instruct"

    def __init__(self, *, tier, window, prompt_tokens):
        super().__init__(tier=tier, window=window, default_max_tokens=512)
        self.model_profile = {
            "provider": "qwen",
            "thinking_mode": "disabled",
            "profile_id": "qwen3-8b-q4-k-m",
        }
        self.runtime = _QwenLikeAdmissionRuntime(prompt_tokens)


def _qwen_large_payload_envelope(manager, payload):
    client = _api_v1_validation_client(manager)
    return client._generate_api_v1_response_with_runtime_model(
        request_id="req-large-qwen",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": payload}],
        options={"max_tokens": 512, "stream": False},
        requested_context_tier=manager.context_tier,
    )


def test_api_v1_242kb_prompt_passes_abuse_validation_for_exact_admission():
    payload = _large_natural_language_payload()

    result = RelayClient._validate_api_v1_chat_messages([{"role": "user", "content": payload}])

    assert result.valid is True
    assert result.total_content_chars == len(payload)
    assert result.total_content_utf8_bytes == len(payload.encode("utf-8"))
    assert result.total_content_utf8_bytes > 131072
    assert result.total_content_utf8_bytes < RelayClient._API_V1_MAX_TOTAL_MESSAGE_UTF8_BYTES


def test_api_v1_large_qwen_prompt_reaches_exact_64k_admission_and_completes():
    payload = _large_natural_language_payload()
    manager = _QwenLikeAdmissionManager(tier="64k-full", window=65536, prompt_tokens=55229)

    envelope = _qwen_large_payload_envelope(manager, payload)

    assert manager.runtime.render_and_tokenize_calls
    assert 55229 + 512 <= manager.context_window_tokens
    assert envelope["api_v1_response"]["message"] == {"role": "assistant", "content": "ok"}
    assert "error" not in envelope["api_v1_response"]
    assert manager.runtime.calls
    assert manager.runtime.calls[-1]["max_tokens"] == 512


def test_api_v1_large_qwen_prompt_reaches_8k_admission_before_context_rejection():
    payload = _large_natural_language_payload()
    manager = _QwenLikeAdmissionManager(tier="8k-fast", window=8192, prompt_tokens=55229)

    envelope = _qwen_large_payload_envelope(manager, payload)

    assert manager.runtime.render_and_tokenize_calls
    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["prompt_tokens"] == 55229
    assert error["requested_output_tokens"] == 512
    assert error["required_total_tokens"] == 55741
    assert manager.runtime.calls == []


def test_api_v1_large_qwen_prompt_exact_64k_overflow_uses_context_error():
    payload = _large_natural_language_payload()
    manager = _QwenLikeAdmissionManager(tier="64k-full", window=65536, prompt_tokens=65025)

    envelope = _qwen_large_payload_envelope(manager, payload)

    assert manager.runtime.render_and_tokenize_calls
    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["required_total_tokens"] == 65537
    assert manager.runtime.calls == []


def test_api_v1_context_admission_includes_template_overhead_and_explicit_budget():
    manager = _AdmissionManager(window=32)
    client = _api_v1_validation_client(manager)
    # Rendered prompt is len("<s><user>" + content + "<assistant>") = 20 + content.
    accepted = _admission_envelope(client, manager, "x" * 7, options={"max_tokens": 5})
    rejected = _admission_envelope(client, manager, "x" * 8, options={"max_tokens": 5})

    assert "error" not in accepted["api_v1_response"]
    assert manager.runtime.calls[-1]["max_tokens"] == 5
    error = rejected["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["prompt_tokens"] == 28
    assert error["requested_output_tokens"] == 5
    assert error["required_total_tokens"] == 33


def test_api_v1_large_structurally_valid_message_uses_exact_tier_admission():
    large_content = "x" * 65000
    eight_k = _AdmissionManager(tier="8k-fast", window=8192, default_max_tokens=5)
    eight_k_client = _api_v1_validation_client(eight_k)

    rejected = _admission_envelope(eight_k_client, eight_k, large_content)
    error = rejected["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["prompt_tokens"] == 65020
    assert error["requested_output_tokens"] == 5
    assert error["required_total_tokens"] == 65025
    assert eight_k.runtime.calls == []

    sixty_four_k = _AdmissionManager(tier="64k-full", window=65536, default_max_tokens=5)
    sixty_four_k_client = _api_v1_validation_client(sixty_four_k)
    accepted = _admission_envelope(
        sixty_four_k_client, sixty_four_k, large_content, requested_tier="64k-full"
    )

    assert accepted["api_v1_response"]["message"] == {
        "role": "assistant",
        "content": "ok",
    }
    assert sixty_four_k.runtime.calls
    assert sixty_four_k.runtime.calls[-1]["max_tokens"] == 5


def test_api_v1_context_admission_uses_default_output_budget_for_omitted_max_tokens():
    manager = _AdmissionManager(window=32, default_max_tokens=4)
    client = _api_v1_validation_client(manager)

    accepted = _admission_envelope(client, manager, "x" * 8)
    rejected = _admission_envelope(client, manager, "x" * 9)

    assert "error" not in accepted["api_v1_response"]
    error = rejected["api_v1_response"]["error"]
    assert error["requested_output_tokens"] == 4
    assert error["prompt_tokens"] == 29


def test_api_v1_context_admission_uses_recovered_runtime_before_rejecting():
    manager = _AdmissionManager(window=64, default_max_tokens=4)
    manager.get_llm_instance = MagicMock(side_effect=[None])
    manager.get_llm_instance_with_recovery = MagicMock(return_value=manager.runtime)
    manager.create_chat_completion_with_recovery = MagicMock(
        return_value={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    )
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "hello", options={"max_tokens": 4})

    assert "error" not in envelope["api_v1_response"]
    manager.get_llm_instance_with_recovery.assert_called_once()
    manager.create_chat_completion_with_recovery.assert_called_once()
    assert manager.runtime.calls == []


def test_api_v1_context_admission_rejects_when_runtime_count_unavailable():
    manager = _AdmissionManager(window=32)
    manager.runtime.apply_chat_template = lambda *args, **kwargs: None
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x")

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_admission_unavailable"
    assert error["retryable"] is False
    assert manager.runtime.calls == []


def test_api_v1_context_admission_rejects_when_tokenizer_fallbacks_raise():
    manager = _AdmissionManager(window=32)

    def reject_all_tokenize_signatures(*args, **kwargs):
        raise TypeError("unsupported tokenizer signature")

    manager.runtime.tokenize = reject_all_tokenize_signatures
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x")

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_admission_unavailable"
    assert error["code"] != "compute_node_internal_error"
    assert error["retryable"] is False
    assert manager.runtime.calls == []


def test_api_v1_context_admission_rejects_when_chat_template_fallback_raises():
    manager = _AdmissionManager(window=32)

    def reject_all_template_signatures(*args, **kwargs):
        raise TypeError("unsupported chat template signature")

    manager.runtime.apply_chat_template = reject_all_template_signatures
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x")

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_admission_unavailable"
    assert error["code"] != "compute_node_internal_error"
    assert error["retryable"] is False
    assert manager.runtime.calls == []


def test_api_v1_context_admission_rejects_when_tokenizer_template_branch_raises():
    manager = _AdmissionManager(window=32)
    manager.runtime.apply_chat_template = None

    class RejectingTokenizerTemplate:
        def apply_chat_template(self, *args, **kwargs):
            raise RuntimeError("template render failed")

    manager.runtime.tokenizer = lambda: RejectingTokenizerTemplate()
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x")

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_admission_unavailable"
    assert error["code"] != "compute_node_internal_error"
    assert error["retryable"] is False
    assert manager.runtime.calls == []


def test_api_v1_context_admission_uses_llama_cpp_chat_format_fallback(monkeypatch):
    manager = _AdmissionManager(window=64)
    manager.runtime.apply_chat_template = None
    manager.runtime.chat_format = "llama-3"

    class FakeRendered:
        prompt = "<|start_header_id|>user<|end_header_id|>\n\nx<|eot_id|>"

    fake_chat_format_module = SimpleNamespace(
        format_llama3=MagicMock(return_value=FakeRendered())
    )
    original_import_module = relay_client_module.importlib.import_module

    def fake_import_module(name, *args, **kwargs):
        if name == "llama_cpp.llama_chat_format":
            return fake_chat_format_module
        return original_import_module(name, *args, **kwargs)

    monkeypatch.setattr(
        "utils.networking.relay_client.importlib.import_module",
        fake_import_module,
    )
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x", options={"max_tokens": 1})

    assert "error" not in envelope["api_v1_response"]
    fake_chat_format_module.format_llama3.assert_called_once_with(
        [{"role": "user", "content": "x"}],
        tokenize=False,
        add_generation_prompt=True,
    )
    assert manager.runtime.calls


def test_api_v1_chat_format_import_ignores_repo_local_llama_stub(monkeypatch):
    repo_llama_stub = SimpleNamespace(__file__=str(Path.cwd() / "llama_cpp.py"))
    fake_chat_format_module = SimpleNamespace(format_llama3=object())
    monkeypatch.setitem(sys.modules, "llama_cpp", repo_llama_stub)

    def fake_import_module(name, *args, **kwargs):
        assert name == "llama_cpp.llama_chat_format"
        assert sys.modules.get("llama_cpp") is not repo_llama_stub
        return fake_chat_format_module

    monkeypatch.setattr(
        "utils.networking.relay_client.importlib.import_module",
        fake_import_module,
    )

    assert RelayClient._api_v1_llama_chat_format_module() is fake_chat_format_module
    assert sys.modules["llama_cpp"] is repo_llama_stub


def test_api_v1_64k_request_on_8k_runtime_reports_exact_admission_counts():
    manager = _AdmissionManager(tier="8k-fast", window=8192, default_max_tokens=4)
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(
        client,
        manager,
        "x" * 8170,
        options={"max_tokens": 3},
        requested_tier="64k-full",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["active_context_tier"] == "8k-fast"
    assert error["configured_context_tokens"] == 8192
    assert error["prompt_tokens"] == 8190
    assert error["requested_output_tokens"] == 3
    assert error["required_total_tokens"] == 8193
    assert error["recommended_context_tier"] == "64k-full"
    assert error["retryable"] is True
    assert manager.runtime.calls == []


def test_api_v1_64k_request_on_8k_runtime_reports_tier_unsupported_when_it_fits():
    manager = _AdmissionManager(tier="8k-fast", window=8192, default_max_tokens=4)
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(
        client,
        manager,
        "small",
        options={"max_tokens": 10},
        requested_tier="64k-full",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_tier_unsupported"
    assert error["message"] == "Requested context tier is not active on this compute node"
    assert error["active_context_tier"] == "8k-fast"
    assert error["requested_context_tier"] == "64k-full"
    assert error["configured_context_tokens"] == 8192
    assert error["prompt_tokens"] == 25
    assert error["requested_output_tokens"] == 10
    assert error["required_total_tokens"] == 35
    assert error["retryable"] is False
    assert "recommended_context_tier" not in error
    assert manager.runtime.calls == []


def test_api_v1_context_admission_exact_64k_boundaries_and_unicode_structured_text():
    manager = _AdmissionManager(tier="64k-full", window=65536, default_max_tokens=1)
    client = _api_v1_validation_client(manager)
    content = [{"type": "text", "text": "雪" * 32757}]
    accepted = _admission_envelope(client, manager, content, requested_tier="8k-fast")
    rejected = _admission_envelope(client, manager, [{"type": "input_text", "text": "雪" * 32758}])

    assert "error" not in accepted["api_v1_response"]
    error = rejected["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["active_context_tier"] == "64k-full"
    assert error["configured_context_tokens"] == 65536
    assert error["prompt_tokens"] == 65536
    assert error["requested_output_tokens"] == 1
    assert error["retryable"] is False


def test_api_v1_context_overflow_is_request_scoped_and_does_not_restart_worker():
    manager = _AdmissionManager(window=24, default_max_tokens=5)
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x" * 10)

    assert envelope["api_v1_response"]["error"]["code"] == "compute_node_context_window_exceeded"
    assert manager.worker_restart_count == 0
    assert manager.runtime.calls == []


def test_api_v1_context_admission_recommends_64k_for_8k_overflow():
    manager = _AdmissionManager(tier="8k-fast", window=8192, default_max_tokens=1)
    client = _api_v1_validation_client(manager)

    envelope = _admission_envelope(client, manager, "x" * 8172)

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_window_exceeded"
    assert error["recommended_context_tier"] == "64k-full"
    assert error["retryable"] is True


def test_api_v1_stop_prevents_racing_start_from_leaving_live_heartbeat():
    relay_client = _api_v1_validation_client()
    relay_client._api_v1_registered_relays.add(relay_client.relay_url)
    relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = time.monotonic()
    relay_client._api_v1_start_heartbeat_worker()
    original = relay_client._api_v1_heartbeat_thread
    assert original is not None

    entered_join = threading.Event()
    release_join = threading.Event()
    original_join = original.join

    def blocking_join(timeout=None):
        entered_join.set()
        release_join.wait(timeout=1.0)
        return original_join(timeout=timeout)

    original.join = blocking_join
    stopper = threading.Thread(target=relay_client._api_v1_stop_heartbeat_worker)
    stopper.start()
    assert entered_join.wait(timeout=1.0)

    relay_client._api_v1_start_heartbeat_worker()
    release_join.set()
    stopper.join(timeout=2.0)

    assert not stopper.is_alive()
    assert relay_client._api_v1_heartbeat_thread is None

def test_api_v1_heartbeat_worker_refreshes_during_blocked_inference():
    relay_client = _api_v1_validation_client()
    relay_client._api_v1_registered_relays.add(relay_client.relay_url)
    relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = 0.0
    relay_client._api_v1_relay_wait_hints = {
        relay_client.relay_url: {"next_ping_in_x_seconds": 1, "poll_wait_seconds": 1}
    }
    calls = []

    def register(url):
        calls.append(url)
        relay_client.stop()
        return {"next_ping_in_x_seconds": 1, "poll_wait_seconds": 1}

    relay_client.register_api_v1_compute_node = register
    relay_client._api_v1_start_heartbeat_worker()
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    relay_client.stop()

    assert calls == [relay_client.relay_url]
    assert relay_client._api_v1_heartbeat_thread is None


def test_api_v1_heartbeat_stops_when_response_posting_raises():
    manager = _AdmissionManager()
    client = _api_v1_validation_client(manager)
    client._api_v1_registered_relays.add(client.relay_url)
    client.crypto_manager.decrypt_message.return_value = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-post-raises",
        "client_public_key": TEST_VALID_RESPONSE["client_public_key"],
        "api_v1_request": {
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "Hello"}],
            "options": {},
            "routing": {"context_tier": "8k-fast"},
        },
    }
    client._post_api_v1_response = MagicMock(side_effect=RuntimeError("post failed"))

    result = client.process_client_request_result(TEST_VALID_RESPONSE.copy())

    assert result.submitted is False
    assert client._api_v1_heartbeat_thread is None


def test_api_v1_request_heartbeat_teardown_does_not_latch_global_polling():
    manager = _ApiV1RuntimeManager()
    client = _api_v1_validation_client(manager)
    client.stop_polling = False
    client._api_v1_registered_relays.add(client.relay_url)
    client._api_v1_last_heartbeat_at[client.relay_url] = time.monotonic()
    client.crypto_manager.decrypt_message.side_effect = [
        _api_v1_decrypted_payload(request_id="req-heartbeat-1"),
        _api_v1_decrypted_payload(request_id="req-heartbeat-2"),
    ]
    client.crypto_manager.encrypt_message.return_value = {
        "chat_history": "encrypted_chat_history",
        "cipherkey": "encrypted_key",
        "iv": "encrypted_iv",
    }
    client._post_api_v1_response = MagicMock(return_value=True)

    first_result = client.process_client_request_result(TEST_VALID_RESPONSE.copy())

    assert first_result.submitted is True
    assert client._api_v1_heartbeat_thread is None
    assert client.stop_polling is False
    assert client._polling_stopped_by_request is False

    second_result = client.process_client_request_result(TEST_VALID_RESPONSE.copy())

    assert second_result.submitted is True
    assert client.stop_polling is False
    assert client._polling_stopped_by_request is False
    assert client._post_api_v1_response.call_count == 2


def test_api_v1_heartbeat_logs_sanitized_relay_targets():
    relay_client = _api_v1_validation_client()
    relay_url = "https://user:secret@example.test:443/path?token=abc#frag"
    relay_client._api_v1_registered_relays.add(relay_url)
    relay_client._api_v1_last_heartbeat_at[relay_url] = 0.0
    relay_client._api_v1_relay_wait_hints = {
        relay_url: {"next_ping_in_x_seconds": 1, "poll_wait_seconds": 1}
    }
    logged = []

    def register(url):
        relay_client.stop()
        return {"next_ping_in_x_seconds": 1, "poll_wait_seconds": 1}

    relay_client.register_api_v1_compute_node = register
    with patch("utils.networking.relay_client.log_info") as mock_log_info:
        mock_log_info.side_effect = lambda message, *args: logged.append(message.format(*args))
        relay_client._api_v1_start_heartbeat_worker()
        deadline = time.monotonic() + 2.0
        while relay_client._api_v1_heartbeat_thread is not None and time.monotonic() < deadline:
            time.sleep(0.01)

    joined = "\n".join(logged)
    assert "https://example.test" in joined
    assert "secret" not in joined
    assert "token=abc" not in joined


def test_api_v1_stop_unregister_terminates_heartbeat_cleanly():
    relay_client = _api_v1_validation_client()
    relay_client._api_v1_registered_relays.add(relay_client.relay_url)
    relay_client._api_v1_last_heartbeat_at[relay_client.relay_url] = time.monotonic()
    relay_client._api_v1_start_heartbeat_worker()
    assert relay_client._api_v1_heartbeat_thread is not None

    relay_client.stop()

    assert relay_client._api_v1_heartbeat_thread is None


def test_qwen_context_admission_preserves_messages_without_no_think_injection():
    class Runtime(_AdmissionRuntime):
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
            self.calls.append({'template_kwargs': kwargs, 'messages': messages})
            return super().apply_chat_template(messages, tokenize=tokenize, add_generation_prompt=add_generation_prompt)
    manager = _AdmissionManager(window=128)
    manager.api_model_id = 'qwen3-8b-instruct'
    manager.model_profile = {'provider': 'qwen', 'thinking_mode': 'disabled'}
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id='req-qwen-template',
        model_id='qwen3-8b-instruct',
        messages=[{'role': 'user', 'content': 'hello'}],
        options={'max_tokens': 5},
        requested_context_tier='8k-fast',
    )

    assert envelope['api_v1_response']['message']['content'] == 'ok'
    assert manager.runtime.calls[0]['template_kwargs'] == {'enable_thinking': False}
    assert manager.runtime.calls[0]['messages'][-1]['content'] == 'hello'
    assert manager.runtime.calls[-1]['messages'][-1]['content'] == 'hello'


def test_qwen_context_admission_uses_packaged_render_tokenize_bridge():
    class Runtime(_AdmissionRuntime):
        def render_and_tokenize_chat(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
            self.calls.append({
                'bridge': 'render_and_tokenize_chat',
                'messages': messages,
                'template_kwargs': kwargs,
            })
            assert tokenize is False
            assert add_generation_prompt is True
            return {'prompt_tokens': 7}

        def apply_chat_template(self, *args, **kwargs):  # pragma: no cover - should not be used
            raise AssertionError('legacy parent render path should not be used')

    manager = _AdmissionManager(window=128)
    manager.api_model_id = 'qwen3-8b-instruct'
    manager.model_profile = {'provider': 'qwen', 'thinking_mode': 'disabled'}
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id='req-qwen-bridge',
        model_id='qwen3-8b-instruct',
        messages=[{'role': 'user', 'content': 'hello'}],
        options={'max_tokens': 5},
        requested_context_tier='8k-fast',
    )

    assert envelope['api_v1_response']['message']['content'] == 'ok'
    assert manager.runtime.calls[0]['bridge'] == 'render_and_tokenize_chat'
    assert manager.runtime.calls[0]['messages'][-1]['content'] == 'hello'
    assert manager.runtime.calls[0]['template_kwargs'] == {
        'token_place_provider': 'qwen',
        'enable_thinking': False,
    }


def test_qwen_no_think_messages_preserves_content_blocks_without_injection():
    prepared = RelayClient._api_v1_qwen_no_think_messages([
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,...'}}]},
    ])

    assert prepared[0]['content'][0]['type'] == 'image_url'
    assert all(block.get('text') != '/no_think\n' for block in prepared[0]['content'] if isinstance(block, dict))


def test_qwen_render_returns_none_when_enable_thinking_typeerror_would_drop_control():
    calls = []

    class Runtime:
        def apply_chat_template(self, messages, **kwargs):
            calls.append(dict(kwargs))
            assert 'enable_thinking' in kwargs, (
                'enable_thinking was omitted from kwargs before raising TypeError'
            )
            assert kwargs['enable_thinking'] is False
            raise TypeError('unexpected keyword argument enable_thinking')

    assert RelayClient._api_v1_render_chat_prompt(
        Runtime(),
        [{'role': 'user', 'content': 'hello'}],
        enable_thinking=False,
        allow_chat_format_fallback=False,
    ) is None
    assert calls == [{'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False}]


def test_qwen_tokenizer_render_returns_none_when_enable_thinking_typeerror_would_drop_control(monkeypatch):
    calls = []

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            calls.append(dict(kwargs))
            assert 'enable_thinking' in kwargs, (
                'enable_thinking was omitted from kwargs before raising TypeError'
            )
            assert kwargs['enable_thinking'] is False
            raise TypeError('unexpected keyword argument enable_thinking')

    class Runtime:
        chat_format = 'llama-3'

        def tokenizer(self):
            return Tokenizer()

    class ChatFormatModule:
        @staticmethod
        def format_llama3(*args, **kwargs):  # pragma: no cover - should never be used
            raise AssertionError('chat_format fallback should not run for hard non-thinking render requests')

    monkeypatch.setattr(
        RelayClient,
        '_api_v1_llama_chat_format_module',
        staticmethod(lambda: ChatFormatModule),
    )

    assert RelayClient._api_v1_render_chat_prompt(
        Runtime(),
        [{'role': 'user', 'content': 'hello'}],
        enable_thinking=False,
        allow_chat_format_fallback=True,
    ) is None
    assert calls == [{'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False}]


def test_qwen_context_admission_unavailable_when_template_missing_no_llama_fallback():
    class Runtime:
        chat_format = 'llama-3'
        def tokenize(self, payload, *args):
            return [1]
        def create_chat_completion(self, **kwargs):
            return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
    manager = _AdmissionManager(window=128)
    manager.api_model_id = 'qwen3-8b-instruct'
    manager.model_profile = {'provider': 'qwen', 'thinking_mode': 'disabled'}
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id='req-qwen-template-missing',
        model_id='qwen3-8b-instruct',
        messages=[{'role': 'user', 'content': 'hello'}],
        options={'max_tokens': 5},
        requested_context_tier='8k-fast',
    )

    assert envelope['api_v1_response']['error']['code'] == 'compute_node_context_admission_unavailable'


def test_qwen_context_admission_fails_closed_when_fallback_render_rejects_enable_thinking():
    class Runtime(_AdmissionRuntime):
        def __init__(self):
            super().__init__()
            self.render_calls = []

        def render_and_tokenize_chat(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
            self.render_calls.append({'bridge': kwargs, 'messages': messages})
            return None

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
            self.calls.append({'template_kwargs': kwargs, 'messages': messages})
            assert kwargs['enable_thinking'] is False
            raise TypeError('unexpected keyword argument enable_thinking')

        def create_chat_completion(self, **kwargs):  # pragma: no cover - admission fails first
            raise AssertionError('generation should not run when admission cannot preserve enable_thinking=False')

    manager = _AdmissionManager(window=128)
    manager.api_model_id = 'qwen3-8b-instruct'
    manager.model_profile = {
        'id': 'qwen3-local',
        'provider': 'qwen',
        'thinking_mode': 'disabled',
        'chat_template_policy': 'qwen3-no-think',
    }
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id='req-qwen-fallback-hard-switch',
        model_id='qwen3-8b-instruct',
        messages=[{'role': 'user', 'content': 'hello'}],
        options={'max_tokens': 5},
        requested_context_tier='8k-fast',
    )

    error = envelope['api_v1_response']['error']
    assert error['code'] == 'compute_node_context_admission_unavailable'
    assert error['internal_reason'] == 'runtime_qwen_non_thinking_hard_switch_unavailable'
    assert error['rejected_generation_kwarg'] == 'enable_thinking'
    assert error['generation_exception_category'] == 'qwen_non_thinking_hard_switch_unavailable'
    assert error['method'] == 'apply_chat_template'
    assert error['retryable'] is False
    assert manager.runtime.render_calls[0]['bridge']['enable_thinking'] is False
    assert manager.runtime.render_calls[0]['messages'][-1]['content'] == 'hello'
    assert manager.runtime.calls[0]['template_kwargs'] == {'enable_thinking': False}
    serialized = json.dumps(error, sort_keys=True)
    assert '/no_think' not in serialized


def test_api_v1_qwen_paths_never_send_enable_thinking_true():
    class Runtime(_AdmissionRuntime):
        def __init__(self):
            super().__init__()
            self.render_calls = []

        def render_and_tokenize_chat(self, messages, tokenize=False, add_generation_prompt=True, **kwargs):
            self.render_calls.append(kwargs)
            assert kwargs['enable_thinking'] is False
            return {'prompt_tokens': 7}

        def create_chat_completion_from_rendered_prompt(self, messages, **kwargs):
            self.calls.append(kwargs)
            assert kwargs['enable_thinking'] is False
            return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}

    manager = _AdmissionManager(window=128)
    manager.api_model_id = 'qwen3-8b-instruct'
    manager.model_profile = {'provider': 'qwen', 'thinking_mode': 'disabled'}
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id='req-qwen-never-true',
        model_id='qwen3-8b-instruct',
        messages=[{'role': 'user', 'content': 'hello'}],
        options={'max_tokens': 5},
        requested_context_tier='8k-fast',
    )

    assert envelope['api_v1_response']['message']['content'] == 'ok'
    sent_values = [
        kwargs['enable_thinking']
        for kwargs in manager.runtime.render_calls + manager.runtime.calls
        if 'enable_thinking' in kwargs
    ]
    assert sent_values
    assert sent_values == [False, False]




@pytest.mark.parametrize(
    "content,expected",
    [
        ("ready", "ready"),
        (" <think></think> ready ", "ready"),
        ("<think>\n\n</think>\n\nok", "ok"),
        ("<THINK>   </THINK> final", "final"),
        ("<think></think><think> </think> answer", "answer"),
    ],
)
def test_qwen_non_thinking_normalizer_strips_only_empty_leading_wrappers(content, expected):
    cleaned, reason = RelayClient._api_v1_normalize_qwen_non_thinking_content(
        {"provider": "qwen", "thinking_mode": "disabled"}, content
    )
    assert cleaned == expected
    assert reason is None


@pytest.mark.parametrize(
    "content,reason",
    [
        ("<think>I am reasoning</think> answer", "qwen_thinking_output_leaked"),
        ("answer <think></think>", "qwen_thinking_output_leaked"),
        ("<think partial", "qwen_thinking_output_leaked"),
        ("<think></think>", "qwen_empty_after_think_wrapper_strip"),
    ],
)
def test_qwen_non_thinking_normalizer_rejects_reasoning_and_empty_output(content, reason):
    cleaned, actual_reason = RelayClient._api_v1_normalize_qwen_non_thinking_content(
        {"provider": "qwen", "thinking_mode": "disabled"}, content
    )
    assert cleaned is None
    assert actual_reason == reason


def test_assistant_message_extraction_strips_empty_qwen_think_wrapper_from_message():
    manager = _AdmissionManager(window=128)
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    client = _api_v1_validation_client(manager)

    message = client._assistant_message_from_runtime_completion(
        {"choices": [{"message": {"content": "<think>\n\n</think>\n\nok"}}]}
    )

    assert message == {"role": "assistant", "content": "ok"}


def test_assistant_message_extraction_strips_empty_qwen_think_wrapper_from_text_choice():
    manager = _AdmissionManager(window=128)
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    client = _api_v1_validation_client(manager)

    message = client._assistant_message_from_runtime_completion(
        {"choices": [{"text": "<think></think> final"}]}
    )

    assert message == {"role": "assistant", "content": "final"}


def test_assistant_message_extraction_preserves_tool_call_only_message():
    manager = _AdmissionManager(window=128)
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    client = _api_v1_validation_client(manager)
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]

    message = client._assistant_message_from_runtime_completion(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    }
                }
            ]
        }
    )

    assert message == {"role": "assistant", "content": None, "tool_calls": tool_calls}
    assert client._last_api_v1_invalid_model_output_reason is None


def test_assistant_message_extraction_preserves_tool_calls_when_normalizing_content():
    manager = _AdmissionManager(window=128)
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    client = _api_v1_validation_client(manager)
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]

    message = client._assistant_message_from_runtime_completion(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think></think> final",
                        "tool_calls": tool_calls,
                    }
                }
            ]
        }
    )

    assert message == {"role": "assistant", "content": "final", "tool_calls": tool_calls}
    assert client._last_api_v1_invalid_model_output_reason is None


def test_assistant_message_extraction_allows_non_think_xml_like_content():
    manager = _AdmissionManager(window=128)
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    client = _api_v1_validation_client(manager)

    message = client._assistant_message_from_runtime_completion(
        {"choices": [{"message": {"role": "assistant", "content": "<note>ordinary</note>"}}]}
    )

    assert message == {"role": "assistant", "content": "<note>ordinary</note>"}
    assert client._last_api_v1_invalid_model_output_reason is None


def test_qwen_think_output_is_rejected():
    manager = _AdmissionManager(window=128)
    manager.api_model_id = 'qwen3-8b-instruct'
    manager.model_profile = {'provider': 'qwen', 'thinking_mode': 'disabled'}
    manager.runtime.create_chat_completion = lambda **kwargs: {
        'choices': [{'message': {'role': 'assistant', 'content': '<think>secret</think>answer'}}]
    }
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id='req-qwen-think',
        model_id='qwen3-8b-instruct',
        messages=[{'role': 'user', 'content': 'hello'}],
        options={'max_tokens': 5},
        requested_context_tier='8k-fast',
    )

    assert envelope['api_v1_response']['error']['code'] == 'compute_node_invalid_model_output'
    error = envelope['api_v1_response']['error']
    assert error['request_id'] == 'req-qwen-think'
    assert error['internal_reason'] == 'qwen_thinking_output_leaked'
    assert error['active_context_tier'] == '8k-fast'
    assert error['requested_context_tier'] == '8k-fast'
    assert error['configured_context_tokens'] == 128
    assert isinstance(error['prompt_tokens'], int)
    assert error['prompt_tokens'] > 0
    assert error['requested_output_tokens'] == 5
    assert error['runtime_healthy'] is True
    assert error['recovery_attempted'] is False
    assert error['recovery_succeeded'] is False
    assert 'secret' not in json.dumps(error)


@pytest.mark.parametrize(
    "leaked_content",
    [
        "<think>secret</think>answer",
        "<THINK>secret</THINK>answer",
        "   <think>secret</think>answer",
        "< think>secret</ think>answer",
        "<think secret partial",
        "</think>answer",
    ],
)
def test_qwen_think_output_variants_are_rejected_with_safe_reason(leaked_content):
    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion = lambda **kwargs: {
        "choices": [{"message": {"role": "assistant", "content": leaked_content}}]
    }
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-think-variant",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"
    assert "secret" not in json.dumps(error)


def test_qwen_reasoning_content_field_is_rejected_with_safe_reason():
    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion = lambda **kwargs: {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "final answer",
                    "reasoning_content": "secret hidden reasoning",
                }
            }
        ]
    }
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-reasoning-content",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"
    assert "secret hidden reasoning" not in json.dumps(error)

def test_qwen_top_level_reasoning_content_field_is_rejected_with_safe_reason():
    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    leaked_reasoning = "top level secret reasoning"
    manager.runtime.create_chat_completion = lambda **kwargs: {
        "reasoning_content": leaked_reasoning,
        "choices": [{"message": {"role": "assistant", "content": "final answer"}}],
    }
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-top-level-reasoning",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    response_json = json.dumps(envelope["api_v1_response"], sort_keys=True)
    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"
    assert leaked_reasoning not in response_json


def test_qwen_nested_reasoning_field_outside_first_choice_is_rejected_with_safe_reason():
    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    leaked_reasoning = "nested secret reasoning"
    manager.runtime.create_chat_completion = lambda **kwargs: {
        "choices": [{"message": {"role": "assistant", "content": "final answer"}}],
        "usage": {"diagnostics": [{"reasoning": leaked_reasoning}]},
    }
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-nested-reasoning",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    response_json = json.dumps(envelope["api_v1_response"], sort_keys=True)
    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"
    assert leaked_reasoning not in response_json


@pytest.mark.parametrize(
    ("field_name", "field_value", "completion_patch"),
    [
        ("reasoning_content", "", {"reasoning_content": ""}),
        ("reasoning", None, {"reasoning": None}),
        (
            "reasoning_content",
            "",
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "final answer",
                            "reasoning_content": "",
                        }
                    }
                ]
            },
        ),
        (
            "reasoning",
            None,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "final answer",
                            "reasoning": None,
                        }
                    }
                ]
            },
        ),
        ("reasoning_content", "", {"usage": {"details": [{"reasoning_content": ""}]}}),
        ("reasoning", None, {"usage": {"details": [{"reasoning": None}]}}),
    ],
)
def test_qwen_reasoning_field_presence_is_rejected_even_when_empty_or_none(
    field_name, field_value, completion_patch
):
    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_text = "final answer"
    base_completion = {"choices": [{"message": {"role": "assistant", "content": model_text}}]}
    completion = {**base_completion, **completion_patch}
    manager.runtime.create_chat_completion = lambda **kwargs: completion
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-field-present",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    response_json = json.dumps(envelope["api_v1_response"], sort_keys=True)
    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"
    assert model_text not in response_json
    assert field_name not in response_json
    if field_value not in (None, ""):
        assert str(field_value) not in response_json


def test_api_v1_qwen_non_thinking_policy_is_single_source_of_truth():
    policy = RelayClient._API_V1_QWEN_NON_THINKING_POLICY

    assert policy["thinking_mode"] == "disabled"
    assert policy["message_control"] == "/no_think"
    assert policy["visible_think_output_forbidden"] is True
    assert policy["reasoning_content_forbidden"] is True
    assert RelayClient._api_v1_qwen_non_thinking_required(
        {"provider": "qwen", "thinking_mode": "disabled"}
    )


def test_qwen_profile_generation_defaults_include_top_k():
    manager = _AdmissionManager()
    manager.model_profile = {'generation_defaults': {'temperature': 0.7, 'top_p': 0.8, 'top_k': 20}}
    client = _api_v1_validation_client(manager)

    kwargs = client._api_v1_runtime_completion_kwargs({})

    assert kwargs['temperature'] == 0.7
    assert kwargs['top_p'] == 0.8
    assert kwargs['top_k'] == 20


def test_qwen_render_tokenize_worker_diagnostics_propagate_to_admission_error():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    class Runtime:
        def __init__(self):
            self.calls = []

        def render_and_tokenize_chat(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "reason": "runtime_chat_template_metadata_missing",
                    "method": "unsupported",
                    "stream": False,
                    "exception_type": "RuntimeError",
                    "unsafe_prompt": "do not expose me",
                    "nested": {"message": "do not expose me"},
                },
            )

        def create_chat_completion(self, **kwargs):  # pragma: no cover - admission fails first
            raise AssertionError("generation should not run when admission is unavailable")

    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {
        "id": "qwen3-local",
        "provider": "qwen",
        "thinking_mode": "disabled",
        "chat_template_policy": "qwen3-no-think",
    }
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-worker-diagnostics",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello diagnostic secret"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_context_admission_unavailable"
    assert error["internal_reason"] == "runtime_chat_template_metadata_missing"
    assert error["active_model_id"] == "qwen3-8b-instruct"
    assert error["active_profile_id"] == "qwen3-local"
    assert error["context_tier"] == "8k-fast"
    assert error["template_policy"] == "qwen3-no-think"
    assert error["non_thinking_mode"] is True
    assert error["runtime_facade_type"] == "Runtime"
    assert error["direct_apply_chat_template_available"] is False
    assert error["metadata_template_available"] is False
    assert error["jinja_renderer_available"] is True
    assert manager.runtime._token_place_last_render_tokenize_error["reason"] == "runtime_chat_template_metadata_missing"
    serialized = json.dumps(error, sort_keys=True)
    assert "hello diagnostic secret" not in serialized
    assert "do not expose me" not in serialized
    assert "unsafe_prompt" not in serialized


def test_qwen_render_tokenize_multimodal_rejection_surfaces_invalid_request():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    class Runtime:
        def render_and_tokenize_chat(self, messages, **kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "code": "compute_node_invalid_request",
                    "reason": "runtime_text_only_content_blocks_required",
                    "generation_exception_category": "text_only_content_blocks_required",
                    "unsafe_prompt": "do not expose me",
                },
            )

        def create_chat_completion(self, **kwargs):  # pragma: no cover - admission fails first
            raise AssertionError("generation should not run when admission rejects the request")

    manager = _AdmissionManager(window=128)
    manager.api_model_id = "qwen3-8b-instruct"
    manager.model_profile = {
        "id": "qwen3-local",
        "provider": "qwen",
        "thinking_mode": "disabled",
        "chat_template_policy": "qwen3-no-think",
    }
    manager.runtime = Runtime()
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-multimodal-invalid",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello diagnostic secret"}],
        options={"max_tokens": 5},
        requested_context_tier="8k-fast",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_request"
    assert error["internal_reason"] == "runtime_text_only_content_blocks_required"
    assert error["message"] == (
        "API v1 chat completions are text-only and do not support non-text content blocks. "
        "Use text-only messages or API v2 for multimodal requests."
    )
    assert "unsafe_prompt" not in json.dumps(error, sort_keys=True)


def test_render_tokenize_success_clears_stale_worker_diagnostics():
    class Runtime:
        def __init__(self):
            self._token_place_last_render_tokenize_error = {
                "reason": "runtime_chat_template_metadata_missing"
            }

        def render_and_tokenize_chat(self, messages, **kwargs):
            return {"prompt_tokens": 3}

    runtime = Runtime()

    prompt_tokens = RelayClient._api_v1_render_and_tokenize_chat_prompt(
        runtime,
        [{"role": "user", "content": "hello"}],
        model_profile={"provider": "qwen", "chat_template_policy": "qwen3-no-think"},
    )

    assert prompt_tokens == 3
    assert not hasattr(runtime, "_token_place_last_render_tokenize_error")


def test_api_v1_qwen_completion_text_fallback_converts_to_assistant_message():
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion.return_value = {"choices": [{"text": "Qwen ok"}]}
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-text",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    assert envelope["api_v1_response"]["message"] == {"role": "assistant", "content": "Qwen ok"}


def test_api_v1_qwen_message_without_role_converts_to_assistant_message():
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion.return_value = {"choices": [{"message": {"content": "Qwen ok"}}]}
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-qwen-no-role",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    assert envelope["api_v1_response"]["message"] == {"role": "assistant", "content": "Qwen ok"}


def test_api_v1_qwen_thinking_output_is_rejected_with_safe_reason(caplog):
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "<think>secret reasoning</think> final"}}]
    }
    client = _api_v1_validation_client(manager)

    with caplog.at_level("ERROR", logger="relay_client"):
        envelope = client._generate_api_v1_response_with_runtime_model(
            request_id="req-qwen-think",
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
            options={},
        )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["request_id"] == "req-qwen-think"
    assert error["internal_reason"] == "qwen_thinking_output_leaked"
    assert "secret reasoning" not in caplog.text
    assert "qwen_thinking_output_leaked" in caplog.text


def test_api_v1_malformed_qwen_completion_reports_safe_shape_not_content(caplog):
    manager = _ApiV1RuntimeManager()
    manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    manager.runtime.create_chat_completion.return_value = {
        "choices": [{"message": {"role": "assistant", "content": ["SECRET"]}}]
    }
    client = _api_v1_validation_client(manager)

    with caplog.at_level("ERROR", logger="relay_client"):
        envelope = client._generate_api_v1_response_with_runtime_model(
            request_id="req-qwen-bad",
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
            options={},
        )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "unsupported_completion_shape"
    assert error["request_id"] == "req-qwen-bad"
    assert error["active_context_tier"] == "8k-fast"
    assert error["requested_context_tier"] == "8k-fast"
    assert error["configured_context_tokens"] == 8192
    assert isinstance(error["prompt_tokens"], int)
    assert error["prompt_tokens"] > 0
    assert error["requested_output_tokens"] == 512
    assert error["runtime_healthy"] is True
    assert error["recovery_attempted"] is False
    assert error["recovery_succeeded"] is False
    assert "SECRET" not in json.dumps(error)
    assert "SECRET" not in caplog.text
    assert "message_content_type" in caplog.text


def test_api_v1_runtime_rejected_generation_option_is_safe_options_error():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    manager = _ApiV1RuntimeManager()
    manager.runtime.create_chat_completion.side_effect = LlamaCppInferenceRequestError(
        "unexpected keyword argument 'top_k'",
        diagnostics={
            "code": "compute_node_options_unsupported",
            "reason": "unsupported_generation_option",
            "rejected_option": "top_k",
        },
    )
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-option-rejected",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert error["request_id"] == "req-option-rejected"
    assert error["internal_reason"] == "unsupported_generation_option"
    assert error["rejected_option"] == "top_k"
    assert error["active_context_tier"] == "8k-fast"
    assert error["requested_context_tier"] == "8k-fast"
    assert error["configured_context_tokens"] == 8192
    assert isinstance(error["prompt_tokens"], int)
    assert error["prompt_tokens"] > 0
    assert error["requested_output_tokens"] == 512
    assert error["runtime_healthy"] is True
    assert error["recovery_attempted"] is False
    assert error["recovery_succeeded"] is False


def test_api_v1_runtime_rejected_generation_option_filters_worker_diagnostics():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    manager = _ApiV1RuntimeManager()
    manager.runtime.create_chat_completion.side_effect = LlamaCppInferenceRequestError(
        "unexpected keyword argument 'top_k'",
        diagnostics={
            "code": "compute_node_options_unsupported",
            "reason": "unsupported_generation_option",
            "rejected_option": "top_k",
            "generation_exception_category": "unsupported_generation_kwarg",
            "exception_type": "TypeError",
            "method": "create_chat_completion",
            "prompt": "SECRET prompt text",
            "raw_message": "SECRET raw message",
            "assistant_text": "SECRET assistant text",
            "unsafe_prompt": "SECRET prompt text",
            "long_summary": "x" * 1000,
            "nested": {"rendered_prompt": "SECRET rendered prompt"},
        },
    )
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-option-rejected-safe",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert error["internal_reason"] == "unsupported_generation_option"
    assert error["worker_diagnostics"] == {
        "code": "compute_node_options_unsupported",
        "reason": "unsupported_generation_option",
        "rejected_option": "top_k",
        "generation_exception_category": "unsupported_generation_kwarg",
        "exception_type": "TypeError",
        "method": "create_chat_completion",
    }
    assert "SECRET" not in json.dumps(error)


def test_api_v1_runtime_rejected_generation_option_drops_unsafe_allowed_values():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    manager = _ApiV1RuntimeManager()
    manager.runtime.create_chat_completion.side_effect = LlamaCppInferenceRequestError(
        "worker rejected request",
        diagnostics={
            "generation_exception_category": "kv_cache_allocation",
            "exception_type": "RuntimeError",
            "reason": "SECRET prompt in an allowed key",
            "method": "SECRET assistant output in an allowed key",
            "stderr_tail": "redacted SECRET prompt in stderr tail",
            "sanitized_error_summary": "RuntimeError:redacted SECRET assistant text",
        },
    )
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-unsafe-allowed-values",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["internal_reason"] == "runtime_kv_cache_allocation"
    assert error["worker_diagnostics"] == {
        "generation_exception_category": "kv_cache_allocation",
        "exception_type": "RuntimeError",
    }
    assert "SECRET" not in json.dumps(error)


def test_api_v1_generic_unsupported_generation_kwarg_uses_options_error():
    manager = _ApiV1RuntimeManager()
    manager.runtime.create_chat_completion.side_effect = TypeError(
        "create_chat_completion() got an unexpected keyword argument 'top_k'"
    )
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-generic-option-rejected",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert error["internal_reason"] == "unsupported_generation_option"
    assert error["exception_category"] == "unsupported_generation_kwarg"


def test_extract_unsupported_generation_kwarg_requires_attempted_name():
    from utils.networking.relay_client import _extract_unsupported_generation_kwarg

    attempted = ["temperature", "top_k", "top_p", "stop"]
    assert _extract_unsupported_generation_kwarg("unexpected keyword argument 'top_k'", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("got an unexpected keyword argument 'top_k'", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("unsupported option 'top_k'", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("unsupported option: top_k", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("invalid keyword 'top_k'", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("invalid keyword=top_k", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("invalid keyword argument 'top_k'", attempted) == "top_k"
    assert _extract_unsupported_generation_kwarg("unsupported option in model configuration", attempted) is None
    assert _extract_unsupported_generation_kwarg("unsupported option 'mirostat'", attempted) is None


def test_api_v1_cached_internal_filter_does_not_drop_later_explicit_client_option():
    manager = _RejectingAdmissionManager(["temperature", "temperature"], window=256)
    manager.model_profile["generation_defaults"] = {"temperature": 0.3}
    client = _api_v1_validation_client(manager)

    first = client._generate_api_v1_response_with_runtime_model(
        request_id="req-filter-internal-temperature",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )
    assert first["api_v1_response"]["message"]["content"] == "ok"

    second = client._generate_api_v1_response_with_runtime_model(
        request_id="req-explicit-temperature",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False, "temperature": 0.3},
        requested_context_tier="8k-fast",
    )

    error = second["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert error["internal_reason"] == "unsupported_generation_option"
    assert error["rejected_option"] == "temperature"
    assert all("temperature" not in call for call in manager.runtime.calls)


def test_api_v1_invalid_output_reason_clears_before_unavailable_completion_callable():
    class RuntimeWithoutCompletion:
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
            return "".join(
                f"<{message['role']}>{message['content']}" for message in messages
            ) + ("<assistant>" if add_generation_prompt else "")

        def tokenize(self, payload, _add_bos=False):
            return list(range(len(payload)))

    class ManagerWithoutCompletion(_ApiV1RuntimeManager):
        def __init__(self):
            super().__init__()
            self.runtime = RuntimeWithoutCompletion()

    manager = ManagerWithoutCompletion()
    client = _api_v1_validation_client(manager)
    client._last_api_v1_invalid_model_output_reason = "qwen_thinking_output_leaked"

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-stale-invalid-reason",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_invalid_model_output"
    assert error["internal_reason"] == "unsupported_completion_shape"


class _RejectingAdmissionRuntime(_AdmissionRuntime):
    def __init__(self, rejected_sequence):
        super().__init__()
        self.rejected_sequence = list(rejected_sequence)

    def create_chat_completion(self, **kwargs):
        self.calls.append(dict(kwargs))
        for rejected in list(self.rejected_sequence):
            if rejected in kwargs:
                self.rejected_sequence.remove(rejected)
                raise TypeError(f"got an unexpected keyword argument '{rejected}'")
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def create_chat_completion_from_rendered_prompt(self, messages, **kwargs):
        return self.create_chat_completion(messages=messages, **kwargs)


class _RejectingAdmissionManager(_AdmissionManager):
    def __init__(self, rejected_sequence, **kwargs):
        super().__init__(**kwargs)
        self.runtime = _RejectingAdmissionRuntime(rejected_sequence)
        self.model_profile = {
            "provider": "qwen",
            "thinking_mode": "disabled",
            "generation_defaults": {"top_k": 20},
        }
        self.api_model_id = "qwen3-8b-instruct"


def test_api_v1_internal_top_k_default_is_filtered_and_cached_per_client_after_runtime_rejection():
    manager = _RejectingAdmissionManager(["top_k"], window=256)
    client = _api_v1_validation_client(manager)

    first = client._generate_api_v1_response_with_runtime_model(
        request_id="req-filter-top-k",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )
    second = client._generate_api_v1_response_with_runtime_model(
        request_id="req-filter-top-k-cached",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )

    assert first["api_v1_response"]["message"]["content"] == "ok"
    assert second["api_v1_response"]["message"]["content"] == "ok"
    assert all("top_k" not in call for call in manager.runtime.calls)
    assert not client._api_v1_generation_kwargs_filtered

    fresh_client = _api_v1_validation_client(manager)
    third = fresh_client._generate_api_v1_response_with_runtime_model(
        request_id="req-filter-top-k-fresh-client",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )

    assert third["api_v1_response"]["message"]["content"] == "ok"
    assert "top_k" not in manager.runtime.calls[-1]
    assert not hasattr(manager, "api_v1_generation_kwargs_filtered")


def test_api_v1_internal_empty_stop_default_is_filtered_after_runtime_rejection():
    manager = _RejectingAdmissionManager(["stop"], window=256)
    manager.model_profile["generation_defaults"] = {}
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-filter-stop",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )

    assert envelope["api_v1_response"]["message"]["content"] == "ok"
    assert all("stop" not in call for call in manager.runtime.calls)


def test_api_v1_client_supplied_runtime_unsupported_option_is_not_silently_dropped():
    manager = _RejectingAdmissionManager(["temperature"], window=256)
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-client-temperature",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False, "temperature": 0.2},
        requested_context_tier="8k-fast",
    )

    error = envelope["api_v1_response"]["error"]
    assert error["code"] == "compute_node_options_unsupported"
    assert error["rejected_option"] == "temperature"


def test_api_v1_generation_kwarg_filtering_stops_after_bounded_internal_retries():
    manager = _RejectingAdmissionManager(["top_k", "temperature", "top_p", "stop"], window=256)
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-bounded-filtering",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )

    assert envelope["api_v1_response"]["message"]["content"] == "ok"
    assert len(manager.runtime.calls) == 1
    assert all(key not in manager.runtime.calls[0] for key in {"top_k", "temperature", "top_p", "stop"})


def test_api_v1_qwen_messages_remain_unmutated_after_generation_kwarg_filtering():
    manager = _RejectingAdmissionManager(["top_k"], window=256)
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-no-think-filtered",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )

    assert envelope["api_v1_response"]["message"]["content"] == "ok"
    assert manager.runtime.calls[-1]["messages"][-1]["content"] == "hello"


def test_api_v1_qwen_preserves_user_provided_literal_no_think():
    manager = _RejectingAdmissionManager([], window=256)
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-literal-no-think",
        model_id="qwen3-8b-instruct",
        messages=[{"role": "user", "content": "/no_think\nhello"}],
        options={"max_tokens": 5, "stream": False},
        requested_context_tier="8k-fast",
    )

    assert envelope["api_v1_response"]["message"]["content"] == "ok"
    assert manager.runtime.calls[-1]["messages"][-1]["content"] == "/no_think\nhello"


def test_api_v1_runtime_error_promotes_plain_completion_capability_diagnostics():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    manager = _ApiV1RuntimeManager()
    worker_diagnostics = {
        "code": "compute_node_internal_error",
        "generation_exception_category": "worker_exception",
        "exception_type": "RuntimeError",
        "method": "create_completion_keyword_prompt",
        "attempted_generation_kwargs": "max_tokens,prompt",
        "attempted_plain_completion_methods": "create_completion_keyword_prompt",
        "plain_completion_create_completion_callable": True,
        "plain_completion_llama_call_callable": True,
        "plain_completion_signature_inspectable": True,
        "plain_completion_accepts_prompt_kwarg": False,
        "plain_completion_accepts_max_tokens_kwarg": True,
        "plain_completion_accepts_var_kwargs": False,
        "plain_completion_reset_after_failure_count": 2,
        "plain_completion_prompt_tokenization_attempted": True,
        "plain_completion_prompt_token_count": 3,
        "plain_completion_eval_return_code": 1,
        "plain_completion_prompt_tokenization_method": "llama.tokenize",
        "plain_completion_prompt_tokenization_special": True,
        "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
        "plain_completion_prompt_tokenization_selected_variant": "tokenize_add_bos_false_special_true",
        "plain_completion_prompt_tokenization_selected_token_count": 28,
        "plain_completion_prompt_tokenization_selected_special": True,
        "qwen_api_v1_non_thinking_template_fallback": False,
        "prompt": "SECRET prompt",
        "rendered_prompt": "SECRET rendered prompt",
        "token_ids": [1, 2, 3],
        "assistant_output": "SECRET output",
        "model_output": "SECRET model output",
        "reasoning_content": "SECRET reasoning",
        "decrypted_payload": "SECRET payload",
        "key": "SECRET key",
        "api_key": "SECRET api key",
        "tool_args": {"secret": True},
        "ciphertext": "SECRET ciphertext",
        "raw_exception": "llama_decode returned -1 SECRET",
    }
    manager.runtime.create_chat_completion.side_effect = LlamaCppInferenceRequestError(
        "llama_cpp request failed",
        diagnostics=worker_diagnostics,
    )
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-plain-capabilities",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    for key, value in worker_diagnostics.items():
        if key in {
            "prompt",
            "rendered_prompt",
            "token_ids",
            "assistant_output",
            "model_output",
            "reasoning_content",
            "decrypted_payload",
            "key",
            "api_key",
            "tool_args",
            "ciphertext",
            "raw_exception",
        }:
            continue
        assert error["worker_diagnostics"].get(key) == value
    for key in (
        "plain_completion_create_completion_callable",
        "plain_completion_llama_call_callable",
        "plain_completion_signature_inspectable",
        "plain_completion_accepts_prompt_kwarg",
        "plain_completion_accepts_max_tokens_kwarg",
        "plain_completion_accepts_var_kwargs",
        "plain_completion_reset_after_failure_count",
        "plain_completion_prompt_tokenization_attempted",
        "plain_completion_prompt_token_count",
        "plain_completion_eval_return_code",
        "plain_completion_prompt_tokenization_method",
        "plain_completion_prompt_tokenization_special",
        "plain_completion_prompt_tokenization_error_category",
        "plain_completion_prompt_tokenization_selected_variant",
        "plain_completion_prompt_tokenization_selected_token_count",
        "plain_completion_prompt_tokenization_selected_special",
        "qwen_api_v1_non_thinking_template_fallback",
    ):
        assert error[key] == worker_diagnostics[key]
    assert "SECRET" not in json.dumps(error)


def test_api_v1_runtime_error_promotes_tokenization_category_to_safe_internal_reason():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    manager = _ApiV1RuntimeManager()
    manager.runtime.create_chat_completion.side_effect = LlamaCppInferenceRequestError(
        "failed to tokenize prompt SECRET_PROMPT",
        diagnostics={
            "generation_exception_category": "prompt_tokenization_failure",
            "exception_type": "RuntimeError",
            "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
            "plain_completion_prompt_tokenization_method": "llama.tokenize",
        },
    )
    client = _api_v1_validation_client(manager)

    envelope = client._generate_api_v1_response_with_runtime_model(
        request_id="req-tokenization-category",
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={},
    )

    error = envelope["api_v1_response"]["error"]
    assert error["internal_reason"] == "runtime_prompt_tokenization_failure"
    assert error["generation_exception_category"] == "prompt_tokenization_failure"
    assert error["plain_completion_prompt_tokenization_method"] == "llama.tokenize"
    assert "SECRET" not in json.dumps(error)


def test_worker_diagnostic_sanitizer_allows_qwen_plain_completion_variant_fields():
    from utils.networking import relay_client

    safe = relay_client._safe_worker_diagnostics({
        "plain_completion_prompt_tokenization_variant_count": 3,
        "plain_completion_prompt_tokenization_variant_ids": "tokenize_add_bos_false_special_false,tokenize_add_bos_false_no_special",
        "plain_completion_prompt_tokenization_token_counts": "50,28",
        "plain_completion_prompt_tokenization_special_values": "false,none,true",
        "plain_completion_prompt_tokenization_selected_variant": "tokenize_add_bos_false_special_false",
        "plain_completion_prompt_tokenization_selected_token_count": 28,
        "plain_completion_prompt_tokenization_selected_special": True,
        "plain_completion_attempt_methods": "create_completion_keyword_prompt,create_completion_keyword_token_ids",
        "plain_completion_attempt_categories": "prompt_eval_failure,prompt_eval_decode_failure",
        "plain_completion_attempt_exception_types": "RuntimeError,TypeError",
        "plain_completion_attempt_safe_summaries": "RuntimeError:prompt_eval_failure,RuntimeError:prompt_eval_decode_failure",
        "plain_completion_attempt_rejected_kwargs": "prompt",
        "plain_completion_attempt_result_shapes": "choices_text",
        "plain_completion_attempt_tokenization_variants": "tokenize_add_bos_false_special_false",
        "plain_completion_attempt_count": 4,
        "qwen_high_level_chat_fallback_attempted": True,
        "qwen_high_level_chat_fallback_supported": True,
        "qwen_high_level_chat_fallback_succeeded": False,
        "qwen_high_level_chat_fallback_rejected_kwarg": "chat_template_kwargs",
        "qwen_high_level_chat_fallback_category": "unsupported_generation_kwarg",
        "plain_completion_eval_return_code": 1,
        "prompt": "SECRET_PROMPT",
        "token_ids": "1,2,3",
    })

    assert safe["plain_completion_prompt_tokenization_variant_count"] == 3
    assert safe["plain_completion_attempt_count"] == 4
    assert safe["qwen_high_level_chat_fallback_attempted"] is True
    assert "prompt" not in safe
    assert "token_ids" not in safe
    assert "SECRET_PROMPT" not in json.dumps(safe)


def test_api_v1_shutdown_latch_waits_for_inflight_mutation_and_blocks_new_register(monkeypatch):
    client = _standalone_relay_client()
    client.start()
    request_started = threading.Event()
    release_request = threading.Event()

    response = MagicMock(status_code=200)
    response.json.return_value = {"next_ping_in_x_seconds": 120, "poll_wait_seconds": 0}

    def fake_post(*args, **kwargs):
        request_started.set()
        assert release_request.wait(timeout=1.0)
        return response

    monkeypatch.setattr(relay_client_module.requests, "post", fake_post)
    result_holder = {}
    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "response", client.register_api_v1_compute_node("http://localhost:5000")
        )
    )
    worker.start()
    try:
        assert request_started.wait(timeout=1.0)
        client._api_v1_latch_shutdown()
        assert client._api_v1_wait_for_mutation_quiescence(
            shutdown_deadline=time.monotonic() + 0.01
        ) is False
        assert client.register_api_v1_compute_node("http://localhost:5000")["error"] == "Relay polling stopped"
        release_request.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert client._api_v1_wait_for_mutation_quiescence(
            shutdown_deadline=time.monotonic() + 1.0
        ) is True
        assert result_holder["response"] == response.json.return_value
    finally:
        release_request.set()
        worker.join(timeout=1.0)


def test_process_client_request_does_not_submit_response_after_shutdown_latch():
    manager = _AdmissionManager()
    client = _api_v1_validation_client(manager)
    client._api_v1_registered_relays.add(client.relay_url)
    client.crypto_manager.decrypt_message.return_value = _api_v1_decrypted_payload(
        request_id="req-shutdown-latched"
    )
    generation_entered = threading.Event()
    release_generation = threading.Event()
    original_generate = client._generate_api_v1_response_with_runtime_model

    def blocked_generate(*args, **kwargs):
        generation_entered.set()
        assert release_generation.wait(timeout=2.0)
        return original_generate(*args, **kwargs)

    client._generate_api_v1_response_with_runtime_model = blocked_generate
    client._post_api_v1_response = MagicMock(return_value=True)
    result_holder = {}

    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result", client.process_client_request_result(TEST_VALID_RESPONSE.copy())
        ),
        daemon=True,
    )
    worker.start()
    assert generation_entered.wait(timeout=2.0)
    client._api_v1_latch_shutdown()
    release_generation.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    result = result_holder["result"]
    assert result.submitted is False
    assert result.runtime_healthy is True
    assert result.safe_error_code == "shutdown_requested"
    client._post_api_v1_response.assert_not_called()


@patch('utils.networking.relay_client.requests.post')
def test_reset_api_v1_polling_session_clear_registration_clears_control_credentials(mock_post):
    client = _standalone_relay_client()
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = 123.0
    client._store_api_v1_control_credential('http://localhost:5000', 'reset-secret')

    client.reset_api_v1_polling_session(clear_registration=True)

    assert client._api_v1_registered_relays == set()
    assert client._api_v1_last_heartbeat_at == {}
    assert client._api_v1_control_credential_for_relay('http://localhost:5000') == ''


@patch('utils.networking.relay_client.requests.post')
def test_register_api_v1_compute_node_preserves_and_rotates_control_credential(mock_post):
    client = _standalone_relay_client()
    first = MagicMock(status_code=200)
    first.json.return_value = {'registered': True, 'control_credential': 'first-secret'}
    refresh = MagicMock(status_code=200)
    refresh.json.return_value = {'registered': True}
    rotated = MagicMock(status_code=200)
    rotated.json.return_value = {'registered': True, 'control_credential': 'rotated-secret'}
    mock_post.side_effect = [first, refresh, rotated]

    assert client.register_api_v1_compute_node()['control_credential'] == 'first-secret'
    assert client._api_v1_control_credential_for_relay('http://localhost:5000') == 'first-secret'
    client.register_api_v1_compute_node()
    assert client._api_v1_control_credential_for_relay('http://localhost:5000') == 'first-secret'
    client.register_api_v1_compute_node()
    assert client._api_v1_control_credential_for_relay('http://localhost:5000') == 'rotated-secret'


@patch('utils.networking.relay_client.requests.post')
def test_api_v1_background_heartbeat_preserves_and_rotates_control_credential(mock_post, monkeypatch):
    ticks = iter([10.0, 20.0, 30.0, 40.0, 50.0])
    monkeypatch.setattr(relay_client_module.time, 'monotonic', lambda: next(ticks, 60.0))
    client = _standalone_relay_client()
    client._api_v1_registered_relays.add('http://localhost:5000')
    client._api_v1_last_heartbeat_at['http://localhost:5000'] = -100.0
    client._api_v1_relay_wait_hints = {
        'http://localhost:5000': {'next_ping_in_x_seconds': 1, 'poll_wait_seconds': 1}
    }
    client._store_api_v1_control_credential('http://localhost:5000', 'current-secret')

    class StopAfterTwo:
        def __init__(self):
            self.calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 2

        def is_set(self):
            return False

    responses = [
        {'registered': True, 'next_ping_in_x_seconds': 1},
        {'registered': True, 'control_credential': 'rotated-secret', 'next_ping_in_x_seconds': 1},
    ]
    observed = []

    def register(url):
        observed.append(client._api_v1_control_credential_for_relay(url))
        response = responses.pop(0)
        credential = response.get('control_credential')
        if credential:
            client._store_api_v1_control_credential(url, credential)
        return response

    client.register_api_v1_compute_node = register
    client._api_v1_heartbeat_stop = StopAfterTwo()

    client._api_v1_heartbeat_worker()

    assert observed == ['current-secret', 'current-secret']
    assert client._api_v1_control_credential_for_relay('http://localhost:5000') == 'rotated-secret'
