import json
import threading
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
    _classify_completion_smoke_exception,
    _completion_smoke_reason_from_api_v1_error,
    _readiness_smoke_model_id,
    _safe_completion_smoke_worker_diagnostics,
    _qwen_64k_readiness_profile_recoverable,
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

    def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def test_first_env_skips_blank_values(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "   ")
    monkeypatch.setenv("TOKEN_PLACE_RELAY_URL", "https://fallback.example")

    assert first_env(["TOKENPLACE_RELAY_URL", "TOKEN_PLACE_RELAY_URL"]) == "https://fallback.example"


def test_completion_smoke_worker_diagnostic_sanitizer_covers_safe_value_shapes():
    safe = _safe_completion_smoke_worker_diagnostics(
        {
            "retryable": True,
            "runtime_healthy": False,
            "stream": None,
            "context_window_tokens": 65536,
            "recovery_attempted": 1,
            "exception_type": "RuntimeError",
            "rejected_option": "temperature",
            "profile_id": "qwen3-8b-q4-k-m",
            "context_tier": "64k-full",
            "type_k": "q8_0",
            "type_v": "q8_0",
            "sanitized_error_summary": "RuntimeError:kv_cache_allocation",
            "stderr_tail": "llama_context kv cache allocation failed redacted",
            "child_stderr_tail": "worker exception redacted",
            "method": "create_chat_completion",
            "reason": "malformed_completion_output",
            "generation_exception_category": "malformed_completion_output",
            "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
            "plain_completion_prompt_tokenization_method": "llama.tokenize",
            "plain_completion_prompt_tokenization_attempted": True,
            "plain_completion_prompt_tokenization_special": True,
            "plain_completion_prompt_token_count": 3,
            "plain_completion_reset_after_failure_count": 2,
            "unsafe_prompt": "Reply with exactly: ok",
            "rendered_prompt": "SECRET rendered prompt",
            "token_ids": [1, 2, 3],
            "assistant_output": "SECRET output",
            "key": "SECRET key",
            "tool_args": {"secret": True},
            "ciphertext": "SECRET ciphertext",
            "nested": {"rendered_prompt": "secret"},
            "content": "assistant output",
        }
    )

    assert safe == {
        "retryable": True,
        "runtime_healthy": False,
        "stream": None,
        "context_window_tokens": 65536,
        "recovery_attempted": 1,
        "exception_type": "RuntimeError",
        "rejected_option": "temperature",
        "profile_id": "qwen3-8b-q4-k-m",
        "context_tier": "64k-full",
        "type_k": "q8_0",
        "type_v": "q8_0",
        "sanitized_error_summary": "RuntimeError:kv_cache_allocation",
        "stderr_tail": "llama_context kv cache allocation failed redacted",
        "child_stderr_tail": "worker exception redacted",
        "method": "create_chat_completion",
        "reason": "malformed_completion_output",
        "generation_exception_category": "malformed_completion_output",
        "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
        "plain_completion_prompt_tokenization_method": "llama.tokenize",
        "plain_completion_prompt_tokenization_attempted": True,
        "plain_completion_prompt_tokenization_special": True,
        "plain_completion_prompt_token_count": 3,
        "plain_completion_reset_after_failure_count": 2,
    }
    assert "Reply with exactly" not in json.dumps(safe)
    assert "assistant output" not in json.dumps(safe)


def test_completion_smoke_worker_diagnostic_sanitizer_covers_new_edge_shapes():
    safe = _safe_completion_smoke_worker_diagnostics(
        {
            "plain_completion_backend_state_sticky": "true",
            "plain_completion_backend_recreation_required": "false",
            "plain_completion_metal_command_buffer_status": "7",
            "attempted_plain_completion_methods": "",
            "plain_completion_attempt_safe_summaries": "",
            "child_stderr_tail": "metal buffer failed redacted",
        }
    )

    assert "plain_completion_backend_state_sticky" not in safe
    assert "plain_completion_backend_recreation_required" not in safe
    assert "plain_completion_metal_command_buffer_status" not in safe
    assert safe["attempted_plain_completion_methods"] == ""
    assert safe["plain_completion_attempt_safe_summaries"] == ""
    assert safe["child_stderr_tail"] == "metal buffer failed redacted"


def test_payload_helpers_reject_non_dict_and_compute_mode_cpu_fallback():
    assert is_legacy_relay_payload(["not", "dict"]) is False
    assert is_api_v1_relay_payload("not-dict") is False

    manager = SimpleNamespace(
        requested_compute_mode="cpu",
        last_compute_diagnostics={"requested_mode": "gpu"},
    )

    assert compute_mode_diagnostics(manager) == {
        "requested_mode": "cpu",
        "effective_mode": "cpu",
        "backend_available": "unknown",
        "backend_selected": "cpu",
        "backend_used": "cpu",
        "n_gpu_layers": 0,
        "fallback_reason": None,
    }


def test_completion_smoke_worker_diagnostic_sanitizer_drops_unsafe_shapes():
    unsafe = _safe_completion_smoke_worker_diagnostics(
        {
            "exception_type": "RuntimeError with spaces",
            "rejected_option": "temperature secret prompt text!",
            "sanitized_error_summary": "RuntimeError:redacted prompt Reply with exactly ok",
            "stderr_tail": "",
            "child_stderr_tail": "redacted prompt assistant output",
            "method": "create_chat_completion Reply with exactly ok",
            "reason": "prompt leaked in reason",
            "generation_exception_category": "prompt_text",
            "worker_diagnostics": {"rendered_prompt": "Reply with exactly ok"},
        }
    )

    assert unsafe == {}
    assert _safe_completion_smoke_worker_diagnostics("not-a-dict") == {}


@pytest.mark.parametrize(
    "category",
    [
        "context_window_exceeded",
        "context_length_exceeded",
        "token_overflow",
    ],
)
def test_completion_smoke_worker_diagnostic_sanitizer_preserves_tokenization_length_categories(category):
    safe = _safe_completion_smoke_worker_diagnostics(
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


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        ({"internal_reason": "runtime_unsupported_generation_kwarg"}, "runtime_completion_smoke_unsupported_generation_kwarg"),
        ({"internal_reason": "runtime_rope_yarn_eval_failure"}, "runtime_completion_smoke_rope_yarn_eval_failure"),
        ({"internal_reason": "runtime_metal_memory_allocation"}, "runtime_completion_smoke_metal_memory_allocation"),
        ({"internal_reason": "runtime_kv_cache_allocation"}, "runtime_completion_smoke_kv_cache_allocation"),
        ({"internal_reason": "runtime_worker_timeout"}, "runtime_completion_smoke_worker_timeout"),
        ({"internal_reason": "runtime_worker_dead"}, "runtime_completion_smoke_worker_dead"),
        ({"code": "compute_node_options_unsupported"}, "runtime_completion_smoke_unsupported_generation_kwarg"),
        # Top-level generation_exception_category checks.
        ({"generation_exception_category": "empty_completion_output"}, "runtime_completion_smoke_plain_completion_empty_output"),
        ({"generation_exception_category": "thinking_leaked"}, "runtime_completion_smoke_plain_completion_thinking_leaked"),
        ({"generation_exception_category": "malformed_completion_output"}, "runtime_completion_smoke_plain_completion_malformed_output"),
        ({"generation_exception_category": "method_shape"}, "runtime_completion_smoke_plain_completion_method_shape"),
        ({"generation_exception_category": "unsupported_prompt_kwarg"}, "runtime_completion_smoke_plain_completion_method_shape"),
        ({"generation_exception_category": "prompt_tokenization_failure"}, "runtime_completion_smoke_plain_completion_prompt_tokenization_failure"),
        ({"generation_exception_category": "prompt_eval_failure"}, "runtime_completion_smoke_plain_completion_eval_failure"),
        ({"generation_exception_category": "sampling_failure"}, "runtime_completion_smoke_plain_completion_sampling_failure"),
        # Relay path: generation_exception_category nested inside worker_diagnostics.
        ({"worker_diagnostics": {"generation_exception_category": "method_shape"}}, "runtime_completion_smoke_plain_completion_method_shape"),
        ({"worker_diagnostics": {"generation_exception_category": "unsupported_prompt_kwarg"}}, "runtime_completion_smoke_plain_completion_method_shape"),
        ({"worker_diagnostics": {"generation_exception_category": "empty_completion_output"}}, "runtime_completion_smoke_plain_completion_empty_output"),
        ({"worker_diagnostics": {"generation_exception_category": "thinking_leaked"}}, "runtime_completion_smoke_plain_completion_thinking_leaked"),
        ({"worker_diagnostics": {"generation_exception_category": "malformed_completion_output"}}, "runtime_completion_smoke_plain_completion_malformed_output"),
        # Top-level takes precedence over nested.
        ({"generation_exception_category": "empty_completion_output", "worker_diagnostics": {"generation_exception_category": "thinking_leaked"}}, "runtime_completion_smoke_plain_completion_empty_output"),
        # Render-path kwarg rejections map to the render-specific reason.
        ({"generation_exception_category": "unsupported_render_kwarg"}, "runtime_completion_smoke_render_template_unexpected_kwarg"),
        ({"worker_diagnostics": {"generation_exception_category": "unsupported_render_kwarg"}}, "runtime_completion_smoke_render_template_unexpected_kwarg"),
        # Relay promotes both internal_reason and generation_exception_category when category is unsupported_render_kwarg;
        # generation_exception_category wins (dict lookup before internal_reason checks).
        ({"internal_reason": "unsupported_render_kwarg", "generation_exception_category": "unsupported_render_kwarg"}, "runtime_completion_smoke_render_template_unexpected_kwarg"),
        # Template-path internal_reason values map to the render-exception reason.
        ({"internal_reason": "runtime_chat_template_metadata_missing"}, "runtime_completion_smoke_render_template_exception"),
        ({"internal_reason": "runtime_chat_template_renderer_unavailable"}, "runtime_completion_smoke_render_template_exception"),
        ({"internal_reason": "runtime_chat_template_qwen_evidence_missing"}, "runtime_completion_smoke_render_template_exception"),
        ({"internal_reason": "runtime_chat_template_render_exception"}, "runtime_completion_smoke_render_template_exception"),
    ],
)
def test_completion_smoke_reason_from_api_v1_error_maps_runtime_reasons(error, expected_reason):
    assert _completion_smoke_reason_from_api_v1_error(error) == expected_reason


