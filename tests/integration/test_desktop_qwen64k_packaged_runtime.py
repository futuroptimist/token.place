import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from utils.compute_node_runtime import ComputeNodeRuntime, ComputeNodeRuntimeConfig
from utils.context_profiles import apply_context_profile
from utils.llm import model_manager as model_manager_module
from utils.llm.model_manager import LlamaCppInferenceRequestError, ModelManager


def _config(tmp_path):
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path / 'models'),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    return config


def _write_fake_llama_cpp_runtime(root: Path, *, completion_error: str | None = None) -> None:
    package = root / 'llama_cpp'
    package.mkdir(parents=True)
    completion_branch = (
        "        raise RuntimeError(os.environ.get('TOKEN_PLACE_COMPLETION_ERROR'))\n"
        if completion_error is not None
        else "        return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}\n"
    )
    (package / '__init__.py').write_text(
        """
import json, os, sys
__version__ = '0.3.32-test'
GGML_USE_METAL = True
GGML_TYPE_Q8_0 = 8
GGML_TYPE_Q4_0 = 2
LLAMA_ROPE_SCALING_TYPE_YARN = 2

def llama_supports_gpu_offload():
    return True

class Llama:
    def __init__(self, **kwargs):
        path = os.environ.get('TOKEN_PLACE_ATTEMPTS_JSONL')
        if path:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(kwargs, sort_keys=True) + '\\n')
        if os.environ.get('TOKEN_PLACE_FAIL_ALL_PROFILES') == '1' or 'type_k' not in kwargs:
            print('ggml_metal: KV cache allocation failed while creating llama_context', file=sys.stderr, flush=True)
            raise ValueError('Failed to create llama_context')
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
        return '<qwen>'
    def tokenize(self, payload, add_bos=False):
        return [1] * 42
    def create_completion(self, prompt, **kwargs):
"""
        + completion_branch,
        encoding='utf-8',
    )


def test_packaged_subprocess_qwen64k_retries_after_context_create_failure(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    attempts_file = tmp_path / 'attempts.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_ATTEMPTS_JSONL', str(attempts_file))

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manager.model_path).write_text('fake')
    facade = model_manager_module._SubprocessLlamaCppModule(
        str(runtime_root / 'llama_cpp' / '__init__.py'),
        desktop_runtime_probe={
            'backend': 'metal',
            'gpu_offload_supported': True,
            'constructor_kwarg_support': {
                'rope_scaling_type': True, 'yarn_ext_factor': True, 'yarn_orig_ctx': True,
                'type_k': True, 'type_v': True, 'flash_attn': True, 'offload_kqv': True,
                'n_batch': True, 'n_ubatch': True,
            },
            'yarn_enum_value': 2,
            'qwen_64k_yarn_support': 'supported',
            'q8_kv_cache_type_value': 8,
            'q4_kv_cache_type_value': 2,
            'llama_cpp_python_version': '0.3.32-test',
        },
    )

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    attempts = [json.loads(line) for line in attempts_file.read_text().splitlines()]
    assert len(attempts) == 2
    assert 'type_k' not in attempts[0]
    assert attempts[1]['type_k'] == 8
    assert manager.last_compute_diagnostics['qwen_64k_memory_profile']['profile_id'] == 'qwen64k_kv_q8'


def test_packaged_subprocess_qwen64k_profile_exhaustion_fails_closed(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    attempts_file = tmp_path / 'attempts.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_ATTEMPTS_JSONL', str(attempts_file))
    monkeypatch.setenv('TOKEN_PLACE_FAIL_ALL_PROFILES', '1')

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manager.model_path).write_text('fake')
    facade = model_manager_module._SubprocessLlamaCppModule(
        str(runtime_root / 'llama_cpp' / '__init__.py'),
        desktop_runtime_probe={
            'backend': 'metal', 'gpu_offload_supported': True,
            'constructor_kwarg_support': {
                'rope_scaling_type': True, 'yarn_ext_factor': True, 'yarn_orig_ctx': True,
                'type_k': True, 'type_v': True, 'flash_attn': True, 'offload_kqv': True,
                'n_batch': True, 'n_ubatch': True,
            },
            'yarn_enum_value': 2, 'qwen_64k_yarn_support': 'supported',
            'q8_kv_cache_type_value': 8, 'q4_kv_cache_type_value': 2,
        },
    )

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert manager.llm is None
    assert 'profile exhaustion before registration' in manager.last_runtime_init_error
    assert 'runtime_context_create' in manager.last_runtime_init_error


