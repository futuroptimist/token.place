from unittest.mock import call, MagicMock

import pytest

from utils.compute_node_runtime import (
    ApiV1RelayRequestAdapter,
    apply_compute_mode,
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    LegacyRelayRequestAdapter,
    first_env,
    format_relay_target,
    is_api_v1_relay_payload,
    is_legacy_relay_payload,
    normalize_compute_mode,
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


def test_compute_node_runtime_ensure_api_v1_runtime_ready_success():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.last_compute_diagnostics = {"requested_mode": "cpu"}
    llm_runtime = MagicMock()
    llm_runtime.create_chat_completion = lambda **_kwargs: {}
    model_manager.get_llm_instance.return_value = llm_runtime
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=MagicMock(),
        crypto_manager=MagicMock(),
    )
    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager.last_compute_diagnostics["api_v1_runtime_ready"] is True


@pytest.mark.parametrize(
    "llm_instance,getter,expected",
    [
        (None, True, False),
        (object(), True, False),
        (MagicMock(create_chat_completion=None), True, False),
        (MagicMock(create_chat_completion=lambda **_kwargs: {}), False, False),
    ],
)
def test_compute_node_runtime_ensure_api_v1_runtime_ready_failure_cases(
    llm_instance, getter, expected
):
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    if getter:
        model_manager.get_llm_instance.return_value = llm_instance
    else:
        delattr(model_manager, "get_llm_instance")
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=MagicMock(),
        crypto_manager=MagicMock(),
    )
    assert runtime.ensure_api_v1_runtime_ready() is expected


def test_compute_node_runtime_ensure_api_v1_runtime_ready_handles_get_llm_exception():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.get_llm_instance.side_effect = RuntimeError("boom")

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=MagicMock(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False


def test_compute_node_runtime_ensure_api_v1_runtime_ready_without_diagnostics_dict():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.last_compute_diagnostics = "not-a-dict"
    llm_runtime = MagicMock()
    llm_runtime.create_chat_completion = lambda **_kwargs: {}
    model_manager.get_llm_instance.return_value = llm_runtime

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=MagicMock(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager.last_compute_diagnostics == "not-a-dict"


def test_compute_node_runtime_polling_thread_delegates_to_relay():
    relay_client = MagicMock()
    relay_client.poll_relay_continuously = MagicMock()
    relay_client.poll_api_v1_encrypted_work_continuously = MagicMock()
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    thread = MagicMock()

    def fake_thread_factory(*, target, daemon):
        assert target == relay_client.poll_api_v1_encrypted_work_continuously
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


def test_compute_node_runtime_start_relay_session_resets_relay_client_start_state():
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

    runtime.start_relay_session()

    relay_client.start.assert_called_once_with()


def test_compute_node_runtime_polling_thread_supports_api_v1_only_relay_client():
    class ApiV1OnlyRelayClient:
        def __init__(self):
            self.poll_api_v1_encrypted_work_continuously = MagicMock()

    relay_client = ApiV1OnlyRelayClient()
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()
    thread = MagicMock()

    def fake_thread_factory(*, target, daemon):
        assert target == relay_client.poll_api_v1_encrypted_work_continuously
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


def test_compute_node_runtime_polling_thread_fails_closed_when_api_v1_poller_missing():
    class LegacyOnlyRelayClient:
        @property
        def poll_relay_continuously(self):
            raise AssertionError("legacy poller must not be accessed")

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()
    thread_factory = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=LegacyOnlyRelayClient(),
        crypto_manager=crypto_manager,
        thread_factory=thread_factory,
    )

    with pytest.raises(
        RuntimeError,
        match="API v1 E2EE relay polling is required; legacy relay polling is deprecated",
    ):
        runtime.start_relay_polling()

    thread_factory.assert_not_called()


def test_compute_node_runtime_polling_thread_fails_closed_when_api_v1_poller_not_callable():
    relay_client = MagicMock()
    relay_client.poll_api_v1_encrypted_work_continuously = None
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()
    thread_factory = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
        thread_factory=thread_factory,
    )

    with pytest.raises(
        RuntimeError,
        match="API v1 E2EE relay polling is required; legacy relay polling is deprecated",
    ):
        runtime.start_relay_polling()

    relay_client.poll_relay_continuously.assert_not_called()
    thread_factory.assert_not_called()


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
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "key",
        "chat_history": "payload",
        "cipherkey": "cipher",
        "iv": "iv",
    }

    assert runtime.process_relay_request(payload) is True
    relay_client.process_client_request.assert_called_once_with(payload)


