import json
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from utils.compute_node_runtime import ComputeNodeRuntime, ComputeNodeRuntimeConfig
from utils.context_profiles import apply_context_profile
from utils.llm import model_manager as model_manager_module
from utils.llm.model_manager import LlamaCppInferenceRequestError, ModelManager

BRIDGE_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'src-tauri'
    / 'python'
    / 'compute_node_bridge.py'
)
BRIDGE_SPEC = importlib.util.spec_from_file_location(
    'desktop_compute_node_bridge_packaged_integration',
    BRIDGE_MODULE_PATH,
)
compute_node_bridge = importlib.util.module_from_spec(BRIDGE_SPEC)
assert BRIDGE_SPEC and BRIDGE_SPEC.loader
BRIDGE_SPEC.loader.exec_module(compute_node_bridge)

UNSAFE_READINESS_SENTINELS = (
    'SECRET_PROMPT',
    'SECRET_RENDERED_PROMPT',
    'SECRET_ASSISTANT_OUTPUT',
    'SECRET_DECRYPTED_PAYLOAD',
    'SECRET_KEY',
    'SECRET_TOOL_ARGS',
    'SECRET_CIPHERTEXT_INTERNALS',
)


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
        path = os.environ.get('TOKEN_PLACE_RENDER_ATTEMPTS_JSONL')
        if path:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({
                    'messages': messages,
                    'tokenize': tokenize,
                    'add_generation_prompt': add_generation_prompt,
                    'enable_thinking': enable_thinking,
                }, sort_keys=True) + '\\n')
        return '<qwen>'
    def tokenize(self, payload, add_bos=False, special=None):
        if special is True:
            return [3] * 42
        if special is False:
            return [1] * 42
        return [2] * 42
    def create_completion(self, prompt, **kwargs):
        path = os.environ.get('TOKEN_PLACE_COMPLETION_KWARGS_JSONL')
        if path:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'prompt': prompt, **kwargs}, sort_keys=True) + '\\n')
        if isinstance(prompt, str) and os.environ.get('TOKEN_PLACE_STRING_COMPLETION_THINK'):
            return {'choices': [{'text': os.environ.get('TOKEN_PLACE_STRING_COMPLETION_THINK')}]}
        if isinstance(prompt, list) and os.environ.get('TOKEN_PLACE_TOKEN_COMPLETION_THINK'):
            return {'choices': [{'text': os.environ.get('TOKEN_PLACE_TOKEN_COMPLETION_THINK')}]}
        if isinstance(prompt, str) and os.environ.get('TOKEN_PLACE_STRING_COMPLETION_ERROR'):
            raise RuntimeError(os.environ.get('TOKEN_PLACE_STRING_COMPLETION_ERROR'))
        if isinstance(prompt, list) and os.environ.get('TOKEN_PLACE_TOKEN_COMPLETION_ERROR_SPECIAL_TRUE') and prompt == [3] * 42:
            raise RuntimeError(os.environ.get('TOKEN_PLACE_TOKEN_COMPLETION_ERROR_SPECIAL_TRUE'))
        if isinstance(prompt, list) and os.environ.get('TOKEN_PLACE_TOKEN_COMPLETION_ERROR'):
            raise RuntimeError(os.environ.get('TOKEN_PLACE_TOKEN_COMPLETION_ERROR'))
"""
        + completion_branch
        + """
    def create_chat_completion(self, *, messages, max_tokens, chat_template_kwargs):
        _ = chat_template_kwargs
        path = os.environ.get('TOKEN_PLACE_CHAT_COMPLETION_KWARGS_JSONL') or os.environ.get('TOKEN_PLACE_COMPLETION_KWARGS_JSONL')
        if path:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'messages': messages, 'max_tokens': max_tokens, 'chat_template_kwargs': chat_template_kwargs}, sort_keys=True) + '\\n')
        if os.environ.get('TOKEN_PLACE_CHAT_COMPLETION_ERROR'):
            raise RuntimeError(os.environ.get('TOKEN_PLACE_CHAT_COMPLETION_ERROR'))
        if os.environ.get('TOKEN_PLACE_CHAT_COMPLETION_OK') == '1':
            return {'choices': [{'message': {'role': 'assistant', 'content': 'chat ok'}}]}
        raise RuntimeError('failed to eval prompt')
