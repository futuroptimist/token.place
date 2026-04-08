from unittest.mock import MagicMock, patch

from utils.runtime.compute_node import (
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    format_relay_target,
    resolve_relay_port,
    resolve_relay_url,
)


def _runtime_config() -> ComputeNodeRuntimeConfig:
    return ComputeNodeRuntimeConfig(
        relay_url="https://token.place",
        relay_port=None,
        server_host="127.0.0.1",
        server_port=3000,
    )


def test_resolve_relay_url_uses_env_override(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "https://relay.example")
    assert resolve_relay_url("https://token.place") == "https://relay.example"


def test_resolve_relay_port_logs_invalid_env(monkeypatch):
    errors = []
    monkeypatch.setenv("RELAY_PORT", "not-a-port")

    result = resolve_relay_port(5443, "https://token.place", log_error=errors.append)

    assert result == 5443
    assert errors == ["Invalid relay port override: not-a-port"]


def test_format_relay_target_avoids_duplicate_port():
    assert format_relay_target("http://localhost:5000", 5000) == "http://localhost:5000"


def test_initialize_model_uses_download_when_not_mock():
    model_manager = MagicMock()
    model_manager.use_mock_llm = False
    model_manager.download_model_if_needed.return_value = True
    runtime = ComputeNodeRuntime(
        _runtime_config(),
        model_manager=model_manager,
        crypto_manager=MagicMock(),
        relay_client=MagicMock(),
        log_info=MagicMock(),
        log_error=MagicMock(),
    )

    assert runtime.initialize_model() is True
    model_manager.download_model_if_needed.assert_called_once_with()


def test_start_relay_polling_starts_thread():
    relay_client = MagicMock()
    relay_client.poll_relay_continuously = MagicMock()
    runtime = ComputeNodeRuntime(
        _runtime_config(),
        model_manager=MagicMock(),
        crypto_manager=MagicMock(),
        relay_client=relay_client,
        log_info=MagicMock(),
        log_error=MagicMock(),
    )

    with patch("utils.runtime.compute_node.threading.Thread") as thread_cls:
        thread = MagicMock()
        thread_cls.return_value = thread

        returned = runtime.start_relay_polling()

    thread_cls.assert_called_once_with(
        target=relay_client.poll_relay_continuously,
        daemon=True,
    )
    thread.start.assert_called_once_with()
    assert returned is thread
