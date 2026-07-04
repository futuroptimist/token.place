import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from utils.context_profiles import apply_context_profile
from utils.llm import model_manager as model_manager_module
from utils.llm.model_manager import ModelManager


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


def _write_fake_llama_cpp_runtime(root: Path) -> None:
    package = root / 'llama_cpp'
    package.mkdir(parents=True)
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
        path = os.environ['TOKEN_PLACE_ATTEMPTS_JSONL']
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(kwargs, sort_keys=True) + '\\n')
        if os.environ.get('TOKEN_PLACE_FAIL_ALL_PROFILES') == '1' or 'type_k' not in kwargs:
            print('ggml_metal: KV cache allocation failed while creating llama_context', file=sys.stderr, flush=True)
            raise ValueError('Failed to create llama_context')
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
        return '<qwen>'
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
