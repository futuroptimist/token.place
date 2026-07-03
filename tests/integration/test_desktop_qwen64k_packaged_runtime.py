import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from utils.context_profiles import apply_context_profile
from utils.llm import model_manager as model_manager_module
from utils.llm.model_manager import ModelManager


def _config(tmp_path):
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': 0,
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config = MagicMock(is_production=False)
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    return config


def _write_fake_llama_cpp(site_dir: Path, record_path: Path, *, supports_yarn: bool) -> None:
    pkg = site_dir / 'llama_cpp'
    pkg.mkdir(parents=True)
    if supports_yarn:
        init = f'''
__version__ = '0.3.32'
LLAMA_ROPE_SCALING_TYPE_YARN = 2
GGML_USE_METAL = True

def llama_supports_gpu_offload():
    return True

class Llama:
    def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, yarn_ext_factor, yarn_orig_ctx, **kwargs):
        import json
        with open({str(record_path)!r}, 'w', encoding='utf-8') as handle:
            json.dump({{
                'model_path': model_path,
                'n_gpu_layers': n_gpu_layers,
                'n_ctx': n_ctx,
                'verbose': verbose,
                'rope_scaling_type': rope_scaling_type,
                'yarn_ext_factor': yarn_ext_factor,
                'yarn_orig_ctx': yarn_orig_ctx,
                **kwargs,
            }}, handle)

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
        return '<qwen>'
'''
    else:
        init = '''
__version__ = '0.3.32'
GGML_USE_METAL = True

def llama_supports_gpu_offload():
    return True

class Llama:
    def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
        pass

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
        return '<qwen>'
'''
    (pkg / '__init__.py').write_text(init, encoding='utf-8')


def _prepare_packaged_qwen64k(tmp_path, monkeypatch, *, supports_yarn: bool):
    fake_site = tmp_path / 'fake_site'
    record_path = tmp_path / 'kwargs.json'
    _write_fake_llama_cpp(fake_site, record_path, supports_yarn=supports_yarn)
    monkeypatch.syspath_prepend(str(fake_site))
    sys.modules.pop('llama_cpp', None)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    manager = ModelManager(_config(tmp_path))
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake model', encoding='utf-8')
    manager.requested_compute_mode = 'cpu'
    return manager, record_path


def test_packaged_subprocess_facade_qwen64k_uses_child_yarn_probe(tmp_path, monkeypatch):
    manager, record_path = _prepare_packaged_qwen64k(tmp_path, monkeypatch, supports_yarn=True)

    with patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        llm = manager.get_llm_instance()

    assert llm is not None
    captured = json.loads(record_path.read_text(encoding='utf-8'))
    assert captured['n_ctx'] == 65536
    assert captured['rope_scaling_type'] == 2
    assert captured['yarn_ext_factor'] == 2.0
    assert captured['yarn_orig_ctx'] == 32768
    assert manager.last_yarn_rope_diagnostics['constructor_signature_classification'] in {'explicit_signature', 'var_kwargs', 'child_runtime_probe'}
    assert manager.last_yarn_rope_diagnostics['yarn_resolver_source'] in {'top_level_enum', 'numeric_fallback'}


def test_packaged_subprocess_facade_qwen64k_fails_closed_when_child_lacks_yarn(tmp_path, monkeypatch):
    manager, record_path = _prepare_packaged_qwen64k(tmp_path, monkeypatch, supports_yarn=False)

    with patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert not record_path.exists()
    assert 'Qwen 64K requires YaRN/RoPE support in llama-cpp-python' in manager.last_runtime_init_error
    assert 'yarn_resolver_source=unsupported' in manager.last_runtime_init_error
