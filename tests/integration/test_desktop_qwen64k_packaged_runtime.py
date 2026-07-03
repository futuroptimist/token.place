import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from utils.context_profiles import apply_context_profile
from utils.llm.model_manager import ModelManager


def _write_fake_llama_cpp(fake_site: Path, *, supports_yarn: bool) -> None:
    pkg = fake_site / 'llama_cpp'
    pkg.mkdir(parents=True)
    init_signature = (
        'model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, yarn_ext_factor, yarn_orig_ctx, **kwargs'
        if supports_yarn
        else 'model_path, n_gpu_layers, n_ctx, verbose'
    )
    init_body = (
        "        data['constructor_kwargs'] = {'model_path': model_path, 'n_gpu_layers': n_gpu_layers, 'n_ctx': n_ctx, 'verbose': verbose, 'rope_scaling_type': rope_scaling_type, 'yarn_ext_factor': yarn_ext_factor, 'yarn_orig_ctx': yarn_orig_ctx, **kwargs}\n"
        if supports_yarn
        else "        data['constructor_kwargs'] = {'model_path': model_path, 'n_gpu_layers': n_gpu_layers, 'n_ctx': n_ctx, 'verbose': verbose}\n"
    )
    enum_line = 'LLAMA_ROPE_SCALING_TYPE_YARN = 2\n' if supports_yarn else ''
    (pkg / '__init__.py').write_text(
        f"""
import json, os
from pathlib import Path
__version__ = '0.3.32-fake'
GGML_USE_METAL = True
LLAMA_TYPE_Q8_0 = 8
{enum_line}

def llama_supports_gpu_offload():
    return True

class Llama:
    def __init__(self, {init_signature}):
        state = Path(os.environ['TOKEN_PLACE_FAKE_LLAMA_STATE'])
        data = json.loads(state.read_text(encoding='utf-8')) if state.exists() else {{}}
{init_body}        state.write_text(json.dumps(data, sort_keys=True), encoding='utf-8')

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
        return '<|im_start|>assistant'

    def create_chat_completion(self, *args, **kwargs):
        return {{'choices': [{{'message': {{'content': 'ok'}}}}]}}
""",
        encoding='utf-8',
    )


def _manager(tmp_path):
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake qwen gguf metadata placeholder', encoding='utf-8')
    manager.requested_compute_mode = 'cpu'
    return manager


@pytest.mark.parametrize('supports_yarn', [True])
def test_qwen64k_packaged_subprocess_facade_uses_child_yarn_probe(tmp_path, monkeypatch, supports_yarn):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'fake_site'
    state = tmp_path / 'state.json'
    state.write_text('{}', encoding='utf-8')
    _write_fake_llama_cpp(fake_site, supports_yarn=supports_yarn)
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_FAKE_LLAMA_STATE', str(state))
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)

    manager = _manager(tmp_path)

    assert manager.get_llm_instance() is not None
    recorded = json.loads(state.read_text(encoding='utf-8'))['constructor_kwargs']
    assert recorded['n_ctx'] == 65536
    assert recorded['rope_scaling_type'] == 2
    assert recorded['yarn_ext_factor'] == 2.0
    assert recorded['yarn_orig_ctx'] == 32768
    assert manager.last_compute_diagnostics['yarn_rope_diagnostics']['capability_source'] == 'worker_probe'


def test_qwen64k_packaged_subprocess_facade_fails_closed_without_child_yarn(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'fake_site'
    state = tmp_path / 'state.json'
    state.write_text('{}', encoding='utf-8')
    _write_fake_llama_cpp(fake_site, supports_yarn=False)
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_FAKE_LLAMA_STATE', str(state))
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)

    manager = _manager(tmp_path)

    assert manager.get_llm_instance() is None
    assert 'runtime_yarn_constructor_kwargs_unsupported' in manager.last_runtime_init_error
    assert 'constructor_kwargs' not in json.loads(state.read_text(encoding='utf-8'))
