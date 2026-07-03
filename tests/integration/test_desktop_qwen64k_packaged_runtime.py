from pathlib import Path
from unittest.mock import MagicMock, patch

from utils.context_profiles import apply_context_profile
from utils.llm.model_manager import ModelManager


def _config(tmp_path):
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': 0,
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    return config


def test_packaged_subprocess_qwen64k_child_probe_prevents_parent_false_negative(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    stale_parent_probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'metal_already_supported',
        'constructor_kwarg_support': {
            'rope_scaling_type': False,
            'yarn_ext_factor': False,
            'yarn_orig_ctx': False,
        },
        'capability_source': 'parent_facade_signature',
    }
    child_probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'llama_module_path': '/packaged/real/llama_cpp/__init__.py',
        'llama_cpp_python_version': '0.3.32',
        'constructor_kwarg_support': {
            'rope_scaling_type': True,
            'yarn_ext_factor': True,
            'yarn_orig_ctx': True,
            'type_k': False,
            'type_v': False,
            'flash_attn': False,
            'offload_kqv': False,
            'n_batch': False,
            'n_ubatch': False,
        },
        'constructor_signature_inspectable': True,
        'constructor_has_var_kwargs': False,
        'yarn_resolver_source': 'numeric_fallback',
        'yarn_enum_value': 2,
        'qwen_64k_yarn_support': 'supported',
        'capability_source': 'worker_probe',
    }
    captured = {}

    class RecordingProxy:
        __token_place_supported_constructor_kwargs__ = ()

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return [1, 2, 3] if tokenize else '<qwen>'

    class RecordingFacade(model_manager_module._SubprocessLlamaCppModule):
        @property
        def Llama(self):
            return RecordingProxy

    facade = RecordingFacade(
        '/packaged/facade/llama_cpp/__init__.py',
        desktop_runtime_probe=stale_parent_probe,
    )
    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', lambda **_: child_probe)

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert captured['n_ctx'] == 65536
    assert captured['rope_scaling_type'] == 2
    assert captured['yarn_ext_factor'] == 2.0
    assert captured['yarn_orig_ctx'] == 32768
    assert manager.last_yarn_rope_diagnostics['capability_source'] == 'worker_probe'


def test_packaged_subprocess_qwen64k_child_unsupported_fails_before_ready(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')
    facade = model_manager_module._SubprocessLlamaCppModule('/packaged/facade/llama_cpp/__init__.py')
    child_probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'constructor_kwarg_support': {
            'rope_scaling_type': False,
            'yarn_ext_factor': False,
            'yarn_orig_ctx': False,
        },
        'constructor_signature_inspectable': True,
        'yarn_resolver_source': 'unsupported',
        'qwen_64k_yarn_support': 'unsupported',
        'capability_source': 'worker_probe',
    }
    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', lambda **_: child_probe)

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert manager.last_yarn_rope_diagnostics['supported'] is False
    assert manager.last_yarn_rope_diagnostics['capability_source'] == 'worker_probe'
    assert 'Qwen 64K requires YaRN/RoPE support' in manager.last_runtime_init_error
    assert 'missing constructor kwargs' in manager.last_runtime_init_error
