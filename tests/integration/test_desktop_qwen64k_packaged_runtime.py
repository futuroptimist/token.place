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
        'model.n_gpu_layers': -1,
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    return config


def _write_fake_llama_cpp(site_dir, *, supports_yarn):
    package_dir = site_dir / 'llama_cpp'
    package_dir.mkdir(parents=True)
    init_file = package_dir / '__init__.py'
    if supports_yarn:
        init_file.write_text(
            "import json, os\n"
            "__version__ = '0.3.32'\n"
            "GGML_USE_METAL = True\n"
            "LLAMA_TYPE_Q8_0 = 8\n"
            "def llama_supports_gpu_offload(): return True\n"
            "class Llama:\n"
            "    def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, yarn_ext_factor, yarn_orig_ctx):\n"
            "        with open(os.environ['TOKEN_PLACE_CAPTURE_LLAMA_KWARGS'], 'w') as fh:\n"
            "            json.dump({\n"
            "                'model_path': model_path, 'n_gpu_layers': n_gpu_layers, 'n_ctx': n_ctx, 'verbose': verbose,\n"
            "                'rope_scaling_type': rope_scaling_type, 'yarn_ext_factor': yarn_ext_factor,\n"
            "                'yarn_orig_ctx': yarn_orig_ctx,\n"
            "            }, fh)\n"
            "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):\n"
            "        return [1, 2, 3] if tokenize else '<qwen>'\n"
        )
    else:
        init_file.write_text(
            "__version__ = '0.3.32'\n"
            "GGML_USE_METAL = True\n"
            "def llama_supports_gpu_offload(): return True\n"
            "class Llama:\n"
            "    def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):\n"
            "        pass\n"
        )
    return init_file


def _write_retry_fake_llama_cpp(site_dir, *, fail_all=False):
    package_dir = site_dir / 'llama_cpp'
    package_dir.mkdir(parents=True)
    init_file = package_dir / '__init__.py'
    init_file.write_text(
        "import json, os, sys\n"
        "__version__ = '0.3.32'\n"
        "GGML_USE_METAL = True\n"
        "GGML_TYPE_Q8_0 = 8\n"
        "GGML_TYPE_Q4_0 = 2\n"
        "GGML_TYPE_F16 = 1\n"
        "LLAMA_ROPE_SCALING_TYPE_YARN = 2\n"
        "def llama_supports_gpu_offload(): return True\n"
        "class Llama:\n"
        "    def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, yarn_ext_factor, yarn_orig_ctx, type_k=None, type_v=None, flash_attn=None, offload_kqv=None, n_batch=None, n_ubatch=None):\n"
        "        path = os.environ['TOKEN_PLACE_CAPTURE_LLAMA_KWARGS']\n"
        "        prior = []\n"
        "        if os.path.exists(path):\n"
        "            prior = json.load(open(path))\n"
        "        attempt = {'n_ctx': n_ctx, 'rope_scaling_type': rope_scaling_type, 'yarn_ext_factor': yarn_ext_factor, 'yarn_orig_ctx': yarn_orig_ctx, 'type_k': type_k, 'type_v': type_v, 'flash_attn': flash_attn, 'offload_kqv': offload_kqv, 'n_batch': n_batch, 'n_ubatch': n_ubatch}\n"
        "        prior.append(attempt)\n"
        "        json.dump(prior, open(path, 'w'))\n"
        "        print('ggml_metal: failed to allocate KV cache buffer for n_ctx=65536', file=sys.stderr, flush=True)\n"
        f"        if {bool(fail_all)!r} or type_k is None:\n"
        "            raise ValueError('Failed to create llama_context')\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):\n"
        "        return [1, 2, 3] if tokenize else '<qwen>'\n"
        "    def tokenize(self, content, add_bos=False):\n"
        "        return [1, 2, 3]\n"
    )
    return init_file