def test_compute_node_runtime_submit_api_v1_error_response_delegates_to_relay_client():
    relay_client = MagicMock()
    relay_client.submit_api_v1_error_response.return_value = True
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )
    payload = {"request_id": "req-runtime-error", "chat_history": "ciphertext"}

    assert (
        runtime.submit_api_v1_error_response(
            payload,
            code="compute_node_runtime_unavailable",
            message="failed to initialize API v1 model runtime",
        )
        is True
    )
    relay_client.submit_api_v1_error_response.assert_called_once_with(
        payload,
        code="compute_node_runtime_unavailable",
        message="failed to initialize API v1 model runtime",
    )


def test_compute_node_runtime_submit_api_v1_error_response_fails_closed_without_helper():
    relay_client = MagicMock()
    relay_client.submit_api_v1_error_response = None
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    assert (
        runtime.submit_api_v1_error_response(
            {"request_id": "req-runtime-error"},
            code="compute_node_runtime_unavailable",
            message="failed to initialize API v1 model runtime",
        )
        is False
    )


def test_compute_node_runtime_process_relay_request_returns_false_for_unknown_payload():
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

    assert runtime.process_relay_request({"unexpected": "payload"}) is False
    relay_client.process_client_request.assert_not_called()


def test_compute_node_runtime_respects_explicit_empty_adapter_list():
    relay_client = MagicMock()
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
        request_adapters=[],
    )

    legacy_payload = {
        "client_public_key": "key",
        "chat_history": "payload",
        "cipherkey": "cipher",
        "iv": "iv",
    }
    assert runtime.process_relay_request(legacy_payload) is False
    relay_client.process_client_request.assert_not_called()


def test_api_v1_relay_payload_detection_identifies_valid_api_v1_envelope():
    assert is_api_v1_relay_payload({"api_v1_payload": True}) is False
    assert is_api_v1_relay_payload({
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }) is True


def test_api_v1_relay_payload_is_not_reported_as_legacy():
    payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
        "e2ee_v1": True,
    }

    assert is_api_v1_relay_payload(payload) is True
    assert is_legacy_relay_payload(payload) is False


def test_api_v1_relay_payload_detection_rejects_legacy_messages_shape():
    assert is_api_v1_relay_payload({
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "k",
        "api_v1_request": {"messages": []},
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }) is True
    assert is_api_v1_relay_payload({
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }) is False
    assert is_api_v1_relay_payload({
        "protocol": "wrong",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }) is False
    assert is_api_v1_relay_payload({
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 2,
        "request_id": "req-1",
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }) is False
    assert is_api_v1_relay_payload({
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": 123,
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }) is False
    assert is_api_v1_relay_payload({
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "k",
        "chat_history": {},
        "cipherkey": "k",
        "iv": "i",
    }) is False


def test_api_v1_relay_request_adapter_processes_api_v1_payload():
    relay_client = MagicMock()
    adapter = ApiV1RelayRequestAdapter(relay_client)

    payload = {
        "protocol": "tokenplace_api_v1_relay_e2ee",
        "version": 1,
        "request_id": "req-1",
        "client_public_key": "k",
        "chat_history": "c",
        "cipherkey": "k",
        "iv": "i",
    }
    relay_client.process_client_request.return_value = True
    assert adapter.can_process(payload) is True
    assert adapter.process(payload) is True
    relay_client.process_client_request.assert_called_once_with(payload)