@pytest.mark.parametrize(
    ("exc", "expected_category", "expected_reason"),
    [
        (TimeoutError("worker timeout"), "worker_timeout", "runtime_completion_smoke_worker_timeout"),
        (RuntimeError("worker dead: broken pipe"), "worker_dead", "runtime_completion_smoke_worker_dead"),
        (RuntimeError("Metal buffer allocation out of memory"), "metal_memory_allocation", "runtime_completion_smoke_metal_memory_allocation"),
        (RuntimeError("KV cache allocation failed"), "kv_cache_allocation", "runtime_completion_smoke_kv_cache_allocation"),
        (RuntimeError("failed to tokenize prompt"), "prompt_tokenization_failure", "runtime_completion_smoke_plain_completion_prompt_tokenization_failure"),
        (RuntimeError("llama_decode failed to eval prompt"), "prompt_eval_failure", "runtime_completion_smoke_plain_completion_eval_failure"),
        (RuntimeError("llama_decode returned -3"), "backend_graph_compute_failure", "runtime_completion_smoke_backend_graph_compute_failure"),
        (RuntimeError("llama_decode returned 2"), "decode_aborted", "runtime_completion_smoke_decode_aborted"),
        (RuntimeError("llama_decode returned -4"), "backend_decode_failure", "runtime_completion_smoke_backend_decode_failure"),
        (RuntimeError("sampler failed with no logits"), "sampling_failure", "runtime_completion_smoke_plain_completion_sampling_failure"),
        (RuntimeError("unexpected keyword argument 'mirostat'"), "unsupported_generation_kwarg", "runtime_completion_smoke_plain_completion_unexpected_kwarg"),
        (RuntimeError("unclassified failure with prompt text"), "unknown_generation_exception", "runtime_completion_smoke_exception"),
    ],
)
def test_classify_completion_smoke_exception_uses_safe_specific_reasons(exc, expected_category, expected_reason):
    category, reason, diagnostics = _classify_completion_smoke_exception(exc)

    assert category == expected_category
    assert reason == expected_reason
    assert diagnostics["exception_type"] == type(exc).__name__
    assert diagnostics["sanitized_error_summary"] == f"{type(exc).__name__}:redacted"
    if "llama_decode returned" in str(exc):
        assert diagnostics["plain_completion_eval_return_code"] == int(str(exc).rsplit(" ", 1)[-1])
    assert "prompt text" not in json.dumps(diagnostics)



def test_completion_smoke_worker_diagnostic_sanitizer_covers_rejected_value_types_and_unknown_keys():
    safe = _safe_completion_smoke_worker_diagnostics(
        {
            "retryable": {"nested": "not allowed"},
            "unallowlisted": "safe-looking-but-not-allowed",
            "stderr_tail": "llama kv cache allocation failed " + "x" * 1300,
            "profile_id": "profile id with spaces",
            "exception_type": "RuntimeError",
        }
    )

    assert safe == {"exception_type": "RuntimeError"}


class _SmokeDiagnosticsError(RuntimeError):
    def __init__(self, diagnostics):
        super().__init__("worker diagnostics should drive smoke reason")
        self.diagnostics = diagnostics


def test_classify_completion_smoke_exception_uses_safe_worker_category_and_reason():
    category, reason, diagnostics = _classify_completion_smoke_exception(
        _SmokeDiagnosticsError(
            {
                "generation_exception_category": "worker_dead",
                "reason": "unsupported_generation_option",
                "prompt": "Reply with exactly: ok",
            }
        )
    )

    assert category == "worker_dead"
    assert reason == "runtime_completion_smoke_worker_dead"
    assert diagnostics["worker_diagnostics"] == {
        "generation_exception_category": "worker_dead",
        "reason": "unsupported_generation_option",
    }
    assert "Reply with exactly" not in json.dumps(diagnostics)


def test_classify_completion_smoke_exception_uses_safe_worker_unsupported_reason_without_category():
    category, reason, diagnostics = _classify_completion_smoke_exception(
        _SmokeDiagnosticsError({"reason": "unsupported_generation_option"})
    )

    assert category == "unsupported_generation_kwarg"
    assert reason == "runtime_completion_smoke_plain_completion_unexpected_kwarg"
    assert diagnostics["worker_diagnostics"] == {"reason": "unsupported_generation_option"}


def test_classify_completion_smoke_exception_uses_unsupported_render_kwarg_worker_category():
    """Worker diagnostics with unsupported_render_kwarg category maps to render-specific reason."""
    exc = _SmokeDiagnosticsError(
        {
            "generation_exception_category": "unsupported_render_kwarg",
            "rejected_generation_kwarg": "tokenize",
            "method": "apply_chat_template",
            "prompt": "plaintext prompt must not appear",
        }
    )
    category, reason, diagnostics = _classify_completion_smoke_exception(exc)

    assert category == "unsupported_render_kwarg"
    assert reason == "runtime_completion_smoke_render_template_unexpected_kwarg"
    safe_worker = diagnostics["worker_diagnostics"]
    assert safe_worker["generation_exception_category"] == "unsupported_render_kwarg"
    assert safe_worker["rejected_generation_kwarg"] == "tokenize"
    assert safe_worker["method"] == "apply_chat_template"
    assert "plaintext prompt" not in json.dumps(diagnostics)


def test_completion_smoke_reason_prefers_nested_worker_specific_category_over_generic_internal_reason():
    error = {
        'internal_reason': 'unsupported_generation_option',
        'worker_diagnostics': {
            'generation_exception_category': 'unsupported_prompt_kwarg',
            'rejected_generation_kwarg': 'prompt',
            'attempted_generation_kwargs': 'max_tokens,prompt',
            'attempted_plain_completion_methods': 'create_completion_keyword_prompt',
            'method': 'create_completion_keyword_prompt',
            'prompt': 'plaintext prompt must not appear',
        },
    }

    reason = _completion_smoke_reason_from_api_v1_error(error)
    safe = _safe_completion_smoke_worker_diagnostics(error['worker_diagnostics'])

    assert reason == 'runtime_completion_smoke_plain_completion_method_shape'
    assert safe == {
        'generation_exception_category': 'unsupported_prompt_kwarg',
        'rejected_generation_kwarg': 'prompt',
        'attempted_generation_kwargs': 'max_tokens,prompt',
        'attempted_plain_completion_methods': 'create_completion_keyword_prompt',
        'method': 'create_completion_keyword_prompt',
    }
    assert 'plaintext prompt' not in json.dumps(safe)