class _Qwen64kFakeRuntime:
    def render_and_tokenize_chat(self, *_args, **_kwargs):
        return {"prompt_tokens": 42}

    def tokenize(self, *_args, **_kwargs):
        return [1] * 42

    def apply_chat_template(self, *_args, **_kwargs):
        return "<redacted-test-template>"

    def create_chat_completion(self, **_kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def _model_manager(runtime):
    manager = MagicMock()
    manager.use_mock_llm = True
    manager.config = None
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
    manager.create_chat_completion_with_recovery = None
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


@pytest.mark.parametrize(
    ("category", "reason"),
    [
        ("kv_cache_allocation", "runtime_completion_smoke_kv_cache_allocation"),
        ("unsupported_generation_kwarg", "runtime_completion_smoke_unsupported_generation_kwarg"),
        ("rope_yarn_eval_failure", "runtime_completion_smoke_rope_yarn_eval_failure"),
        ("worker_timeout", "runtime_completion_smoke_worker_timeout"),
        ("worker_dead", "runtime_completion_smoke_worker_dead"),
    ],
)
def test_qwen64k_packaged_fake_runtime_generation_exception_has_specific_safe_reason(category, reason):
    class FailingRuntime(_Qwen64kFakeRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "generation_exception_category": category,
                    "exception_type": "RuntimeError",
                    "method": "create_chat_completion_from_rendered_prompt",
                },
            )

    runtime, manager = _runtime_for(FailingRuntime())

    assert runtime.ensure_api_v1_runtime_ready() is False
    diagnostics = manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_error_reason"] == reason
    assert diagnostics["api_v1_readiness_error_reason"] != "runtime_completion_smoke_exception"
    assert diagnostics["api_v1_readiness_completion_smoke_path"] == "shared_api_v1_generation"


def test_qwen64k_packaged_subprocess_generation_error_preserves_safe_diagnostics(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root, completion_error='kv')
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_ERROR', 'KV cache allocation failed for SECRET_PROMPT')

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        assert proxy.render_and_tokenize_chat([{'role': 'user', 'content': 'safe'}]) == {'prompt_tokens': 42}
        with pytest.raises(LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt([{'role': 'user', 'content': 'SECRET_PROMPT'}], stream=False)
        assert exc_info.value.diagnostics['method'] == 'create_chat_completion_from_rendered_prompt'
        assert exc_info.value.diagnostics['generation_exception_category'] == 'kv_cache_allocation'
        assert 'SECRET_PROMPT' not in str(exc_info.value.diagnostics)

        runtime, manager = _runtime_for(proxy)
        assert runtime.ensure_api_v1_runtime_ready() is False
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics['api_v1_readiness_error_reason'] == 'runtime_completion_smoke_kv_cache_allocation'
        assert diagnostics['api_v1_readiness_error_reason'] != 'runtime_completion_smoke_exception'
        assert 'SECRET_PROMPT' not in str(diagnostics)
    finally:
        proxy.close()


def test_qwen64k_packaged_fake_runtime_valid_generation_passes_readiness():
    runtime, manager = _runtime_for(_Qwen64kFakeRuntime())

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_result"] == "passed"
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert diagnostics["api_v1_readiness_completion_smoke_path"] == "shared_api_v1_generation"


def test_qwen64k_packaged_fake_runtime_filters_unsupported_internal_top_k_and_registers():
    class TopKRejectingRuntime(_Qwen64kFakeRuntime):
        def __init__(self):
            self.calls = []
            self.rejected = False

        def create_chat_completion_from_rendered_prompt(self, messages, **kwargs):
            self.calls.append(dict(kwargs))
            if "top_k" in kwargs and not self.rejected:
                self.rejected = True
                raise TypeError("got an unexpected keyword argument 'top_k'")
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    fake = TopKRejectingRuntime()
    runtime, manager = _runtime_for(fake)
    manager.model_profile["generation_defaults"] = {"top_k": 20}

    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = manager.last_compute_diagnostics
    assert diagnostics["api_v1_readiness_result"] == "passed"
    assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"
    assert fake.calls[0]["top_k"] == 20
    assert "top_k" not in fake.calls[1]
    assert "top_k" in diagnostics["api_v1_generation_kwargs_filtered"]
