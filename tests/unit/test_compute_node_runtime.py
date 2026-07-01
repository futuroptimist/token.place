from types import SimpleNamespace
from unittest.mock import call, MagicMock

import pytest

from utils.compute_node_runtime import (
    ApiV1RelayRequestAdapter,
    apply_compute_mode,
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    LegacyRelayRequestAdapter,
    compute_mode_diagnostics,
    first_env,
    format_relay_target,
    is_api_v1_relay_payload,
    is_legacy_relay_payload,
    normalize_compute_mode,
    resolve_relay_port,
    resolve_relay_url,
)


def _ready_relay_client():
    return SimpleNamespace(
        _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 3)
    )


class _ReadyRuntime:
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **_kwargs):
        rendered = "".join(str(message.get("content", "")) for message in messages)
        return rendered + ("<assistant>" if add_generation_prompt else "")

    def tokenize(self, payload, *args, **_kwargs):
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return list(range(max(1, len(str(payload)))))

    def create_chat_completion(self, **_kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


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


def test_compute_node_runtime_model_file_preflight_log_is_not_runtime_ready(caplog):
    model_manager = MagicMock()
    model_manager.use_mock_llm = False
    model_manager.model_path = "/tmp/model.gguf"
    model_manager.download_model_if_needed.return_value = True
    relay_client = MagicMock()
    crypto_manager = MagicMock()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=crypto_manager,
    )

    with caplog.at_level("INFO", logger="utils.compute_node_runtime"):
        assert runtime.ensure_model_ready() is True

    messages = [record.getMessage() for record in caplog.records]
    assert "Model ready for inference" not in messages
    assert "Model file ready for runtime initialization: /tmp/model.gguf" in messages


def test_compute_node_runtime_warmup_logs_model_instantiation_stages(caplog):
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_path = "/tmp/model.gguf"
    model_manager.last_compute_diagnostics = {"requested_mode": "cpu"}
    llm_runtime = _ReadyRuntime()
    model_manager.get_llm_instance.return_value = llm_runtime
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    with caplog.at_level("INFO", logger="utils.compute_node_runtime"):
        assert runtime.ensure_api_v1_runtime_ready() is True

    messages = [record.getMessage() for record in caplog.records]
    assert "API v1 runtime warmup about to instantiate model: /tmp/model.gguf" in messages
    assert "API v1 runtime warmup model instantiated: /tmp/model.gguf" in messages


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


def test_compute_mode_diagnostics_reports_backend_parity_fields():
    model_manager = MagicMock()

    assert apply_compute_mode(model_manager, "auto") == "auto"
    pending = compute_mode_diagnostics(model_manager)
    assert pending["backend_available"] == "unknown"
    assert pending["backend_selected"] == "unknown"
    assert pending["backend_used"] == "unknown"
    assert pending["fallback_reason"] is None

    model_manager.last_compute_diagnostics = {
        "requested_mode": "auto",
        "effective_mode": "gpu",
        "backend_available": "metal",
        "backend_selected": "metal",
        "backend_used": "metal",
        "n_gpu_layers": -1,
        "fallback_reason": "metal runtime warmed for API v1 relay processing",
    }

    ready = compute_mode_diagnostics(model_manager)
    assert ready["backend_available"] == "metal"
    assert ready["backend_selected"] == "metal"
    assert ready["backend_used"] == "metal"
    assert ready["fallback_reason"] == "metal runtime warmed for API v1 relay processing"


def test_compute_node_runtime_ensure_api_v1_runtime_ready_success():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.last_compute_diagnostics = {"requested_mode": "cpu"}
    llm_runtime = _ReadyRuntime()
    model_manager.get_llm_instance.return_value = llm_runtime
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )
    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager.last_compute_diagnostics["api_v1_readiness_result"] == "passed"


def test_compute_node_runtime_readiness_admission_exception_is_generic_not_bridge_missing():
    class BridgeRuntime:
        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 1}

    def _raise_admission_error(**_kwargs):
        raise TimeoutError("relay admission timed out")

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = BridgeRuntime()
    relay_client = SimpleNamespace(
        _api_v1_authoritative_context_admission=_raise_admission_error
    )
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager.last_runtime_init_error == (
        "API v1 context admission readiness failed: "
        "compute_node_context_admission_unavailable reason=unknown"
    )
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_exception_type"] == "TimeoutError"
    assert diagnostics["api_v1_readiness_packaged_bridge_available"] is True
    assert diagnostics["api_v1_readiness_tokenizer_render_bridge_available"] is False