def test_completion_smoke_reason_maps_top_level_render_kwarg_rejection_by_method():
    error = {
        'internal_reason': 'unsupported_generation_option',
        'rejected_generation_kwarg': 'enable_thinking',
        'method': 'apply_chat_template',
    }

    assert _completion_smoke_reason_from_api_v1_error(error) == (
        'runtime_completion_smoke_render_template_unexpected_kwarg'
    )


def test_completion_smoke_reason_maps_top_level_plain_completion_kwarg_rejection_by_method():
    error = {
        'internal_reason': 'unsupported_generation_option',
        'rejected_generation_kwarg': 'stream',
        'method': 'llama_call_positional_prompt',
    }

    assert _completion_smoke_reason_from_api_v1_error(error) == (
        'runtime_completion_smoke_plain_completion_unexpected_kwarg'
    )


def test_classify_completion_smoke_exception_detects_rope_scaling_text():
    category, reason, diagnostics = _classify_completion_smoke_exception(
        RuntimeError("RoPE scaling failure before eval")
    )

    assert category == "rope_yarn_eval_failure"
    assert reason == "runtime_completion_smoke_rope_yarn_eval_failure"
    assert diagnostics["sanitized_error_summary"] == "RuntimeError:redacted"


def test_readiness_smoke_model_id_falls_back_to_model_path_basename_and_empty():
    manager = SimpleNamespace(api_model_id="  ", model_id=None, file_name="", model_path="/models/Qwen3-8B.gguf")
    assert _readiness_smoke_model_id(manager) == "Qwen3-8B.gguf"
    assert _readiness_smoke_model_id(SimpleNamespace()) == ""

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
            return {}

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 1}

    def _raise_admission_error(**_kwargs):
        raise TimeoutError("relay admission timed out")

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "profile_id": "qwen3-8b-q4-k-m",
    }
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
            return {}

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
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

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
    model_manager.last_yarn_rope_diagnostics = {
        "supported": True,
        "missing_reason": None,
        "qwen_yarn_requested_context_tokens": 65536,
        "qwen_yarn_original_context_tokens": 32768,
        "qwen_yarn_context_multiplier": 2.0,
        "qwen_yarn_rope_freq_scale": 0.5,
        "qwen_yarn_ext_factor_overridden": False,
        "qwen_yarn_rope_scaling_type_source": "enum",
        "qwen_yarn_configuration_valid": True,
    }
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
    assert diagnostics["api_v1_readiness_yarn_requested_context_tokens"] == 65536
    assert diagnostics["api_v1_readiness_yarn_context_multiplier"] == 2.0
    assert diagnostics["api_v1_readiness_yarn_rope_freq_scale"] == 0.5
    assert diagnostics["api_v1_readiness_yarn_ext_factor_overridden"] is False
    assert diagnostics["api_v1_readiness_yarn_rope_scaling_type_source"] == "enum"
    assert diagnostics["api_v1_readiness_yarn_configuration_valid"] is True
    assert diagnostics["api_v1_readiness_tokenizer_render_bridge_available"] is True


def test_compute_node_runtime_qwen_64k_readiness_rejects_missing_yarn_rope():
    class ReadyRuntime:
        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}

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
            "required_for_tier": "64k-full",
        },
    }
    model_manager.context_tier = "64k-full"
    model_manager.context_window_tokens = 65536
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.last_yarn_rope_diagnostics = {
        "supported": False,
        "missing_reason": "missing LLAMA_ROPE_SCALING_TYPE_YARN enum constant",
    }
    model_manager.get_llm_instance.return_value = ReadyRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_yarn_rope_enabled"] is False
    assert diagnostics["api_v1_readiness_error_code"] == "compute_node_yarn_rope_unsupported"
    assert diagnostics["api_v1_readiness_error_reason"] == (
        "missing LLAMA_ROPE_SCALING_TYPE_YARN enum constant"
    )


def test_compute_node_runtime_qwen_8k_readiness_ignores_missing_yarn_rope_support():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "rope_scaling_policy": {
            "type": "yarn",
            "factor": 2.0,
            "original_context_tokens": 32768,
            "required_for_tier": "64k-full",
        },
    }
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.last_yarn_rope_diagnostics = {
        "supported": False,
        "required": False,
        "missing_reason": "not_required_for_active_profile_or_tier",
    }
    model_manager.get_llm_instance.return_value = _ReadyRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_context_tier"] == "8k-fast"
    assert diagnostics["api_v1_runtime_ready"] is True

def test_compute_node_runtime_readiness_smoke_completion_passes(monkeypatch):
    class SmokeRuntime:
        def __init__(self):
            self.completion_kwargs = None

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **kwargs):
            self.completion_kwargs = {**kwargs, "messages": messages}
            return {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "profile_id": "qwen3-8b-q4-k-m",
    }
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
    assert diagnostics["api_v1_readiness_model_profile_id"] == "qwen3-8b-q4-k-m"
    assert llm_runtime.completion_kwargs["max_tokens"] == 64
    assert "stream" not in llm_runtime.completion_kwargs
    assert "stop" not in llm_runtime.completion_kwargs
    assert llm_runtime.completion_kwargs["messages"][-1]["content"] == "Reply with exactly: ok"




def test_compute_node_runtime_readiness_smoke_completion_accepts_empty_qwen_think_wrapper(monkeypatch):
    class SmokeRuntime:
        def __init__(self):
            self.completion_kwargs = None

        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **kwargs):
            self.completion_kwargs = {**kwargs, "messages": messages}
            return {"choices": [{"message": {"role": "assistant", "content": "<think>\n\n</think>\n\nok"}}]}

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    llm_runtime = SmokeRuntime()
    model_manager.get_llm_instance.return_value = llm_runtime
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2)
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert diagnostics["api_v1_readiness_completion_smoke_shape"] == "api_v1_assistant_message"
    assert llm_runtime.completion_kwargs["max_tokens"] == 64
    assert llm_runtime.completion_kwargs["messages"][-1]["content"] == "Reply with exactly: ok"


def test_compute_node_runtime_readiness_smoke_completion_empty_after_strip(monkeypatch):
    class SmokeRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "<think></think>"}}]}

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = SmokeRuntime()
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
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_empty_after_think_strip"


def test_compute_node_runtime_readiness_smoke_completion_rejects_empty_output(monkeypatch):
    class EmptyRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "   "}}]}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = EmptyRuntime()
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
    assert diagnostics["api_v1_readiness_completion_smoke_failure_reason"] == "runtime_completion_smoke_invalid_model_output"
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_invalid_model_output"


def test_compute_node_runtime_readiness_smoke_completion_rejects_malformed_shape(monkeypatch):
    class MalformedRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **_kwargs):
            return {"choices": []}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = MalformedRuntime()
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
    assert diagnostics["api_v1_readiness_completion_smoke_failure_reason"] == "runtime_completion_smoke_invalid_model_output"
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_invalid_model_output"


def test_compute_node_runtime_readiness_smoke_completion_rejects_invalid_shared_envelope(monkeypatch):
    class EnvelopeRelayClient:
        def _api_v1_authoritative_context_admission(self, **_kwargs):
            return True, None, 2

        def _generate_api_v1_response_with_runtime_model(self, **_kwargs):
            return {"api_v1_response": {"message": {"role": "tool", "content": "not assistant"}}}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = _ReadyRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=EnvelopeRelayClient(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "failed"
    assert diagnostics["api_v1_readiness_completion_smoke_failure_reason"] == "runtime_completion_smoke_invalid_model_output"
    assert diagnostics["api_v1_readiness_completion_smoke_shape"] == "invalid_api_v1_envelope"


def test_compute_node_runtime_readiness_smoke_completion_rejects_missing_content(monkeypatch):
    class MissingContentRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"message": {"role": "assistant"}}]}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = MissingContentRuntime()
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
    assert diagnostics["api_v1_readiness_completion_smoke_failure_reason"] == "runtime_completion_smoke_invalid_model_output"
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_invalid_model_output"