def test_packaged_subprocess_qwen64k_child_probe_prevents_parent_false_negative(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    site_dir = tmp_path / 'fake-site'
    fake_module_path = _write_fake_llama_cpp(site_dir, supports_yarn=True)
    capture_path = tmp_path / 'captured_kwargs.json'
    monkeypatch.syspath_prepend(str(site_dir))
    monkeypatch.setenv('TOKEN_PLACE_CAPTURE_LLAMA_KWARGS', str(capture_path))

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    stale_parent_probe = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'metal_already_supported',
        'llama_module_path': str(fake_module_path),
        'constructor_kwarg_support': {
            'rope_scaling_type': False,
            'yarn_ext_factor': False,
            'yarn_orig_ctx': False,
        },
        'capability_source': 'parent_facade_signature',
    }

    facade = model_manager_module._SubprocessLlamaCppModule(
        str(fake_module_path),
        desktop_runtime_probe=stale_parent_probe,
    )

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    import json
    captured = json.loads(capture_path.read_text())
    assert captured['n_ctx'] == 65536
    assert captured['rope_scaling_type'] is not None
    assert captured['yarn_ext_factor'] == 2.0
    assert captured['yarn_orig_ctx'] == 32768
    assert manager.last_yarn_rope_diagnostics['capability_source'] == 'worker_probe'
    assert manager.last_yarn_rope_diagnostics['parent_facade_type'] == '_SubprocessLlamaCppModule'


def test_packaged_subprocess_qwen64k_child_unsupported_fails_before_ready(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    site_dir = tmp_path / 'fake-site-unsupported'
    fake_module_path = _write_fake_llama_cpp(site_dir, supports_yarn=False)
    capture_path = tmp_path / 'captured_kwargs.json'
    monkeypatch.syspath_prepend(str(site_dir))
    monkeypatch.setenv('TOKEN_PLACE_CAPTURE_LLAMA_KWARGS', str(capture_path))

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')
    facade = model_manager_module._SubprocessLlamaCppModule(str(fake_module_path))

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert not capture_path.exists()
    assert manager.last_yarn_rope_diagnostics['supported'] is False
    assert manager.last_yarn_rope_diagnostics['capability_source'] == 'worker_probe'
    assert manager.last_yarn_rope_diagnostics['child_probe_reprobe_attempted'] is True
    assert 'Qwen 64K requires YaRN/RoPE support' in manager.last_runtime_init_error
    assert 'missing constructor kwargs' in manager.last_runtime_init_error


def test_packaged_subprocess_qwen64k_retries_after_llama_context_create_failure(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    site_dir = tmp_path / 'fake-site-retry'
    fake_module_path = _write_retry_fake_llama_cpp(site_dir)
    capture_path = tmp_path / 'captured_retry_kwargs.json'
    monkeypatch.syspath_prepend(str(site_dir))
    monkeypatch.setenv('TOKEN_PLACE_CAPTURE_LLAMA_KWARGS', str(capture_path))

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')
    facade = model_manager_module._SubprocessLlamaCppModule(str(fake_module_path))

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    import json
    attempts = json.loads(capture_path.read_text())
    assert len(attempts) == 2
    assert attempts[0]['n_ctx'] == 65536
    assert attempts[0]['type_k'] is None
    assert attempts[1]['type_k'] == 8
    assert attempts[1]['type_v'] == 8
    assert 'flash_attn' in attempts[1]
    assert manager.last_compute_diagnostics['selected_runtime_profile'] == 'qwen64k_kv_q8'
    assert manager.last_compute_diagnostics['qwen_64k_init_attempts'][0]['safe_error_category'] in {
        'runtime_context_create_kv_cache_allocation',
        'runtime_context_create_metal_memory',
        'runtime_context_create_failed',
    }


def test_packaged_subprocess_qwen64k_profile_exhaustion_fails_closed(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    site_dir = tmp_path / 'fake-site-fail-all'
    fake_module_path = _write_retry_fake_llama_cpp(site_dir, fail_all=True)
    capture_path = tmp_path / 'captured_fail_all_kwargs.json'
    monkeypatch.syspath_prepend(str(site_dir))
    monkeypatch.setenv('TOKEN_PLACE_CAPTURE_LLAMA_KWARGS', str(capture_path))

    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')
    facade = model_manager_module._SubprocessLlamaCppModule(str(fake_module_path))

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=facade), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    import json
    attempts = json.loads(capture_path.read_text())
    assert [attempt['type_k'] for attempt in attempts] == [None, 8, 2]
    assert 'memory/KV/cache runtime profile exhaustion' in manager.last_runtime_init_error
    assert manager.worker_state == 'failed'