def test_compute_node_runtime_qwen_blocks_registration_when_admission_bridge_missing():
    class MissingBridgeRuntime:
        def create_chat_completion(self, **_kwargs):
            return {}

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "chat_template_policy": "gguf-jinja",
    }
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = MissingBridgeRuntime()
    relay_client = SimpleNamespace(
        _api_v1_authoritative_context_admission=lambda **_kwargs: (
            False,
            {
                "code": "compute_node_context_admission_unavailable",
                "internal_reason": "runtime_template_tokenizer_bridge_unavailable",
            },
            None,
        )
    )
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager.last_runtime_init_error == (
        "Qwen API v1 context admission unavailable: runtime template/tokenizer bridge missing"
    )
    assert model_manager.last_compute_diagnostics["api_v1_readiness_result"] == "failed"
    assert (
        model_manager.last_compute_diagnostics[
            "api_v1_readiness_tokenizer_render_bridge_available"
        ]
        is False
    )


def test_compute_node_runtime_qwen_generic_admission_failure_keeps_safe_reason():
    class BridgeRuntime:
        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 1}

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = BridgeRuntime()
    relay_client = SimpleNamespace(
        _api_v1_authoritative_context_admission=lambda **_kwargs: (
            False,
            {
                "code": "compute_node_context_tier_unsupported",
                "reason": "requested_tier_not_active",
            },
            8,
        )
    )
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager.last_runtime_init_error == (
        "API v1 context admission readiness failed: "
        "compute_node_context_tier_unsupported reason=requested_tier_not_active"
    )
    assert model_manager.last_compute_diagnostics["api_v1_readiness_error_reason"] == (
        "requested_tier_not_active"
    )


def test_compute_node_runtime_qwen_64k_readiness_reports_yarn_rope():
    class ReadyRuntime:
        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "rope_scaling_policy": {
            "type": "yarn",
            "factor": 2.0,
            "original_context_tokens": 32768,
        },
    }
    model_manager.context_tier = "64k-full"
    model_manager.context_window_tokens = 65536
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = ReadyRuntime()
    relay_client = SimpleNamespace(
        _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2)
    )
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_yarn_rope_enabled"] is True
    assert diagnostics["api_v1_readiness_yarn_rope_factor"] == 2.0
    assert diagnostics["api_v1_readiness_yarn_original_context_tokens"] == 32768
    assert diagnostics["api_v1_readiness_tokenizer_render_bridge_available"] is True


def test_compute_node_runtime_readiness_smoke_completion_passes(monkeypatch):
    class SmokeRuntime:
        def __init__(self):
            self.completion_kwargs = None

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **kwargs):
            self.completion_kwargs = kwargs
            return {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    llm_runtime = SmokeRuntime()
    model_manager.get_llm_instance.return_value = llm_runtime
    relay_client = SimpleNamespace(
        _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2)
    )
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert diagnostics["api_v1_readiness_result"] == "passed"
    assert llm_runtime.completion_kwargs["stream"] is False
    assert llm_runtime.completion_kwargs["max_tokens"] == 4
    assert llm_runtime.completion_kwargs["messages"][-1]["content"].startswith("/no_think")


def test_compute_node_runtime_readiness_smoke_completion_rejects_think_output(monkeypatch):
    class ThinkRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "<think>no"}}]}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = ThinkRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2)
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "failed"
    assert diagnostics["api_v1_readiness_result"] == "failed"
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_failed"


def test_compute_node_runtime_readiness_smoke_completion_records_safe_exception(monkeypatch):
    class RaisingRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **_kwargs):
            raise RuntimeError("prompt text must not leak")

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = RaisingRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2)
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "failed"
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "RuntimeError"
    assert "prompt text" not in str(diagnostics)

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
    llm_runtime = _ReadyRuntime()
    model_manager.get_llm_instance.return_value = llm_runtime

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager.last_compute_diagnostics["api_v1_runtime_ready"] is True


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
    result = adapter.process(payload)
    assert bool(result) is True
    assert result.inference_succeeded is True
    assert result.submitted is True
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