def test_compute_node_runtime_api_v1_error_falls_back_to_redacted_safe_summary(monkeypatch):
    class ErrorEnvelopeRelayClient:
        def _api_v1_authoritative_context_admission(self, **_kwargs):
            return True, None, 2

        def _generate_api_v1_response_with_runtime_model(self, **_kwargs):
            return {
                "api_v1_response": {
                    "error": {
                        "code": "compute_node_context_admission_unavailable",
                        "internal_reason": "runtime_completion_smoke_worker_exception",
                        "exception_type": "RuntimeError",
                    }
                }
            }

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = _ReadyRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=ErrorEnvelopeRelayClient(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "RuntimeError"
    assert diagnostics["api_v1_readiness_completion_smoke_safe_summary"] == "RuntimeError:redacted"


def test_compute_node_runtime_exception_path_promotes_nested_worker_failure_details(monkeypatch):
    class NestedWorkerDiagnosticException(Exception):
        def __init__(self):
            super().__init__("outer wrapper with SECRET_PROMPT")
            self.diagnostics = {
                "worker_diagnostics": {
                    "prompt": "SECRET_PROMPT",
                    "rendered_prompt": "SECRET_RENDERED_PROMPT",
                    "assistant_output": "SECRET_OUTPUT",
                    "decrypted_payload": "SECRET_PAYLOAD",
                    "ciphertext": "SECRET_CIPHERTEXT",
                    "key": "SECRET_KEY",
                    "tool_args": {"secret": True},
                    "method": "create_completion_keyword_prompt",
                    "attempted_generation_kwargs": "max_tokens,prompt",
                    "attempted_plain_completion_methods": "create_completion_keyword_prompt",
                    "generation_exception_category": "worker_timeout",
                    "exception_type": "TimeoutError",
                    "sanitized_error_summary": "TimeoutError:redacted",
                    "plain_completion_create_completion_callable": True,
                    "plain_completion_llama_call_callable": True,
                    "plain_completion_signature_inspectable": True,
                    "plain_completion_accepts_prompt_kwarg": True,
                    "plain_completion_accepts_max_tokens_kwarg": True,
                    "plain_completion_accepts_var_kwargs": False,
                    "qwen_api_v1_non_thinking_template_fallback": True,
                }
            }

    class RaisingRelayClient:
        def _api_v1_authoritative_context_admission(self, **_kwargs):
            return True, None, 2

        def _generate_api_v1_response_with_runtime_model(self, **_kwargs):
            raise NestedWorkerDiagnosticException()

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = _ReadyRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=RaisingRelayClient(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_method"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_generation_kwargs"] == "max_tokens,prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_plain_completion_methods"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == "worker_timeout"
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "TimeoutError"
    assert diagnostics["api_v1_readiness_completion_smoke_safe_summary"] == "TimeoutError:redacted"
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_create_completion_callable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_llama_call_callable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_signature_inspectable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs"] is False
    assert diagnostics["api_v1_readiness_completion_smoke_qwen_api_v1_non_thinking_template_fallback"] is True
    dumped = json.dumps(diagnostics)
    for unsafe_key in (
        "prompt",
        "rendered_prompt",
        "assistant_output",
        "decrypted_payload",
        "ciphertext",
        "key",
        "tool_args",
    ):
        assert f'"{unsafe_key}"' not in dumped
    assert "SECRET_" not in dumped

def test_compute_node_runtime_promotes_nested_worker_diagnostics_to_flat_readiness_fields(monkeypatch):
    class NestedWorkerDiagnosticRelayClient:
        def _api_v1_authoritative_context_admission(self, **_kwargs):
            return True, None, 2

        def _generate_api_v1_response_with_runtime_model(self, **_kwargs):
            return {
                "api_v1_response": {
                    "error": {
                        "code": "compute_node_internal_error",
                        "worker_diagnostics": {
                            "rejected_generation_kwarg": "stream",
                        "attempted_generation_kwargs": "max_tokens,stream",
                        "attempted_plain_completion_methods": "create_completion_keyword_prompt",
                        "result_shape": "dict_malformed",
                        "plain_completion_prompt_tokenization_attempted": True,
                        "plain_completion_prompt_token_count": 3,
                        "plain_completion_prompt_tokenization_method": "llama.tokenize",
                        "plain_completion_prompt_tokenization_special": True,
                        "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
                        "plain_completion_reset_after_failure_count": 2,
                        "plain_completion_eval_return_code": 1,
                        "prompt": "SECRET_PROMPT",
                        "rendered_prompt": "SECRET_RENDERED_PROMPT",
                        "token_ids": [1, 2, 3],
                        "assistant_output": "SECRET_OUTPUT",
                        "key": "SECRET_KEY",
                        "tool_args": {"secret": True},
                        "ciphertext": "SECRET_CIPHERTEXT",
                    },
                }
            }
            }

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = _ReadyRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=NestedWorkerDiagnosticRelayClient(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_worker_diagnostics"] == {
        "rejected_generation_kwarg": "stream",
        "attempted_generation_kwargs": "max_tokens,stream",
        "attempted_plain_completion_methods": "create_completion_keyword_prompt",
        "result_shape": "dict_malformed",
        "plain_completion_prompt_tokenization_attempted": True,
        "plain_completion_prompt_token_count": 3,
        "plain_completion_prompt_tokenization_method": "llama.tokenize",
        "plain_completion_prompt_tokenization_special": True,
        "plain_completion_prompt_tokenization_error_category": "prompt_tokenization_failure",
        "plain_completion_reset_after_failure_count": 2,
        "plain_completion_eval_return_code": 1,
    }
    assert diagnostics["api_v1_readiness_completion_smoke_rejected_generation_kwarg"] == "stream"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_generation_kwargs"] == "max_tokens,stream"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_plain_completion_methods"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_result_shape"] == "dict_malformed"
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_token_count"] == 3
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method"] == "llama.tokenize"
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category"] == "prompt_tokenization_failure"
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_reset_after_failure_count"] == 2
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_eval_return_code"] == 1
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_token_count"] == 3
    assert "SECRET" not in json.dumps(diagnostics)

def test_compute_node_runtime_qwen_readiness_smoke_completion_is_required_without_env(monkeypatch):
    class ThinkRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "<THINK>no"}}]}

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

    monkeypatch.delenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", raising=False)
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
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_thinking_leaked"


def test_compute_node_runtime_readiness_smoke_completion_rejects_think_output(monkeypatch):
    class ThinkRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "<think>no"}}]}

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

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
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_thinking_leaked"


def test_compute_node_runtime_readiness_smoke_completion_rejects_reasoning_content(monkeypatch):
    class ReasoningRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ready",
                            "reasoning_content": "secret hidden reasoning",
                        }
                    }
                ]
            }

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = ReasoningRuntime()
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
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_thinking_leaked"
    assert diagnostics["api_v1_readiness_completion_smoke_shape"] == "api_v1_error"
    assert "secret hidden reasoning" not in json.dumps(diagnostics)


def test_compute_node_runtime_readiness_smoke_completion_accepts_text_choice(monkeypatch):
    class TextRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion(self, **_kwargs):
            return {"choices": [{"text": "ready"}]}

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "local-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = TextRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2)
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert diagnostics["api_v1_readiness_result"] == "passed"


def test_compute_node_runtime_readiness_smoke_uses_configured_model_id_fallback(monkeypatch):
    observed = {}

    def generate(**kwargs):
        observed.update(kwargs)
        return {
            "api_v1_response": {
                "message": {"role": "assistant", "content": "ok"},
            }
        }

    monkeypatch.setenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION", "1")
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "local", "thinking_mode": "n/a"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = None
    model_manager.model_id = "configured-runtime-model"
    model_manager.last_compute_diagnostics = {}
    model_manager.get_llm_instance.return_value = SimpleNamespace(
        create_chat_completion=lambda **_kwargs: {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
    )
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 2),
            _generate_api_v1_response_with_runtime_model=generate,
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert observed["model_id"] == "configured-runtime-model"


