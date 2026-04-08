from unittest.mock import MagicMock

from utils.compute_node_runtime import (
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    first_env,
    format_relay_target,
    resolve_relay_port,
    resolve_relay_url,
)


def test_first_env_skips_blank_values(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "   ")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_URL", "https://fallback.example")

    assert first_env(["TOKENPLACE_RELAY_URL", "TOKEN_PLACE_RELAY_URL"]) == "https://fallback.example"


def test_compute_node_runtime_ensure_model_ready_download_success():
    model_manager = MagicMock()
    model_manager.use_mock_llm = False
    model_manager.download_model_if_needed.return_value = True
    relay_client = MagicMock()
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    assert runtime.ensure_model_ready() is True
    model_manager.download_model_if_needed.assert_called_once_with()


def test_compute_node_runtime_ensure_model_ready_with_mock_model():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    relay_client = MagicMock()
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    assert runtime.ensure_model_ready() is True
    model_manager.download_model_if_needed.assert_not_called()


def test_compute_node_runtime_ensure_model_ready_download_failure():
    model_manager = MagicMock()
    model_manager.use_mock_llm = False
    model_manager.download_model_if_needed.return_value = False
    relay_client = MagicMock()
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    assert runtime.ensure_model_ready() is False
    model_manager.download_model_if_needed.assert_called_once_with()


def test_compute_node_runtime_polling_thread_delegates_to_relay():
    relay_client = MagicMock()
    relay_client.poll_relay_continuously = MagicMock()
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    thread = MagicMock()

    def fake_thread_factory(*, target, daemon):
        assert target == relay_client.poll_relay_continuously
        assert daemon is True
        return thread

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
        thread_factory=fake_thread_factory,
    )

    created_thread = runtime.start_relay_polling()

    assert created_thread is thread
    thread.start.assert_called_once_with()


def test_compute_node_runtime_request_flow_delegates_to_relay_client():
    relay_client = MagicMock()
    relay_client.process_client_request.return_value = True
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    payload = {
        "client_public_key": "key",
        "chat_history": "payload",
        "cipherkey": "cipher",
        "iv": "iv",
    }

    assert runtime.process_relay_request(payload) is True
    relay_client.process_client_request.assert_called_once_with(payload)


def test_compute_node_runtime_relay_resolution_uses_env_overrides(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("TOKENPLACE_RELAY_PORT", "4444")

    relay_url = resolve_relay_url("https://token.place")
    relay_port = resolve_relay_port(None, relay_url)

    assert relay_url == "https://relay.example"
    assert relay_port == 4444
    assert format_relay_target(relay_url, relay_port) == "https://relay.example:4444"


def test_compute_node_runtime_resolve_relay_port_accepts_explicit_zero_port():
    assert resolve_relay_port(None, "https://token.place:0") == 0


def test_compute_node_runtime_relay_port_returns_cli_default_for_invalid_env(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_PORT", "bad-port")

    assert resolve_relay_port(9000, "https://token.place") == 9000


def test_compute_node_runtime_relay_port_returns_none_when_no_values(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_RELAY_PORT", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_PORT", raising=False)
    monkeypatch.delenv("RELAY_PORT", raising=False)

    assert resolve_relay_port(None, "https://token.place") is None


def test_compute_node_runtime_format_relay_target_preserves_explicit_url_port():
    assert format_relay_target("https://token.place:7443", 9999) == "https://token.place:7443"


def test_compute_node_runtime_register_and_poll_once_delegates_to_relay_client():
    relay_client = MagicMock()
    relay_client.ping_relay.return_value = {"relayStatus": "ok"}
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    assert runtime.register_and_poll_once() == {"relayStatus": "ok"}
    relay_client.ping_relay.assert_called_once_with()


def test_compute_node_runtime_stop_delegates_to_relay_client():
    relay_client = MagicMock()
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    runtime.stop()
    relay_client.stop.assert_called_once_with()
