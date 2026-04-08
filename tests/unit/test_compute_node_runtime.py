from unittest.mock import MagicMock, patch

from utils.runtime.compute_node import ComputeNodeRuntime, RuntimeConfig


@patch("utils.runtime.compute_node.RelayClient")
@patch("utils.runtime.compute_node.get_crypto_manager")
@patch("utils.runtime.compute_node.get_model_manager")
def test_runtime_initializes_shared_components(mock_get_model, mock_get_crypto, mock_relay):
    model_manager = MagicMock()
    crypto_manager = MagicMock()
    mock_get_model.return_value = model_manager
    mock_get_crypto.return_value = crypto_manager

    runtime = ComputeNodeRuntime(
        RuntimeConfig(relay_url="https://token.place", relay_port=None),
        is_production=False,
    )

    mock_relay.assert_called_once_with(
        base_url="https://token.place",
        port=None,
        crypto_manager=crypto_manager,
        model_manager=model_manager,
    )
    assert runtime.model_manager is model_manager
    assert runtime.crypto_manager is crypto_manager


@patch("utils.runtime.compute_node.RelayClient")
@patch("utils.runtime.compute_node.get_crypto_manager")
@patch("utils.runtime.compute_node.get_model_manager")
def test_initialize_model_readiness_downloads_when_not_mock(
    mock_get_model, mock_get_crypto, mock_relay
):
    model_manager = MagicMock()
    model_manager.use_mock_llm = False
    model_manager.download_model_if_needed.return_value = True
    mock_get_model.return_value = model_manager
    mock_get_crypto.return_value = MagicMock()

    runtime = ComputeNodeRuntime(RuntimeConfig(relay_url="https://token.place", relay_port=7443))
    runtime.initialize_model_readiness()

    model_manager.download_model_if_needed.assert_called_once()


@patch("utils.runtime.compute_node.RelayClient")
@patch("utils.runtime.compute_node.get_crypto_manager")
@patch("utils.runtime.compute_node.get_model_manager")
def test_process_relay_request_delegates_to_relay_client(mock_get_model, mock_get_crypto, mock_relay):
    mock_get_model.return_value = MagicMock(use_mock_llm=True)
    mock_get_crypto.return_value = MagicMock()
    relay_instance = mock_relay.return_value
    relay_instance.process_client_request.return_value = True

    runtime = ComputeNodeRuntime(RuntimeConfig(relay_url="https://token.place", relay_port=7443))
    payload = {
        "client_public_key": "key",
        "chat_history": "cipher",
        "cipherkey": "k",
        "iv": "iv",
    }

    assert runtime.process_relay_request(payload) is True
    relay_instance.process_client_request.assert_called_once_with(payload)


@patch("utils.runtime.compute_node.threading.Thread")
@patch("utils.runtime.compute_node.RelayClient")
@patch("utils.runtime.compute_node.get_crypto_manager")
@patch("utils.runtime.compute_node.get_model_manager")
def test_start_relay_polling_starts_daemon_thread(
    mock_get_model,
    mock_get_crypto,
    mock_relay,
    mock_thread,
):
    mock_get_model.return_value = MagicMock(use_mock_llm=True)
    mock_get_crypto.return_value = MagicMock()
    relay_instance = mock_relay.return_value
    thread_instance = mock_thread.return_value

    runtime = ComputeNodeRuntime(RuntimeConfig(relay_url="http://localhost", relay_port=5000))
    returned_thread = runtime.start_relay_polling()

    mock_thread.assert_called_once_with(target=relay_instance.poll_relay_continuously, daemon=True)
    thread_instance.start.assert_called_once()
    assert returned_thread is thread_instance
    assert runtime.relay_target == "http://localhost:5000"