def test_compute_node_runtime_readiness_smoke_completion_records_safe_exception(monkeypatch):
    class RaisingRuntime:
        def render_and_tokenize_chat(self, *_args, **_kwargs):
            return {"prompt_tokens": 2}

        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise RuntimeError("prompt text must not leak")

        def create_chat_completion(self, **_kwargs):
            raise AssertionError("Qwen readiness must use render-then-complete")

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
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_exception"
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


def test_llama_cpp_runtime_reuses_private_env_probe_for_matching_public_probe(monkeypatch):
    import types
    from utils.llm import model_manager as llama_model_manager

    fake_module = types.SimpleNamespace(__file__='/opt/site-packages/llama_cpp/__init__.py', Llama=object)
    monkeypatch.setenv(
        llama_model_manager.DESKTOP_RUNTIME_PROBE_ENV,
        json.dumps(
            {
                'runtime_action': 'already_supported',
                'selected_backend': 'cuda',
                'gpu_offload_supported': True,
                'detected_device': 'cuda',
                'interpreter': '/python',
                'prefix': '/prefix',
                'llama_module_path': '/opt/site-packages/llama_cpp/__init__.py',
            }
        ),
    )
    monkeypatch.setattr(
        llama_model_manager,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 3},
    )
    monkeypatch.setattr(
        llama_model_manager,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: pytest.fail('matching private env probe should avoid child discovery'),
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
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'interpreter': '/python',
            'prefix': '/prefix',
        },
    )

    assert imported is fake_module


def test_effective_desktop_runtime_probe_rejects_private_env_probe_identity_mismatch(monkeypatch):
    from utils.llm import model_manager as llama_model_manager

    monkeypatch.setenv(
        llama_model_manager.DESKTOP_RUNTIME_PROBE_ENV,
        json.dumps(
            {
                'runtime_action': 'already_supported',
                'selected_backend': 'cuda',
                'gpu_offload_supported': True,
                'detected_device': 'cuda',
                'interpreter': '/python',
                'prefix': '/prefix',
                'llama_module_path': '/opt/site-packages/llama_cpp/__init__.py',
            }
        ),
    )

    effective = llama_model_manager._effective_desktop_runtime_probe(
        {
            'runtime_action': 'metal_already_supported',
            'selected_backend': 'metal',
            'gpu_offload_supported': True,
            'detected_device': 'metal',
            'interpreter': '/python',
            'prefix': '/prefix',
        }
    )

    assert effective is not None
    assert effective['backend'] == 'metal'
    assert effective['runtime_action'] == 'metal_already_supported'
    assert effective['llama_module_path'] == 'unknown'


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


def _qwen_64k_model_manager(runtime):
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_path = "/tmp/Qwen3-8B-Q4_K_M.gguf"
    model_manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "profile_id": "qwen3-8b-q4-k-m",
        "chat_template_policy": "gguf-jinja",
        "rope_scaling_policy": {
            "type": "yarn",
            "required_for_tier": "64k-full",
            "factor": 2.0,
            "original_context_tokens": 32768,
        },
    }
    model_manager.context_tier = "64k-full"
    model_manager.context_window_tokens = 65536
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_yarn_rope_diagnostics = {
        "supported": True,
        "yarn_resolver_source": "numeric_fallback",
        "llama_cpp_python_version": "0.3.32",
        "qwen_yarn_requested_context_tokens": 65536,
        "qwen_yarn_original_context_tokens": 32768,
        "qwen_yarn_context_multiplier": 2.0,
        "qwen_yarn_rope_freq_scale": 0.5,
        "qwen_yarn_ext_factor_overridden": False,
        "qwen_yarn_rope_scaling_type_source": "numeric_fallback",
        "qwen_yarn_configuration_valid": True,
    }
    model_manager.last_compute_diagnostics = {
        "active_profile_id": "qwen3-8b-q4-k-m",
        "n_ctx": 65536,
        "native_context_tokens": 32768,
        "kv_cache_mode": {"type_k": 8, "type_v": 8, "flash_attn": True},
        "backend_used": "metal",
    }
    model_manager.get_llm_instance.return_value = runtime
    return model_manager


class _Qwen64kRuntime(_ReadyRuntime):
    def render_and_tokenize_chat(self, *_args, **_kwargs):
        return {"prompt_tokens": 42}


def _real_qwen_64k_model_manager(runtimes):
    from utils.llm.model_manager import ModelManager

    manager = object.__new__(ModelManager)
    manager.llm_lock = threading.RLock()
    manager.llm = runtimes[0]
    manager.use_mock_llm = False
    manager.model_path = "/tmp/Qwen3-8B-Q4_K_M.gguf"
    manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
        "profile_id": "qwen3-8b-q4-k-m",
        "chat_template_policy": "gguf-jinja",
        "rope_scaling_policy": {
            "type": "yarn",
            "required_for_tier": "64k-full",
            "factor": 2.0,
            "original_context_tokens": 32768,
        },
    }
    manager.context_tier = "64k-full"
    manager.context_window_tokens = 65536
    manager.api_model_id = "qwen3-8b-instruct"
    manager.last_yarn_rope_diagnostics = {
        "supported": True,
        "qwen_yarn_requested_context_tokens": 65536,
        "qwen_yarn_original_context_tokens": 32768,
    }
    manager.download_model_if_needed = MagicMock(return_value=True)
    manager.last_compute_diagnostics = {
        "active_profile_id": "qwen3-8b-q4-k-m",
        "qwen_64k_runtime_profile_id": "qwen64k_f16_fa_small_batch",
        "n_ctx": 65536,
        "native_context_tokens": 32768,
        "kv_cache_mode": {"type_k": 8, "type_v": 8, "flash_attn": True},
        "backend_used": "metal",
    }
    manager.worker_state = "ready"
    manager.last_worker_error_code = None
    manager.last_runtime_init_error = None
    manager.last_worker_restart_at_ms = None
    manager.last_plain_completion_eval_return_code = None
    manager.worker_restart_count = 0
    manager._llm_generation = 0
    manager._qwen_64k_profile_recovery_count = 0
    manager._qwen_64k_first_readiness_failure_category = None
    manager._qwen_64k_first_readiness_failure_diagnostics = {}
    manager._qwen_64k_profile_attempt_ids = ["qwen64k_f16_fa_small_batch"]
    manager._qwen_64k_selected_profile_index = 0
    manager._qwen_64k_selected_profile_id = "qwen64k_f16_fa_small_batch"
    manager._qwen_64k_runtime_profiles = [
        {"profile_id": "qwen64k_f16_fa_small_batch", "diagnostics": {"backend": "metal"}},
        {"profile_id": "qwen64k_kv_q8_fa_small_batch", "diagnostics": {"backend": "metal"}},
        {"profile_id": "qwen64k_kv_q4_fa_small_batch", "diagnostics": {"backend": "metal"}},
    ]
    close_calls = []
    manager._close_llm_proxy = MagicMock(side_effect=lambda runtime: close_calls.append(runtime))
    runtime_iter = iter(runtimes)

    def _get_llm_instance():
        try:
            replacement = next(runtime_iter)
        except StopIteration:
            return None
        manager._qwen_64k_selected_profile_id = manager._qwen_64k_runtime_profiles[
            manager._qwen_64k_selected_profile_index
        ]["profile_id"]
        manager.llm = replacement
        manager._qwen_64k_profile_attempt_ids.append(manager._qwen_64k_selected_profile_id)
        manager.last_compute_diagnostics["qwen_64k_runtime_profile_id"] = manager._qwen_64k_selected_profile_id
        return replacement

    manager.get_llm_instance = MagicMock(side_effect=_get_llm_instance)
    manager._test_close_calls = close_calls
    return manager


