from unittest.mock import MagicMock

import server.compute_node_runtime as runtime


def _build_runtime(monkeypatch, *, use_mock_llm=False, download_ok=True):
    model_manager = MagicMock()
    model_manager.use_mock_llm = use_mock_llm
    model_manager.download_model_if_needed.return_value = download_ok

    crypto_manager = MagicMock()
    relay_client = MagicMock()

    monkeypatch.setattr(runtime, "get_model_manager", lambda: model_manager)
    monkeypatch.setattr(runtime, "get_crypto_manager", lambda: crypto_manager)
    monkeypatch.setattr(runtime, "RelayClient", MagicMock(return_value=relay_client))

    cfg = runtime.ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None)
    node_runtime = runtime.ComputeNodeRuntime(cfg, log_info=MagicMock(), log_error=MagicMock())
    return node_runtime, relay_client, model_manager


def test_compute_node_runtime_wires_existing_managers(monkeypatch):
    node_runtime, _, _ = _build_runtime(monkeypatch)

    assert node_runtime.relay_client is not None
    assert node_runtime.model_manager is not None
    assert node_runtime.crypto_manager is not None


def test_initialize_model_skips_download_for_mock_mode(monkeypatch):
    node_runtime, _, model_manager = _build_runtime(monkeypatch, use_mock_llm=True)

    node_runtime.initialize_model()

    model_manager.download_model_if_needed.assert_not_called()


def test_initialize_model_downloads_when_not_in_mock_mode(monkeypatch):
    node_runtime, _, model_manager = _build_runtime(monkeypatch, use_mock_llm=False)

    node_runtime.initialize_model()

    model_manager.download_model_if_needed.assert_called_once_with()


def test_start_relay_polling_starts_daemon_thread(monkeypatch):
    node_runtime, relay_client, _ = _build_runtime(monkeypatch)

    fake_thread = MagicMock()
    thread_cls = MagicMock(return_value=fake_thread)
    monkeypatch.setattr(runtime.threading, "Thread", thread_cls)

    returned = node_runtime.start_relay_polling()

    thread_cls.assert_called_once_with(
        target=relay_client.poll_relay_continuously,
        daemon=True,
    )
    fake_thread.start.assert_called_once_with()
    assert returned is fake_thread


def test_resolve_relay_port_prefers_env(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_PORT", "7443")

    resolved = runtime.resolve_relay_port(None, "https://token.place", log_error=MagicMock())

    assert resolved == 7443
