from types import SimpleNamespace
from unittest.mock import MagicMock

from utils.compute_node_runtime import ComputeNodeRuntime, ComputeNodeRuntimeConfig
from utils.llm.model_manager import LlamaCppInferenceRequestError


class _Qwen64kFakeRuntime:
    def render_and_tokenize_chat(self, *_args, **_kwargs):
        return {"prompt_tokens": 42}

    def tokenize(self, *_args, **_kwargs):
        return [1] * 42

    def apply_chat_template(self, *_args, **_kwargs):
        return "<redacted-test-template>"

    def create_chat_completion(self, **_kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def _model_manager(runtime):
    manager = MagicMock()
    manager.use_mock_llm = True
    manager.model_path = "/tmp/Qwen3-8B-Q4_K_M.gguf"
    manager.model_profile = {
        "provider": "qwen",
        "thinking_mode": "disabled",
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
    manager.last_yarn_rope_diagnostics = {"supported": True, "yarn_resolver_source": "test"}
    manager.last_compute_diagnostics = {"n_ctx": 65536, "kv_cache_mode": {"type_k": 8, "type_v": 8}}
    manager.get_llm_instance.return_value = runtime
    return manager


def _runtime_for(fake_runtime):
    manager = _model_manager(fake_runtime)
    return ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 42)
        ),
        crypto_manager=MagicMock(),
    ), manager


def test_qwen64k_packaged_fake_runtime_generation_exception_has_specific_safe_reason():
    class FailingRuntime(_Qwen64kFakeRuntime):
        def create_chat_completion(self, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "generation_exception_category": "kv_cache_allocation",
                    "exception_type": "RuntimeError",
                    "method": "create_chat_completion",
                },
            )

    runtime, manager = _runtime_for(FailingRuntime())

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_kv_cache_allocation"
    assert diagnostics["api_v1_readiness_error_reason"] != "runtime_completion_smoke_exception"
    assert diagnostics["api_v1_readiness_completion_smoke_path"] == "shared_api_v1_generation"


def test_qwen64k_packaged_fake_runtime_valid_generation_passes_readiness():
    runtime, manager = _runtime_for(_Qwen64kFakeRuntime())

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_result"] == "passed"
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert diagnostics["api_v1_readiness_completion_smoke_path"] == "shared_api_v1_generation"