def test_qwen_64k_completion_smoke_worker_exception_gets_specific_safe_reason():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    class FailingRuntime(_Qwen64kRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "generation_exception_category": "metal_memory_allocation",
                    "exception_type": "RuntimeError",
                    "sanitized_error_summary": "RuntimeError:redacted",
                    "child_stderr_tail": "llama_context: kv cache allocation failed <redacted>",
                    "method": "create_chat_completion_from_rendered_prompt",
                    "reason": "SECRET prompt in allowed reason",
                    "stderr_tail": "redacted SECRET prompt in allowed stderr",
                },
            )

    model_manager = _qwen_64k_model_manager(FailingRuntime())
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_metal_memory_allocation"
    assert diagnostics["api_v1_readiness_completion_smoke_failure_reason"] == "runtime_completion_smoke_metal_memory_allocation"
    assert diagnostics["api_v1_readiness_completion_smoke_exception_category"] == "metal_memory_allocation"
    worker_diagnostics = diagnostics["api_v1_readiness_completion_smoke_worker_diagnostics"]
    assert worker_diagnostics["method"] == "create_chat_completion_from_rendered_prompt"
    assert worker_diagnostics["sanitized_error_summary"] == "RuntimeError:redacted"
    assert worker_diagnostics["child_stderr_tail"] == "llama_context: kv cache allocation failed <redacted>"
    assert "reason" not in worker_diagnostics
    assert "stderr_tail" not in worker_diagnostics
    assert "SECRET" not in json.dumps(diagnostics)


def test_qwen_64k_readiness_recovery_prefers_recoverable_backend_diagnostic():
    failed_runtime = _Qwen64kRuntime()
    recovered_runtime = _Qwen64kRuntime()
    model_manager = _real_qwen_64k_model_manager([failed_runtime, recovered_runtime])
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.side_effect = [
        {
            "api_v1_response": {
                "error": {
                    "code": "compute_node_inference_failed",
                    "internal_reason": "runtime_completion_smoke_metal_memory_allocation",
                    "exception_category": "runtime_metal_memory_allocation",
                    "worker_diagnostics": {
                        "generation_exception_category": "metal_memory_allocation",
                        "plain_completion_backend_failure_category": "metal_command_buffer_out_of_memory",
                        "plain_completion_eval_return_code": -3,
                        "exception_type": "RuntimeError",
                        "sanitized_error_summary": "RuntimeError:redacted",
                    },
                }
            }
        },
        {"api_v1_response": {"message": {"role": "assistant", "content": "ok"}}},
    ]
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager._test_close_calls == [failed_runtime]
    assert model_manager._qwen_64k_selected_profile_index == 1
    assert model_manager._qwen_64k_profile_recovery_count == 1
    assert model_manager.llm is recovered_runtime
    assert model_manager._qwen_64k_first_readiness_failure_category == "metal_command_buffer_out_of_memory"
    assert (
        model_manager._qwen_64k_first_readiness_failure_diagnostics["backend_failure_category"]
        == "metal_command_buffer_out_of_memory"
    )


@pytest.mark.parametrize(
    ("category", "decode_return_code", "internal_reason"),
    [
        ("decode_aborted", 2, "runtime_completion_smoke_decode_aborted"),
        ("backend_decode_failure", -4, "runtime_completion_smoke_backend_decode_failure"),
    ],
)
def test_qwen_64k_readiness_decode_failures_use_profile_recovery(
    category,
    decode_return_code,
    internal_reason,
):
    failed_runtime = _Qwen64kRuntime()
    model_manager = _real_qwen_64k_model_manager([failed_runtime])
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.return_value = {
        "api_v1_response": {
            "error": {
                "code": "compute_node_inference_failed",
                "internal_reason": internal_reason,
                "exception_category": f"runtime_{category}",
                "worker_diagnostics": {
                    "generation_exception_category": category,
                    "plain_completion_eval_return_code": decode_return_code,
                    "exception_type": "RuntimeError",
                    "sanitized_error_summary": "RuntimeError:redacted",
                },
            }
        }
    }
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager._test_close_calls == [failed_runtime]
    assert model_manager._qwen_64k_selected_profile_index == 1
    assert model_manager._qwen_64k_profile_recovery_count == 1
    assert model_manager.last_plain_completion_eval_return_code == decode_return_code
    assert model_manager._qwen_64k_first_readiness_failure_diagnostics["eval_return_code"] == decode_return_code
    relay_client._generate_api_v1_response_with_runtime_model.assert_called_once()


@pytest.mark.parametrize(
    ("message", "expected_category", "decode_return_code"),
    [
        ("llama_decode returned -3", "backend_graph_compute_failure", -3),
        ("llama_decode returned 2", "decode_aborted", 2),
        ("llama_decode returned -4", "backend_decode_failure", -4),
    ],
)
def test_qwen_64k_readiness_raw_decode_exception_uses_profile_recovery(
    message,
    expected_category,
    decode_return_code,
):
    failed_runtime = _Qwen64kRuntime()
    recovered_runtime = _Qwen64kRuntime()
    model_manager = _real_qwen_64k_model_manager([failed_runtime, recovered_runtime])
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.side_effect = [
        RuntimeError(message),
        {"api_v1_response": {"message": {"role": "assistant", "content": "ok"}}},
    ]
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager._test_close_calls == [failed_runtime]
    assert model_manager._qwen_64k_selected_profile_index == 1
    assert model_manager._qwen_64k_profile_recovery_count == 1
    assert model_manager.last_plain_completion_eval_return_code == decode_return_code
    assert model_manager.llm is recovered_runtime
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == expected_category
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_eval_return_code"] == decode_return_code
    assert relay_client._generate_api_v1_response_with_runtime_model.call_count == 2
    assert relay_client._api_v1_authoritative_context_admission.call_count == 2
    assert model_manager.download_model_if_needed.call_count == 1


def test_qwen_64k_readiness_decode_recovery_honors_cancellation():
    failed_runtime = _Qwen64kRuntime()
    q8_runtime = _Qwen64kRuntime()
    model_manager = _real_qwen_64k_model_manager([failed_runtime, q8_runtime])
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.return_value = {
        "api_v1_response": {
            "error": {
                "code": "compute_node_inference_failed",
                "internal_reason": "runtime_completion_smoke_decode_aborted",
                "worker_diagnostics": {
                    "generation_exception_category": "decode_aborted",
                    "plain_completion_eval_return_code": 2,
                },
            }
        }
    }
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
        cancellation_predicate=lambda: True,
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager._test_close_calls == [failed_runtime]
    assert model_manager._qwen_64k_selected_profile_index == 0
    assert model_manager._qwen_64k_profile_recovery_count == 0
    assert model_manager.last_plain_completion_eval_return_code == 2
    assert model_manager.get_llm_instance.call_count == 1
    assert model_manager.llm is None
    assert q8_runtime not in model_manager._test_close_calls


@pytest.mark.parametrize(
    ("budget_value", "expected_attempts"),
    [
        (None, 3),
        (0, 3),
        (False, 3),
        ("3", 3),
        (99, 3),
        (2, 2),
    ],
)
def test_qwen_64k_readiness_profile_budget_validation_is_bounded(budget_value, expected_attempts):
    runtimes = [_Qwen64kRuntime(), _Qwen64kRuntime(), _Qwen64kRuntime()]
    model_manager = _real_qwen_64k_model_manager(runtimes)
    if budget_value is None:
        model_manager.qwen_64k_readiness_profile_attempt_budget = None
    else:
        model_manager.qwen_64k_readiness_profile_attempt_budget = MagicMock(return_value=budget_value)
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.return_value = {
        "api_v1_response": {
            "error": {
                "code": "compute_node_inference_failed",
                "internal_reason": "runtime_completion_smoke_decode_aborted",
                "worker_diagnostics": {
                    "generation_exception_category": "decode_aborted",
                    "plain_completion_eval_return_code": 2,
                },
            }
        }
    }
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    expected_with_fake = min(expected_attempts, 3)
    assert relay_client._generate_api_v1_response_with_runtime_model.call_count == expected_with_fake
    assert len(model_manager._test_close_calls) == expected_with_fake
    assert relay_client._generate_api_v1_response_with_runtime_model.call_count <= 3