def test_compute_node_runtime_relay_port_prefers_explicit_url_port(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_PORT", "9999")

    relay_port = resolve_relay_port(None, "http://127.0.0.1:5010", prefer_cli=True)

    assert relay_port == 5010


def test_compute_node_runtime_relay_port_prefers_explicit_cli_port(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_PORT", "9999")

    relay_port = resolve_relay_port(5010, "http://127.0.0.1", prefer_cli=True)

    assert relay_port == 5010


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
    relay_client._api_v1_registered_relays = {"https://token.place"}
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
    relay_client._api_v1_registered_relays = {"https://token.place"}
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


def test_compute_node_runtime_replaces_stale_error_on_preflight_failure():
    model_manager = MagicMock()
    model_manager.use_mock_llm = False
    model_manager.last_runtime_init_error = 'llama_cpp_import_timeout after 0.01s'
    model_manager.download_model_if_needed.return_value = False

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=MagicMock(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager.last_runtime_init_error == 'model_file_preflight_failed'


def test_compute_node_runtime_replaces_stale_error_on_runtime_shape_failure():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.last_runtime_init_error = 'llama_cpp_import_timeout after 0.01s'
    model_manager.get_llm_instance.return_value = object()

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=MagicMock(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager.last_runtime_init_error == 'runtime_missing_create_chat_completion'


def test_llama_cpp_runtime_reuses_desktop_probe_and_skips_child_import_watchdog(monkeypatch):
    import types
    from utils.llm import model_manager as llama_model_manager

    fake_module = types.SimpleNamespace(__file__='/opt/site-packages/llama_cpp/__init__.py', Llama=object)
    monkeypatch.setattr(
        llama_model_manager,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 3},
    )
    monkeypatch.setattr(
        llama_model_manager,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: pytest.fail('desktop runtime probe should avoid child discovery'),
    )
    monkeypatch.setattr(
        llama_model_manager,
        '_run_llama_cpp_import_watchdog',
        lambda **_kwargs: pytest.fail('startup-critical child import watchdog must not run'),
    )
    monkeypatch.setattr(
        llama_model_manager,
        '_import_llama_cpp_in_parent_with_timeout',
        lambda **_kwargs: fake_module,
    )

    imported = llama_model_manager._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'interpreter': '/python',
            'prefix': '/prefix',
            'llama_module_path': '/opt/site-packages/llama_cpp/__init__.py',
            'fallback_reason': '',
        },
    )

    assert imported is fake_module


def test_llama_cpp_runtime_rejects_desktop_probe_import_mismatch(monkeypatch):
    import types
    from utils.llm import model_manager as llama_model_manager

    fake_module = types.SimpleNamespace(__file__='/other/site-packages/llama_cpp/__init__.py', Llama=object)
    monkeypatch.setattr(
        llama_model_manager,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 3},
    )
    monkeypatch.setattr(
        llama_model_manager,
        '_import_llama_cpp_in_parent_with_timeout',
        lambda **_kwargs: fake_module,
    )

    with pytest.raises(ImportError, match='Desktop runtime probe module path mismatch'):
        llama_model_manager._import_llama_cpp_runtime(
            require_real_runtime=True,
            desktop_runtime_probe={
                'selected_backend': 'metal',
                'gpu_offload_supported': True,
                'detected_device': 'metal',
                'interpreter': '/python',
                'prefix': '/prefix',
                'llama_module_path': '/opt/site-packages/llama_cpp/__init__.py',
                'fallback_reason': '',
            },
        )


def test_compute_node_runtime_stop_skips_unregister_before_api_v1_registration():
    relay_client = MagicMock()
    relay_client._api_v1_registered_relays = set()
    model_manager = MagicMock()
    model_manager.use_mock_llm = True

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url='https://token.place', relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    runtime.stop()

    relay_client.stop.assert_called_once_with()
    relay_client.unregister_from_relay.assert_not_called()


def test_compute_node_runtime_stop_skips_unregister_when_registration_state_missing():
    relay_client = MagicMock()
    del relay_client._api_v1_registered_relays
    model_manager = MagicMock()
    model_manager.use_mock_llm = True

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url='https://token.place', relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    runtime.stop()

    relay_client.stop.assert_called_once_with()
    relay_client.unregister_from_relay.assert_not_called()


def test_compute_node_runtime_stop_skips_unregister_for_non_set_registration_state():
    relay_client = MagicMock()
    relay_client._api_v1_registered_relays = ['https://token.place']
    model_manager = MagicMock()
    model_manager.use_mock_llm = True

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url='https://token.place', relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    runtime.stop()

    relay_client.stop.assert_called_once_with()
    relay_client.unregister_from_relay.assert_not_called()


def test_process_relay_request_result_preserves_error_envelope_submission_semantics():
    from utils.processing_result import RelayProcessingResult

    class Adapter:
        def can_process(self, _payload):
            return True

        def process(self, _payload):
            return RelayProcessingResult(
                inference_succeeded=False,
                submitted=True,
                safe_error_code="compute_node_internal_error",
                runtime_healthy=False,
            )

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        relay_client=MagicMock(),
        model_manager=MagicMock(use_mock_llm=True),
        crypto_manager=MagicMock(),
        request_adapters=[Adapter()],
    )

    result = runtime.process_relay_request_result({"request_id": "req-1"})
    assert bool(result) is True
    assert runtime.process_relay_request({"request_id": "req-1"}) is True
    assert result.submitted is True
    assert result.inference_succeeded is False
    assert result.safe_error_code == "compute_node_internal_error"
    assert result.runtime_healthy is False