""",
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
                'rope_scaling_type': True, 'rope_freq_scale': True, 'yarn_ext_factor': True, 'yarn_orig_ctx': True,
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
    assert manager.last_compute_diagnostics['qwen_64k_memory_profile']['profile_id'] == 'qwen64k_kv_q8_fa_small_batch'


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
                'rope_scaling_type': True, 'rope_freq_scale': True, 'yarn_ext_factor': True, 'yarn_orig_ctx': True,
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
    manager.last_yarn_rope_diagnostics = {
        "supported": True,
        "yarn_resolver_source": "test",
        "qwen_yarn_requested_context_tokens": 65536,
        "qwen_yarn_original_context_tokens": 32768,
        "qwen_yarn_context_multiplier": 2.0,
        "qwen_yarn_rope_freq_scale": 0.5,
        "qwen_yarn_ext_factor_overridden": False,
        "qwen_yarn_rope_scaling_type_source": "enum",
        "qwen_yarn_configuration_valid": True,
    }
    manager.last_compute_diagnostics = {
        "n_ctx": 65536,
        "kv_cache_mode": {"type_k": 8, "type_v": 8},
        "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant": "tokenize_add_bos_false_special_false",
        "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": 50,
        "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special": False,
    }
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
        ("unsupported_generation_kwarg", "runtime_completion_smoke_plain_completion_unexpected_kwarg"),
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
    render_attempts_file = tmp_path / 'render_attempts.jsonl'
    completion_kwargs_file = tmp_path / 'completion_kwargs.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv(
        'TOKEN_PLACE_COMPLETION_ERROR',
        'KV cache allocation failed for '
        'SECRET_PROMPT SECRET_RENDERED_PROMPT SECRET_ASSISTANT_OUTPUT '
        'SECRET_DECRYPTED_PAYLOAD SECRET_KEY SECRET_TOOL_ARGS SECRET_CIPHERTEXT_INTERNALS',
    )
    monkeypatch.setenv('TOKEN_PLACE_RENDER_ATTEMPTS_JSONL', str(render_attempts_file))
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_KWARGS_JSONL', str(completion_kwargs_file))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        literal_no_think_messages = [{'role': 'user', 'content': 'ordinary /no_think text'}]
        assert proxy.render_and_tokenize_chat(literal_no_think_messages) == {'prompt_tokens': 42}
        with pytest.raises(LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt([{'role': 'user', 'content': 'SECRET_PROMPT'}], stream=False)
        assert exc_info.value.diagnostics['method'] == 'create_completion_keyword_prompt'
        assert exc_info.value.diagnostics['generation_exception_category'] == 'kv_cache_allocation'
        assert exc_info.value.diagnostics['attempted_plain_completion_methods'] == 'create_completion_keyword_prompt'
        assert exc_info.value.diagnostics['exception_type'] == 'RuntimeError'
        assert exc_info.value.diagnostics['sanitized_error_summary'] == 'RuntimeError:kv_cache_allocation'
        assert exc_info.value.diagnostics['plain_completion_create_completion_callable'] is True
        assert exc_info.value.diagnostics['plain_completion_llama_call_callable'] is False
        assert exc_info.value.diagnostics['plain_completion_accepts_max_tokens_kwarg'] is True
        assert exc_info.value.diagnostics['plain_completion_accepts_var_kwargs'] is True
        assert all(sentinel not in str(exc_info.value.diagnostics) for sentinel in UNSAFE_READINESS_SENTINELS)

        runtime, manager = _runtime_for(proxy)
        assert runtime.ensure_api_v1_runtime_ready() is False
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics['api_v1_readiness_error_reason'] == 'runtime_completion_smoke_kv_cache_allocation'
        assert diagnostics['api_v1_readiness_error_reason'] != 'runtime_completion_smoke_exception'
        assert diagnostics['api_v1_readiness_completion_smoke_method'] == 'create_completion_keyword_prompt'
        assert diagnostics['api_v1_readiness_completion_smoke_attempted_generation_kwargs'] == 'max_tokens,prompt'
        assert diagnostics['api_v1_readiness_completion_smoke_attempted_plain_completion_methods'] == 'create_completion_keyword_prompt'
        assert diagnostics['api_v1_readiness_completion_smoke_generation_exception_category'] == 'kv_cache_allocation'
        assert diagnostics['api_v1_readiness_completion_smoke_exception_type'] == 'RuntimeError'
        assert diagnostics['api_v1_readiness_completion_smoke_safe_summary'] == 'RuntimeError:kv_cache_allocation'
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_create_completion_callable'] is True
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_llama_call_callable'] is False
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_signature_inspectable'] is True
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg'] is True
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg'] is True
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs'] is True
        dumped = json.dumps(diagnostics)
        assert all(sentinel not in dumped for sentinel in UNSAFE_READINESS_SENTINELS)
        for unsafe in ('rendered_prompt', 'assistant_output', 'decrypted_payload', 'key', 'tool_args', 'ciphertext'):
            assert f'"{unsafe}"' not in dumped

        safe_bridge_diagnostics = compute_node_bridge._safe_readiness_diagnostics(manager)
        for expected_key in (
            'api_v1_readiness_completion_smoke_method',
            'api_v1_readiness_completion_smoke_attempted_generation_kwargs',
            'api_v1_readiness_completion_smoke_attempted_plain_completion_methods',
            'api_v1_readiness_completion_smoke_generation_exception_category',
            'api_v1_readiness_completion_smoke_exception_type',
            'api_v1_readiness_completion_smoke_safe_summary',
            'api_v1_readiness_completion_smoke_plain_completion_create_completion_callable',
            'api_v1_readiness_completion_smoke_plain_completion_llama_call_callable',
            'api_v1_readiness_completion_smoke_plain_completion_signature_inspectable',
            'api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg',
            'api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg',
            'api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs',
        ):
            assert expected_key in safe_bridge_diagnostics
        safe_bridge_dump = json.dumps(safe_bridge_diagnostics, sort_keys=True)
        assert all(sentinel not in safe_bridge_dump for sentinel in UNSAFE_READINESS_SENTINELS)

        rendered_status = io.StringIO()
        with patch.object(compute_node_bridge.sys, 'stdout', rendered_status):
            compute_node_bridge.emit({'event': 'status', **safe_bridge_diagnostics})
        status_line = rendered_status.getvalue()
        assert 'api_v1_readiness_completion_smoke_method' in status_line
        assert 'api_v1_readiness_completion_smoke_safe_summary' in status_line
        assert all(sentinel not in status_line for sentinel in UNSAFE_READINESS_SENTINELS)

        rendered_stderr = []
        with patch.object(compute_node_bridge, 'print', lambda payload, file=None: rendered_stderr.append(payload)):
            compute_node_bridge._emit_safe_readiness_diagnostics_stderr(manager)
        assert rendered_stderr
        stderr_line = rendered_stderr[-1]
        assert 'desktop.compute_node_bridge.api_v1_readiness.safe_diagnostics' in stderr_line
        assert 'api_v1_readiness_completion_smoke_safe_summary=RuntimeError:kv_cache_allocation' in stderr_line
        assert all(sentinel not in stderr_line for sentinel in UNSAFE_READINESS_SENTINELS)

        render_attempts = [json.loads(line) for line in render_attempts_file.read_text().splitlines()]
        assert render_attempts[0]['messages'] == literal_no_think_messages
        assert render_attempts[0]['enable_thinking'] is False
        assert all('/no_think' not in json.dumps(attempt) for attempt in render_attempts[1:])
        assert all(attempt['enable_thinking'] is False for attempt in render_attempts)
        completion_attempts = [
            json.loads(line) for line in completion_kwargs_file.read_text().splitlines()
        ]
        assert completion_attempts
        assert all(attempt.get('max_tokens') == 64 for attempt in completion_attempts)
    finally:
        proxy.close()


def test_qwen64k_packaged_subprocess_token_id_fallback_passes_readiness_and_registers(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    completion_kwargs_file = tmp_path / 'completion_kwargs.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_STRING_COMPLETION_ERROR', 'failed to tokenize prompt')
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_KWARGS_JSONL', str(completion_kwargs_file))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        runtime, manager = _runtime_for(proxy)

        assert runtime.ensure_api_v1_runtime_ready() is True
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics["api_v1_runtime_ready"] is True
        assert diagnostics["api_v1_readiness_completion_smoke_result"] == "passed"

        completion_attempts = [
            json.loads(line) for line in completion_kwargs_file.read_text().splitlines()
        ]
        assert any(isinstance(attempt["prompt"], str) for attempt in completion_attempts)
        assert any(attempt["prompt"] == [1] * 42 for attempt in completion_attempts)
        assert all(attempt.get("max_tokens") == 64 for attempt in completion_attempts)

        assert manager.last_runtime_init_error is None
    finally:
        proxy.close()


def test_qwen64k_packaged_subprocess_token_id_fallback_failure_reports_safe_category(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    completion_kwargs_file = tmp_path / 'completion_kwargs.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv(
        'TOKEN_PLACE_STRING_COMPLETION_ERROR',
        'failed to tokenize prompt SECRET_PROMPT SECRET_RENDERED_PROMPT SECRET_ASSISTANT_OUTPUT',
    )
    monkeypatch.setenv(
        'TOKEN_PLACE_TOKEN_COMPLETION_ERROR',
        'failed to eval prompt SECRET_PROMPT SECRET_KEY SECRET_TOOL_ARGS SECRET_CIPHERTEXT_INTERNALS',
    )
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_KWARGS_JSONL', str(completion_kwargs_file))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        runtime, manager = _runtime_for(proxy)

        assert runtime.ensure_api_v1_runtime_ready() is False
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_plain_completion_eval_failure"
        assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == "prompt_eval_failure"
        assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted"] is True
        assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_token_count"] == 42
        assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method"] == "llama.tokenize"
        assert diagnostics["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special"] is False
        assert diagnostics.get("api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category") in (None, "")
        dumped = json.dumps(diagnostics)
        assert all(sentinel not in dumped for sentinel in UNSAFE_READINESS_SENTINELS)
        assert '"token_ids"' not in dumped

        completion_attempts = [
            json.loads(line) for line in completion_kwargs_file.read_text().splitlines()
        ]
        assert any(isinstance(attempt["prompt"], str) for attempt in completion_attempts)
        assert any(attempt["prompt"] == [1] * 42 for attempt in completion_attempts)
        assert all(attempt.get("max_tokens") == 64 for attempt in completion_attempts)
    finally:
        proxy.close()




def test_qwen64k_packaged_subprocess_high_level_chat_fallback_passes_readiness(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    completion_kwargs_file = tmp_path / 'completion_kwargs.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_STRING_COMPLETION_ERROR', 'failed to eval prompt SECRET_PROMPT')
    monkeypatch.setenv('TOKEN_PLACE_TOKEN_COMPLETION_ERROR', 'failed to eval prompt SECRET_PROMPT')
    monkeypatch.setenv('TOKEN_PLACE_CHAT_COMPLETION_OK', '1')
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_KWARGS_JSONL', str(completion_kwargs_file))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        runtime, manager = _runtime_for(proxy)

        assert runtime.ensure_api_v1_runtime_ready() is True
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics['api_v1_readiness_completion_smoke_result'] == 'passed'
        attempts = [json.loads(line) for line in completion_kwargs_file.read_text().splitlines()]
        assert attempts[-1]['chat_template_kwargs'] == {'enable_thinking': False}
        assert attempts[-1]['max_tokens'] == 64
        assert all(attempt.get('max_tokens', 64) > 0 for attempt in attempts)
    finally:
        proxy.close()


def test_qwen64k_packaged_subprocess_all_plain_paths_fail_reports_safe_variant_diagnostics(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    completion_kwargs_file = tmp_path / 'completion_kwargs.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_STRING_COMPLETION_ERROR', 'failed to eval prompt SECRET_PROMPT SECRET_RENDERED_PROMPT')
    monkeypatch.setenv('TOKEN_PLACE_TOKEN_COMPLETION_ERROR', 'failed to eval prompt SECRET_KEY SECRET_TOOL_ARGS SECRET_CIPHERTEXT_INTERNALS')
    monkeypatch.setenv('TOKEN_PLACE_CHAT_COMPLETION_ERROR', 'failed to eval prompt SECRET_DECRYPTED_PAYLOAD SECRET_ASSISTANT_OUTPUT')
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_KWARGS_JSONL', str(completion_kwargs_file))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        runtime, manager = _runtime_for(proxy)

        assert runtime.ensure_api_v1_runtime_ready() is False
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics['api_v1_readiness_error_reason'] == 'runtime_completion_smoke_plain_completion_eval_failure'
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count'] == 3
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values'] == 'false,none,true'
        assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_attempt_count'] >= 6
        assert diagnostics['api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_attempted'] is True
        assert diagnostics['api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_succeeded'] is False
        dumped = json.dumps(diagnostics)
        assert all(sentinel not in dumped for sentinel in UNSAFE_READINESS_SENTINELS)
        for unsafe in ('rendered_prompt', 'token_ids', 'assistant_output', 'decrypted_payload', 'key', 'tool_args', 'payload', 'ciphertext'):
            assert f'"{unsafe}"' not in dumped
    finally:
        proxy.close()

def test_qwen64k_packaged_subprocess_thinking_leak_fails_closed_without_token_fallback(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'runtime'
    _write_fake_llama_cpp_runtime(runtime_root)
    completion_kwargs_file = tmp_path / 'completion_kwargs.jsonl'
    monkeypatch.syspath_prepend(str(runtime_root))
    monkeypatch.setenv('TOKEN_PLACE_STRING_COMPLETION_THINK', '<think>SECRET_ASSISTANT_OUTPUT</think> bad')
    monkeypatch.setenv('TOKEN_PLACE_COMPLETION_KWARGS_JSONL', str(completion_kwargs_file))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', type_k=8, type_v=8, timeout_seconds=5)
    try:
        runtime, manager = _runtime_for(proxy)

        assert runtime.ensure_api_v1_runtime_ready() is False
        diagnostics = manager.last_compute_diagnostics
        assert diagnostics["api_v1_readiness_completion_smoke_generation_exception_category"] == "thinking_leaked"
        assert diagnostics["api_v1_readiness_error_reason"] == "runtime_completion_smoke_plain_completion_thinking_leaked"
        dumped = json.dumps(diagnostics)
        assert all(sentinel not in dumped for sentinel in UNSAFE_READINESS_SENTINELS)
        assert '<think>' not in dumped

        completion_attempts = [
            json.loads(line) for line in completion_kwargs_file.read_text().splitlines()
        ]
        assert len(completion_attempts) == 1
        assert isinstance(completion_attempts[0]["prompt"], str)
        assert completion_attempts[0].get("max_tokens") == 64
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
    assert "top_k" not in fake.calls[0]
    assert diagnostics.get("api_v1_generation_kwargs_filtered", []) == []

def test_qwen64k_yarn_rope_freq_scale_fix_reproduces_old_decode_failure_and_passes_readiness(tmp_path):
    captured = {}

    class ConstructorSensitiveRuntime:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._bad_yarn = (
                kwargs.get('n_ctx') == 65536
                and (
                    kwargs.get('rope_freq_scale') != 0.5
                    or kwargs.get('yarn_ext_factor') == 2.0
                )
            )

        def apply_chat_template(self, *_args, **_kwargs):
            return '<qwen>'

        def tokenize(self, *_args, special=None, **_kwargs):
            return [3] * 28 if special is True else [1] * 50

        def create_completion(self, *_args, **_kwargs):
            if self._bad_yarn:
                raise RuntimeError('llama_decode returned -1')
            return {'choices': [{'text': 'ok'}]}

        def create_chat_completion(self, *, messages, max_tokens, chat_template_kwargs):
            _ = chat_template_kwargs
            if self._bad_yarn:
                raise RuntimeError('llama_decode returned -1')
            return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}

        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            if self._bad_yarn:
                raise RuntimeError('llama_decode returned -1')
            return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}

    old_runtime = ConstructorSensitiveRuntime(n_ctx=65536, rope_scaling_type=2, yarn_ext_factor=2.0, yarn_orig_ctx=32768)
    with pytest.raises(RuntimeError, match='llama_decode returned -1'):
        old_runtime.create_completion(prompt='x', max_tokens=1)
    captured.clear()

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manager.model_path).write_text('fake')
    fake_llama_cpp = SimpleNamespace(
        Llama=ConstructorSensitiveRuntime,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        __version__='0.3.32-test',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert captured['n_ctx'] == 65536
    assert captured['rope_scaling_type'] == 2
    assert captured['rope_freq_scale'] == 0.5
    assert captured['yarn_orig_ctx'] == 32768
    assert 'yarn_ext_factor' not in captured

    runtime, readiness_manager = _runtime_for(manager.llm)
    assert runtime.ensure_api_v1_runtime_ready() is True
    diagnostics = readiness_manager.last_compute_diagnostics
    assert diagnostics['api_v1_runtime_ready'] is True
    assert diagnostics['api_v1_readiness_completion_smoke_result'] == 'passed'
    assert diagnostics['api_v1_readiness_result'] == 'passed'
    assert diagnostics['api_v1_readiness_yarn_requested_context_tokens'] == 65536
    assert diagnostics['api_v1_readiness_yarn_original_context_tokens'] == 32768
    assert diagnostics['api_v1_readiness_yarn_context_multiplier'] == 2.0
    assert diagnostics['api_v1_readiness_yarn_rope_freq_scale'] == 0.5
    assert diagnostics['api_v1_readiness_yarn_ext_factor_overridden'] is False
    assert diagnostics['api_v1_readiness_yarn_configuration_valid'] is True
    assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant'] == 'tokenize_add_bos_false_special_false'
    assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count'] == 50
    assert diagnostics['api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special'] is False
    for value in diagnostics.values():
        assert not (isinstance(value, str) and any(marker in value for marker in UNSAFE_READINESS_SENTINELS))


def test_qwen64k_packaged_profile_recovery_f16_decode_failure_then_q8_success():
    """F16 smoke raises backend_graph_compute_failure; Q8 runtime passes; recovery count is 1."""
    f16_runtime = _Qwen64kFakeRuntime.__new__(_Qwen64kFakeRuntime)
    q8_runtime = _Qwen64kFakeRuntime()

    class FailingF16(_Qwen64kFakeRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "generation_exception_category": "backend_graph_compute_failure",
                    "plain_completion_backend_failure_category": "backend_graph_compute_failure",
                    "plain_completion_eval_return_code": -3,
                    "exception_type": "RuntimeError",
                    "sanitized_error_summary": "RuntimeError:redacted",
                },
            )

    f16_runtime = FailingF16()
    manager = _model_manager(f16_runtime)
    recovery_calls = []

    def _reinitialize(failed_rt, category, decode_return_code=None):
        recovery_calls.append((failed_rt, category, decode_return_code))
        manager._qwen_64k_first_readiness_failure_category = category
        manager._qwen_64k_profile_recovery_count = 1
        manager.get_llm_instance.return_value = q8_runtime
        return q8_runtime

    manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure.side_effect = _reinitialize

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 42)
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is True
    assert len(recovery_calls) == 1
    assert recovery_calls[0][0] is f16_runtime
    assert recovery_calls[0][1] == "backend_graph_compute_failure"
    assert recovery_calls[0][2] == -3
    assert manager._qwen_64k_profile_recovery_count == 1
    assert manager._qwen_64k_first_readiness_failure_category == "backend_graph_compute_failure"
    assert manager.last_compute_diagnostics["api_v1_readiness_result"] == "passed"


def test_qwen64k_packaged_profile_recovery_all_profiles_exhausted_fails_closed():
    """All three profiles fail the smoke; ensure_api_v1_runtime_ready returns False, first failure preserved."""
    class FailingRuntime(_Qwen64kFakeRuntime):
        def create_chat_completion_from_rendered_prompt(self, messages, **_kwargs):
            raise LlamaCppInferenceRequestError(
                "llama_cpp request failed",
                diagnostics={
                    "generation_exception_category": "backend_graph_compute_failure",
                    "plain_completion_backend_failure_category": "backend_graph_compute_failure",
                    "plain_completion_eval_return_code": -3,
                    "exception_type": "RuntimeError",
                    "sanitized_error_summary": "RuntimeError:redacted",
                },
            )

    f16_runtime = FailingRuntime()
    q8_runtime = FailingRuntime()
    q4_runtime = FailingRuntime()
    manager = _model_manager(f16_runtime)
    recovery_calls = []

    def _reinitialize(failed_rt, category, decode_return_code=None):
        recovery_calls.append((failed_rt, category))
        if len(recovery_calls) == 1:
            manager._qwen_64k_first_readiness_failure_category = category
            manager._qwen_64k_profile_recovery_count = 1
            manager.get_llm_instance.return_value = q8_runtime
            return q8_runtime
        if len(recovery_calls) == 2:
            manager._qwen_64k_profile_recovery_count = 2
            manager.get_llm_instance.return_value = q4_runtime
            return q4_runtime
        manager._qwen_64k_profile_recovery_count = 3
        return None

    manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure.side_effect = _reinitialize

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url="https://token.place", relay_port=None),
        model_manager=manager,
        relay_client=SimpleNamespace(
            _api_v1_authoritative_context_admission=lambda **_kwargs: (True, None, 42)
        ),
        crypto_manager=MagicMock(),
    )

    assert runtime.ensure_api_v1_runtime_ready() is False
    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure.call_count == 3
    assert recovery_calls[0][0] is f16_runtime
    assert recovery_calls[1][0] is q8_runtime
    assert recovery_calls[2][0] is q4_runtime
    assert manager._qwen_64k_first_readiness_failure_category == "backend_graph_compute_failure"
    assert manager._qwen_64k_profile_recovery_count == 3