@pytest.mark.parametrize(
    ("category", "decode_return_code"),
    [("decode_aborted", 2), ("backend_decode_failure", -4)],
)
def test_qwen_64k_readiness_decode_recovery_uses_real_model_manager_lifecycle(
    category, decode_return_code
):
    failed_runtime = _Qwen64kRuntime()
    q8_runtime = _Qwen64kRuntime()
    model_manager = _real_qwen_64k_model_manager([failed_runtime, q8_runtime])
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.side_effect = [
        {
            "api_v1_response": {
                "error": {
                    "code": "compute_node_inference_failed",
                    "internal_reason": f"runtime_completion_smoke_{category}",
                    "worker_diagnostics": {
                        "generation_exception_category": category,
                        "plain_completion_eval_return_code": decode_return_code,
                    },
                }
            }
        },
        {"api_v1_response": {"message": {"role": "assistant", "content": "ok"}}},
    ]
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager._test_close_calls == [failed_runtime]
    assert model_manager._qwen_64k_selected_profile_index == 1
    assert model_manager._qwen_64k_profile_recovery_count == 1
    assert model_manager.last_plain_completion_eval_return_code == decode_return_code
    assert model_manager.llm is q8_runtime
    assert failed_runtime is not q8_runtime
    assert relay_client._api_v1_authoritative_context_admission.call_count == 2
    assert relay_client._generate_api_v1_response_with_runtime_model.call_count == 2
    assert model_manager.download_model_if_needed.call_count == 1


def test_qwen_64k_readiness_error_marks_profile_failed_and_redacted_summary():
    failed_runtime = _Qwen64kRuntime()
    model_manager = _qwen_64k_model_manager(failed_runtime)
    model_manager.last_compute_diagnostics = {
        **model_manager.last_compute_diagnostics,
        "qwen_64k_runtime_profile_id": "qwen64k_f16_fa_small_batch",
    }
    model_manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure.return_value = None
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.return_value = {
        "api_v1_response": {
            "error": {
                "code": "compute_node_inference_failed",
                "internal_reason": "runtime_completion_smoke_worker_exception",
                "worker_diagnostics": {
                    "generation_exception_category": "backend_graph_compute_failure",
                    "exception_type": "RuntimeError",
                },
            }
        }
    }
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_qwen_64k_runtime_profile_result"] == "failed"
    assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == "backend_graph_compute_failure"


def test_qwen_64k_completion_smoke_exception_adds_redacted_summary_without_worker_summary():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    failed_runtime = _Qwen64kRuntime()
    model_manager = _qwen_64k_model_manager(failed_runtime)
    model_manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure.return_value = None
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)

    def raise_without_summary(**_kwargs):
        raise LlamaCppInferenceRequestError(
            "llama_cpp request failed",
            diagnostics={
                "worker_diagnostics": {
                    "generation_exception_category": "backend_graph_compute_failure",
                    "exception_type": "RuntimeError",
                }
            },
        )

    relay_client._generate_api_v1_response_with_runtime_model.side_effect = raise_without_summary
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "RuntimeError"
    assert diagnostics["api_v1_readiness_completion_smoke_safe_summary"] == "LlamaCppInferenceRequestError:redacted"


def test_qwen_64k_completion_smoke_exception_promotes_safe_nested_worker_diagnostics():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    unsafe_fields = {
        "prompt": "SECRET_PROMPT",
        "rendered_prompt": "SECRET_RENDERED_PROMPT",
        "assistant_output": "SECRET_OUTPUT",
        "decrypted_payload": "SECRET_PAYLOAD",
        "ciphertext": "SECRET_CIPHERTEXT",
        "key": "SECRET_KEY",
        "tool_args": {"secret": True},
    }

    class FailingRuntime(_Qwen64kRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "outer wrapper with SECRET_PROMPT",
                diagnostics={
                    **unsafe_fields,
                    "method": "create_completion_keyword_prompt",
                    "attempted_generation_kwargs": "max_tokens,prompt",
                    "attempted_plain_completion_methods": "create_completion_keyword_prompt",
                    "generation_exception_category": "worker_timeout",
                    "exception_type": "TimeoutError",
                    "sanitized_error_summary": "TimeoutError:redacted",
                    "plain_completion_create_completion_callable": True,
                    "plain_completion_llama_call_callable": True,
                    "plain_completion_signature_inspectable": True,
                    "plain_completion_accepts_prompt_kwarg": True,
                    "plain_completion_accepts_max_tokens_kwarg": True,
                    "plain_completion_accepts_var_kwargs": False,
                    "qwen_api_v1_non_thinking_template_fallback": True,
                },
            )

    model_manager = _qwen_64k_model_manager(FailingRuntime())
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_method"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_generation_kwargs"] == "max_tokens,prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_plain_completion_methods"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == "worker_timeout"
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "TimeoutError"
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_create_completion_callable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_llama_call_callable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_signature_inspectable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs"] is False
    assert diagnostics["api_v1_readiness_completion_smoke_qwen_api_v1_non_thinking_template_fallback"] is True
    dumped = json.dumps(diagnostics)
    for unsafe_key in unsafe_fields:
        assert f'"{unsafe_key}"' not in dumped
    assert "SECRET_" not in dumped


def test_qwen_64k_completion_smoke_exception_falls_back_to_redacted_safe_summary():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    class FailingRuntime(_Qwen64kRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "method": "create_completion_keyword_prompt",
                    "generation_exception_category": "worker_exception",
                    "exception_type": "LlamaCppInferenceRequestError",
                    "prompt": "SECRET_PROMPT",
                },
            )

    model_manager = _qwen_64k_model_manager(FailingRuntime())
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "LlamaCppInferenceRequestError"
    assert diagnostics["api_v1_readiness_completion_smoke_safe_summary"] == "LlamaCppInferenceRequestError:redacted"
    assert "SECRET_" not in json.dumps(diagnostics)


def test_qwen_64k_deployed_plain_completion_worker_exception_populates_flat_safe_fields():
    from utils.llm.model_manager import LlamaCppInferenceRequestError

    class FailingRuntime(_Qwen64kRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "method": "create_completion_keyword_prompt",
                    "attempted_generation_kwargs": "max_tokens,prompt",
                    "attempted_plain_completion_methods": "create_completion_keyword_prompt",
                    "generation_exception_category": "worker_exception",
                    "exception_type": "LlamaCppInferenceRequestError",
                    "sanitized_error_summary": "LlamaCppInferenceRequestError:redacted",
                    "plain_completion_create_completion_callable": True,
                    "plain_completion_llama_call_callable": True,
                    "plain_completion_signature_inspectable": True,
                    "plain_completion_accepts_prompt_kwarg": True,
                    "plain_completion_accepts_max_tokens_kwarg": True,
                    "plain_completion_accepts_var_kwargs": False,
                    "prompt": "SECRET_PROMPT",
                    "rendered_prompt": "SECRET_RENDERED_PROMPT",
                },
            )

    model_manager = _qwen_64k_model_manager(FailingRuntime())
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_method"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_generation_kwargs"] == "max_tokens,prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_attempted_plain_completion_methods"] == "create_completion_keyword_prompt"
    assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == "worker_exception"
    assert diagnostics["api_v1_readiness_completion_smoke_exception_type"] == "LlamaCppInferenceRequestError"
    assert diagnostics["api_v1_readiness_completion_smoke_safe_summary"] == "LlamaCppInferenceRequestError:redacted"
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_create_completion_callable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_llama_call_callable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_signature_inspectable"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg"] is True
    assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs"] is False
    dumped = json.dumps(diagnostics)
    assert "SECRET_" not in dumped