def test_legacy_relay_request_adapter_only_matches_legacy_contract():
    relay_client = MagicMock()
    adapter = LegacyRelayRequestAdapter(relay_client)

    legacy_payload = {
        "client_public_key": "key",
        "chat_history": "payload",
        "cipherkey": "cipher",
        "iv": "iv",
    }

    assert is_legacy_relay_payload(legacy_payload) is True
    assert adapter.can_process(legacy_payload) is True
    assert adapter.can_process({"chat_history": "missing keys"}) is False


def test_compute_node_runtime_relay_resolution_uses_env_overrides(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("TOKENPLACE_RELAY_PORT", "4444")

    relay_url = resolve_relay_url("https://token.place")
    relay_port = resolve_relay_port(None, relay_url)

    assert relay_url == "https://relay.example"
    assert relay_port == 4444
    assert format_relay_target(relay_url, relay_port) == "https://relay.example:4444"


def test_compute_node_runtime_relay_resolution_prefers_explicit_cli_value(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "https://relay.example")

    relay_url = resolve_relay_url("http://127.0.0.1:5010", prefer_cli=True)

    assert relay_url == "http://127.0.0.1:5010"


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


def test_normalize_compute_mode_is_case_insensitive_and_falls_back_to_auto():
    assert normalize_compute_mode("GPU") == "gpu"
    assert normalize_compute_mode("  hybrid ") == "hybrid"
    assert normalize_compute_mode("CUDA") == "gpu"
    assert normalize_compute_mode("metal") == "gpu"
    assert normalize_compute_mode("unknown") == "auto"
    assert normalize_compute_mode("") == "auto"
    assert normalize_compute_mode(None) == "auto"


def test_apply_compute_mode_sets_expected_gpu_layer_defaults():
    manager = MagicMock()

    assert apply_compute_mode(manager, "cpu") == "cpu"
    assert manager.default_n_gpu_layers == 0

    assert apply_compute_mode(manager, "auto") == "auto"
    assert manager.default_n_gpu_layers == -1

    manager.hybrid_n_gpu_layers = 12
    assert apply_compute_mode(manager, "hybrid") == "hybrid"
    assert manager.default_n_gpu_layers == 12


def test_compute_node_runtime_register_and_poll_once_delegates_to_relay_client():
    relay_client = MagicMock()
    relay_client.poll_api_v1_encrypted_work.return_value = {"relayStatus": "ok"}
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
    relay_client.poll_api_v1_encrypted_work.assert_called_once_with()


def test_compute_node_runtime_default_path_is_api_v1_only():
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

    assert any(isinstance(adapter, ApiV1RelayRequestAdapter) for adapter in runtime.request_adapters)
    assert not any(isinstance(adapter, LegacyRelayRequestAdapter) for adapter in runtime.request_adapters)

    legacy_payload = {
        "client_public_key": "key",
        "chat_history": "payload",
        "cipherkey": "cipher",
        "iv": "iv",
    }

    relay_client.process_client_request.return_value = True
    relay_client.poll_api_v1_encrypted_work.side_effect = lambda: {
        "relayStatus": "ok",
        "processed": runtime.process_relay_request(legacy_payload),
    }

    assert runtime.register_and_poll_once() == {"relayStatus": "ok", "processed": False}
    relay_client.poll_api_v1_encrypted_work.assert_called_once_with()
    relay_client.process_client_request.assert_not_called()


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
    relay_client.unregister_from_relay.assert_called_once_with()
    relay_client.stop.assert_called_once_with()
    assert relay_client.method_calls[:2] == [
        call.stop(),
        call.unregister_from_relay(),
    ]


def test_compute_node_runtime_stop_continues_when_unregister_raises():
    relay_client = MagicMock()
    relay_client.unregister_from_relay.side_effect = RuntimeError("network down")
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

    relay_client.unregister_from_relay.assert_called_once_with()
    relay_client.stop.assert_called_once_with()
    assert relay_client.method_calls[:2] == [
        call.stop(),
        call.unregister_from_relay(),
    ]
