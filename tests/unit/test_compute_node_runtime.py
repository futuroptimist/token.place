from unittest.mock import MagicMock

from utils.compute_node_runtime import (
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    format_relay_target,
    resolve_relay_port,
    resolve_relay_url,
)


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