def test_qwen_64k_yarn_eval_exception_fails_closed_before_registration():
    class FailingRuntime(_Qwen64kRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise RuntimeError("RoPE YaRN eval failure at n_ctx=65536")

    model_manager = _qwen_64k_model_manager(FailingRuntime())
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager.last_compute_diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_rope_yarn_eval_failure"
    assert model_manager.last_runtime_init_error.endswith("reason=runtime_completion_smoke_rope_yarn_eval_failure")


def test_qwen_64k_completion_smoke_passes_with_yarn_and_kv_diagnostics():
    model_manager = _qwen_64k_model_manager(_Qwen64kRuntime())
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = model_manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert diagnostics["api_v1_readiness_yarn_rope_enabled"] is True
    assert diagnostics["api_v1_readiness_yarn_rope_factor"] == 2.0
    assert diagnostics["api_v1_readiness_yarn_original_context_tokens"] == 32768
    assert diagnostics["api_v1_readiness_yarn_requested_context_tokens"] == 65536
    assert diagnostics["api_v1_readiness_yarn_context_multiplier"] == 2.0
    assert diagnostics["api_v1_readiness_yarn_rope_freq_scale"] == 0.5
    assert diagnostics["api_v1_readiness_yarn_ext_factor_overridden"] is False
    assert diagnostics["api_v1_readiness_yarn_rope_scaling_type_source"] == "numeric_fallback"
    assert diagnostics["api_v1_readiness_yarn_configuration_valid"] is True
    assert diagnostics["api_v1_readiness_kv_cache_mode"] == {"type_k": 8, "type_v": 8, "flash_attn": True}


def test_qwen_8k_readiness_still_passes_without_yarn():
    model_manager = MagicMock()
    model_manager.use_mock_llm = True
    model_manager.model_profile = {"provider": "qwen", "thinking_mode": "disabled", "chat_template_policy": "gguf-jinja"}
    model_manager.context_tier = "8k-fast"
    model_manager.context_window_tokens = 8192
    model_manager.api_model_id = "qwen3-8b-instruct"
    model_manager.last_compute_diagnostics = {"n_ctx": 8192}
    model_manager.last_yarn_rope_diagnostics = {"supported": False, "missing_reason": "not_required_for_active_profile_or_tier"}
    model_manager.get_llm_instance.return_value = _Qwen64kRuntime()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=_ready_relay_client(),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager.last_compute_diagnostics["api_v1_readiness_yarn_rope_enabled"] is False


def test_completion_smoke_diagnostic_sanitizer_allows_qwen_plain_completion_variant_fields():
    from utils import compute_node_runtime

    safe = compute_node_runtime._safe_completion_smoke_worker_diagnostics({
        "generation_exception_category": "prompt_eval_decode_failure",
        "plain_completion_prompt_tokenization_variant_count": 2,
        "plain_completion_prompt_tokenization_variant_ids": "tokenize_add_bos_false_special_false,tokenize_add_bos_false_no_special",
        "plain_completion_prompt_tokenization_selected_variant": "tokenize_add_bos_false_special_false",
        "plain_completion_prompt_tokenization_selected_token_count": 42,
        "plain_completion_prompt_tokenization_selected_special": False,
        "plain_completion_attempt_methods": "create_completion_keyword_prompt,create_completion_keyword_token_ids",
        "plain_completion_attempt_categories": "prompt_eval_failure,prompt_eval_decode_failure",
        "plain_completion_attempt_safe_summaries": "RuntimeError:prompt_eval_failure,RuntimeError:prompt_eval_decode_failure",
        "plain_completion_attempt_count": 3,
        "qwen_high_level_chat_fallback_attempted": True,
        "qwen_high_level_chat_fallback_category": "unsupported_generation_kwarg",
        "prompt": "SECRET_PROMPT",
    })

    assert safe["generation_exception_category"] == "prompt_eval_decode_failure"
    assert safe["plain_completion_prompt_tokenization_variant_count"] == 2
    assert safe["plain_completion_prompt_tokenization_variant_ids"] == (
        "tokenize_add_bos_false_special_false,tokenize_add_bos_false_no_special"
    )
    assert safe["plain_completion_prompt_tokenization_selected_variant"] == (
        "tokenize_add_bos_false_special_false"
    )
    assert safe["plain_completion_prompt_tokenization_selected_token_count"] == 42
    assert safe["plain_completion_prompt_tokenization_selected_special"] is False
    assert safe["plain_completion_attempt_count"] == 3
    assert "prompt" not in safe
    assert "SECRET_PROMPT" not in json.dumps(safe)
    assert compute_node_runtime._COMPLETION_SMOKE_REASON_BY_CATEGORY["prompt_eval_decode_failure"] == "runtime_completion_smoke_plain_completion_decode_failure"
    assert compute_node_runtime._COMPLETION_SMOKE_REASON_BY_CATEGORY["prompt_eval_backend_failure"] == "runtime_completion_smoke_plain_completion_backend_failure"


def test_compute_node_runtime_has_single_authoritative_yarn_original_context_assignment():
    import ast
    from pathlib import Path

    source = Path("utils/compute_node_runtime.py").read_text()
    tree = ast.parse(source)
    matching_dicts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
        count = keys.count("api_v1_readiness_yarn_original_context_tokens")
        if count:
            matching_dicts.append(count)
    assert matching_dicts
    assert all(count == 1 for count in matching_dicts)


def test_qwen_64k_profile_recovery_f16_fail_then_q8_success():
    """F16 smoke raises backend_graph_compute_failure; Q8 runtime passes; recovery count is 1."""
    f16_runtime = _Qwen64kRuntime()
    q8_runtime = _Qwen64kRuntime()
    model_manager = _real_qwen_64k_model_manager([f16_runtime, q8_runtime])

    # First generate_api_v1 call fails; second (Q8) passes
    model_manager._relay_client = MagicMock()
    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.side_effect = [
        {
            "api_v1_response": {
                "error": {
                    "code": "compute_node_inference_failed",
                    "internal_reason": "runtime_completion_smoke_backend_decode_failure",
                    "exception_category": "runtime_backend_graph_compute_failure",
                    "worker_diagnostics": {
                        "generation_exception_category": "backend_graph_compute_failure",
                        "plain_completion_backend_failure_category": "backend_graph_compute_failure",
                        "plain_completion_eval_return_code": -3,
                        "exception_type": "RuntimeError",
                        "sanitized_error_summary": "RuntimeError:redacted",
                    },
                }
            }
        },
        {"api_v1_response": {"message": {"role": "assistant", "content": "ok"}}},
    ]

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert model_manager._test_close_calls == [f16_runtime]
    assert model_manager.llm is q8_runtime
    assert f16_runtime is not q8_runtime
    assert relay_client._api_v1_authoritative_context_admission.call_count == 2
    assert relay_client._generate_api_v1_response_with_runtime_model.call_count == 2
    assert model_manager.download_model_if_needed.call_count == 1
    assert model_manager._qwen_64k_profile_recovery_count == 1
    assert model_manager._qwen_64k_first_readiness_failure_category == "backend_graph_compute_failure"


def test_qwen_64k_profile_recovery_three_profile_exhaustion_fails_closed():
    """All three Metal profiles fail the smoke; ensure_api_v1_runtime_ready returns False."""
    f16_runtime = _Qwen64kRuntime()
    q8_runtime = _Qwen64kRuntime()
    q4_runtime = _Qwen64kRuntime()

    model_manager = _real_qwen_64k_model_manager([f16_runtime, q8_runtime, q4_runtime])

    relay_client = MagicMock()
    relay_client._api_v1_authoritative_context_admission.return_value = (True, None, 42)
    relay_client._generate_api_v1_response_with_runtime_model.return_value = {
        "api_v1_response": {
            "error": {
                "code": "compute_node_inference_failed",
                "internal_reason": "runtime_completion_smoke_backend_graph_compute_failure",
                "exception_category": "runtime_backend_graph_compute_failure",
                "worker_diagnostics": {
                    "generation_exception_category": "backend_graph_compute_failure",
                    "plain_completion_backend_failure_category": "backend_graph_compute_failure",
                    "plain_completion_eval_return_code": -3,
                    "exception_type": "RuntimeError",
                    "sanitized_error_summary": "RuntimeError:redacted",
                },
            }
        }
    }

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=model_manager,
        relay_client=relay_client,
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert model_manager._test_close_calls == [f16_runtime, q8_runtime, q4_runtime]
    assert relay_client._api_v1_authoritative_context_admission.call_count == 3
    assert relay_client._generate_api_v1_response_with_runtime_model.call_count == 3
    assert model_manager.download_model_if_needed.call_count == 1
    assert model_manager.get_llm_instance.call_count == 3
    assert model_manager._qwen_64k_first_readiness_failure_category == "backend_graph_compute_failure"
    assert model_manager._qwen_64k_profile_recovery_count == 3


def test_completion_smoke_cuda_oom_classification_is_qwen64k_recoverable():
    category, reason, diagnostics = _classify_completion_smoke_exception(
        RuntimeError('CUDA out of memory in cudaMalloc during completion smoke')
    )

    assert category == 'runtime_context_create_cuda_memory'
    assert reason == 'runtime_completion_smoke_cuda_memory_allocation'
    assert _qwen_64k_readiness_profile_recoverable(category) is True
    assert diagnostics['exception_type'] == 'RuntimeError'
