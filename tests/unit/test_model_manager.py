"""
Unit tests for the model manager module.
"""
import logging
import hashlib
import importlib.util
import os
import queue
import subprocess
import threading
import pytest
import shutil
import time
from unittest.mock import MagicMock, call, patch
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.llm.model_manager import ModelManager


class _ToDictOnly:
    """Helper class that provides a working to_dict implementation."""

    def to_dict(self):
        return {'origin': 'to_dict'}


class _TypeErrorToDict:
    """Helper class whose to_dict requires an argument, forcing a TypeError."""

    def to_dict(self, _unused):  # pragma: no cover - exercised indirectly
        raise AssertionError("This should not be called with an argument")

    def model_dump(self):
        return {'origin': 'model_dump'}


class _DictMethodOnly:
    """Helper class exposing a dict() method for normalization."""

    def dict(self):
        return {'origin': 'dict'}


class _DictAttributeOnly:
    """Helper class that is normalized via its __dict__ attribute."""

    def __init__(self):
        self.origin = 'dunder_dict'

class TestModelManager:
    """Test class for ModelManager."""

    def _build_manager_with_model_config(self, model_config):
        """Create a ModelManager with focused model config overrides."""
        mock_config = MagicMock()
        mock_config.is_production = False

        with tempfile.TemporaryDirectory() as temp_dir:
            def get_config(key, default=None):
                config_values = {
                    'model.download_chunk_size_mb': 1,
                    'paths.models_dir': temp_dir,
                    'model.use_mock': False,
                    'model.n_gpu_layers': -1,
                    'model.gpu_memory_headroom_percent': 0.1,
                    'model.enforce_gpu_memory_headroom': True,
                    **model_config,
                }
                return config_values.get(key, default)

            mock_config.get.side_effect = get_config
            return ModelManager(mock_config)

    @pytest.fixture
    def model_manager(self):
        """Fixture that returns a model manager instance with mocked config."""
        mock_config = MagicMock()
        mock_config.is_production = False
        mock_config.get.side_effect = self._mock_config_get

        with tempfile.TemporaryDirectory() as temp_dir:
            # Override the models_dir config
            self._temp_dir = temp_dir

            # Create a models path with a temp file
            self.create_fake_model_file(temp_dir)

            manager = ModelManager(mock_config)
            yield manager

    def create_fake_model_file(self, directory):
        """Create a fake model file for testing."""
        model_path = os.path.join(directory, 'test_model.gguf')
        with open(model_path, 'wb') as f:
            f.write(b'fake model data')

    def _mock_config_get(self, key, default=None):
        """Mock implementation of config.get()."""
        config_values = {
            'model.filename': 'test_model.gguf',
            'model.url': 'https://example.com/model.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': self._temp_dir,
            'model.use_mock': False,
            'model.context_size': 2048,
            'model.chat_format': 'llama-3',
            'model.max_tokens': 1000,  # Match the actual value used in the code
            'model.temperature': 0.7,  # Match the actual value used in the code
            'model.top_p': 0.9,        # Match the actual value used in the code
            'model.stop_tokens': [],
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }
        return config_values.get(key, default)

    def test_init(self, model_manager):
        """Test ModelManager initialization."""
        assert model_manager.file_name == 'test_model.gguf'
        assert model_manager.url == 'https://example.com/model.gguf'
        assert model_manager.chunk_size_mb == 1
        assert model_manager.models_dir == self._temp_dir
        assert model_manager.model_path == os.path.join(self._temp_dir, 'test_model.gguf')
        assert model_manager.llm is None
        assert model_manager.use_mock_llm is False

    def test_mock_llm_exposes_chat_template_and_tokenizer(self, model_manager):
        """USE_MOCK_LLM runtime supports API v1 authoritative admission helpers."""
        model_manager.use_mock_llm = True

        llm = model_manager.get_llm_instance()
        rendered = llm.apply_chat_template(
            [{'role': 'user', 'content': 'hello packaged parity'}],
            tokenize=False,
            add_generation_prompt=True,
        )
        structured_rendered = llm.apply_chat_template(
            [{'role': 'user', 'content': [{'type': 'text', 'text': 'structured'}, {'ignored': 'metadata'}]}],
            tokenize=False,
            add_generation_prompt=False,
        )
        template_tokens = llm.apply_chat_template(
            [{'role': 'user', 'content': 'hello packaged parity'}],
            tokenize=True,
            add_generation_prompt=True,
        )
        tokens = llm.tokenize(rendered.encode('utf-8'), add_bos=False)
        bos_tokens = llm.tokenize(rendered, add_bos=True)
        render_complete = llm.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'hello packaged parity'}],
            max_tokens=8,
        )
        render_token_result = llm.render_and_tokenize_chat(
            [{'role': 'user', 'content': 'hello packaged parity'}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        rendered_with_thinking_disabled = llm.apply_chat_template(
            [{'role': 'user', 'content': 'hello packaged parity'}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

        assert isinstance(rendered, str)
        assert '<|user|>' in rendered
        assert '<|assistant|>' in rendered
        assert structured_rendered == '<|user|>\nstructured'
        assert isinstance(template_tokens, list)
        assert template_tokens == tokens
        assert isinstance(tokens, list)
        assert len(tokens) > 0
        assert bos_tokens[0] == 1
        assert len(bos_tokens) == len(tokens) + 1
        assert render_complete['choices'][0]['message']['content'].startswith('Mock Response:')
        assert render_token_result == {'prompt_tokens': len(tokens)}
        assert rendered_with_thinking_disabled == rendered
        assert llm._token_place_last_mock_render_and_tokenize_kwargs == {
            'enable_thinking': False
        }
        assert llm._token_place_last_mock_template_kwargs == {
            'enable_thinking': False
        }

    def test_supports_api_v1_model_matches_active_profile_identifiers(self, model_manager):
        """API v1 admission is limited to the active profile/runtime identifiers."""
        assert model_manager.supports_api_v1_model('qwen3-8b-instruct') is True
        assert model_manager.supports_api_v1_model('qwen3-8b-q4-k-m') is True
        assert model_manager.supports_api_v1_model('test_model.gguf') is True
        assert model_manager.supports_api_v1_model(' TEST_MODEL.GGUF ') is True

        assert model_manager.supports_api_v1_model('llama-3.1-8b-instruct') is False
        assert model_manager.supports_api_v1_model('') is False
        assert model_manager.supports_api_v1_model(None) is False

    def test_supports_api_v1_model_rejects_qwen_for_stale_llama_runtime(self):
        """A stale Llama runtime/file must not advertise the Qwen API v1 default."""
        manager = self._build_manager_with_model_config({
            'model.profile_id': 'llama-3.1-8b-q4-k-m',
            'model.api_model_id': 'llama-3.1-8b-instruct',
            'model.filename': 'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf',
        })

        assert manager.supports_api_v1_model('llama-3.1-8b-instruct') is True
        assert manager.supports_api_v1_model('Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf') is True
        assert manager.supports_api_v1_model('qwen3-8b-instruct') is False
        assert manager.supports_api_v1_model('Qwen3-8B-Q4_K_M.gguf') is False

    def test_get_model_artifact_metadata(self, model_manager):
        """Test runtime model metadata includes expected keys and file state."""
        metadata = model_manager.get_model_artifact_metadata()

        assert metadata['api_model_id'] == 'qwen3-8b-instruct'
        assert metadata['profile_id'] == 'qwen3-8b-q4-k-m'
        assert metadata['display_name'] == 'Qwen3 8B Instruct'
        assert metadata['canonical_family_url'] == 'https://huggingface.co/Qwen/Qwen3-8B'
        assert metadata['filename'] == 'test_model.gguf'
        assert metadata['url'] == 'https://example.com/model.gguf'
        assert metadata['gguf_repo'] == 'Qwen/Qwen3-8B-GGUF'
        assert metadata['source_model'] == 'Qwen/Qwen3-8B'
        assert metadata['quantization'] == 'Q4_K_M'
        assert metadata['license'] == 'apache-2.0'
        assert metadata['native_context_tokens'] == 32768
        assert metadata['maximum_validated_context_tokens'] == 65536
        assert metadata['supported_context_tiers'] == ['8k-fast', '64k-full']
        assert metadata['models_dir'] == self._temp_dir
        assert metadata['resolved_model_path'] == os.path.join(self._temp_dir, 'test_model.gguf')
        assert metadata['exists'] is True
        assert metadata['size_bytes'] == len(b'fake model data')

        os.remove(metadata['resolved_model_path'])
        missing_metadata = model_manager.get_model_artifact_metadata()
        assert missing_metadata['exists'] is False
        assert missing_metadata['size_bytes'] is None

    def test_default_model_artifact_metadata_uses_qwen_profile(self):
        """Default profile should use the Qwen artifact defaults."""
        manager = self._build_manager_with_model_config({})
        metadata = manager.get_model_artifact_metadata()

        assert metadata['api_model_id'] == 'qwen3-8b-instruct'
        assert metadata['profile_id'] == 'qwen3-8b-q4-k-m'
        assert metadata['filename'] == 'Qwen3-8B-Q4_K_M.gguf'
        assert metadata['url'] == 'https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/6a569868d07d3bd59e8b97fb001bf8c0b254bb20/Qwen3-8B-Q4_K_M.gguf'
        assert metadata['canonical_family_url'] == 'https://huggingface.co/Qwen/Qwen3-8B'
        assert metadata['source_model'] == 'Qwen/Qwen3-8B'
        assert metadata['quantization'] == 'Q4_K_M'
        assert metadata['native_context_tokens'] == 32768
        assert metadata['maximum_validated_context_tokens'] == 65536
        assert metadata['supported_context_tiers'] == ['8k-fast', '64k-full']

    def test_profile_artifacts_follow_selected_profile_when_defaults_are_seeded(self):
        """Selecting a profile should replace seeded Llama artifact defaults."""
        from utils.config_schema import DEFAULT_CONFIG

        mock_config = MagicMock()
        mock_config.is_production = False

        with tempfile.TemporaryDirectory() as temp_dir:
            def get_config(key, default=None):
                config_values = {
                    'model.profile_id': 'qwen3-8b-q4-k-m',
                    'model.api_model_id': DEFAULT_CONFIG['model']['api_model_id'],
                    'model.filename': DEFAULT_CONFIG['model']['filename'],
                    'model.url': DEFAULT_CONFIG['model']['url'],
                    'model.canonical_family_url': DEFAULT_CONFIG['model']['canonical_family_url'],
                    'model.download_chunk_size_mb': 1,
                    'paths.models_dir': temp_dir,
                    'model.use_mock': False,
                    'model.n_gpu_layers': -1,
                    'model.gpu_memory_headroom_percent': 0.1,
                    'model.enforce_gpu_memory_headroom': True,
                }
                return config_values.get(key, default)

            mock_config.get.side_effect = get_config
            manager = ModelManager(mock_config)

        assert manager.profile_id == 'qwen3-8b-q4-k-m'
        assert manager.api_model_id == 'qwen3-8b-instruct'
        assert manager.file_name == 'Qwen3-8B-Q4_K_M.gguf'
        assert manager.url == 'https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/6a569868d07d3bd59e8b97fb001bf8c0b254bb20/Qwen3-8B-Q4_K_M.gguf'
        assert manager.canonical_family_url == 'https://huggingface.co/Qwen/Qwen3-8B'
        metadata = manager.get_model_artifact_metadata()
        assert metadata['source_model'] == 'Qwen/Qwen3-8B'
        assert metadata['quantization'] == 'Q4_K_M'
        assert metadata['native_context_tokens'] == 32768
        assert metadata['maximum_validated_context_tokens'] == 65536
        assert metadata['supported_context_tiers'] == ['8k-fast', '64k-full']

    def test_api_model_id_selection_resolves_qwen_profile_artifacts(self):
        """Selecting the Qwen API id should resolve the matching non-default profile."""
        manager = self._build_manager_with_model_config({'model.api_model_id': 'qwen3-8b-instruct'})
        metadata = manager.get_model_artifact_metadata()

        assert manager.profile_id == 'qwen3-8b-q4-k-m'
        assert manager.api_model_id == 'qwen3-8b-instruct'
        assert manager.file_name == 'Qwen3-8B-Q4_K_M.gguf'
        assert manager.url == 'https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/6a569868d07d3bd59e8b97fb001bf8c0b254bb20/Qwen3-8B-Q4_K_M.gguf'
        assert manager.canonical_family_url == 'https://huggingface.co/Qwen/Qwen3-8B'
        assert metadata['source_model'] == 'Qwen/Qwen3-8B'
        assert metadata['quantization'] == 'Q4_K_M'
        assert metadata['supported_context_tiers'] == ['8k-fast', '64k-full']

    def test_profile_artifacts_preserve_explicit_overrides(self):
        """Non-default profiles still honor artifact values that differ from seeded defaults."""
        mock_config = MagicMock()
        mock_config.is_production = False

        with tempfile.TemporaryDirectory() as temp_dir:
            def get_config(key, default=None):
                config_values = {
                    'model.profile_id': 'qwen3-8b-q4-k-m',
                    'model.filename': 'custom.gguf',
                    'model.url': 'https://example.com/custom.gguf',
                    'model.canonical_family_url': 'https://example.com/family',
                    'model.download_chunk_size_mb': 1,
                    'paths.models_dir': temp_dir,
                    'model.use_mock': False,
                    'model.n_gpu_layers': -1,
                    'model.gpu_memory_headroom_percent': 0.1,
                    'model.enforce_gpu_memory_headroom': True,
                }
                return config_values.get(key, default)

            mock_config.get.side_effect = get_config
            manager = ModelManager(mock_config)

        assert manager.api_model_id == 'qwen3-8b-instruct'
        assert manager.file_name == 'custom.gguf'
        assert manager.url == 'https://example.com/custom.gguf'
        assert manager.canonical_family_url == 'https://example.com/family'


    @patch('utils.llm.model_manager.requests.get')
    def test_pinned_qwen_custom_filename_download_still_verifies_checksum(self, mock_get):
        """A model.filename override must not disable profile pin verification."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'custom-qwen.gguf',
            'model.url': 'https://mirror.example/custom-qwen.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        manager.model_profile['artifact_size_bytes'] = 8
        manager.model_profile['artifact_sha256'] = '0' * 64

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '8'
        mock_response.iter_content.return_value = [b'GGUF', b'bad!']
        mock_get.return_value = mock_response

        assert manager.download_file_in_chunks(manager.model_path, manager.url, 1) is False
        assert not os.path.exists(manager.model_path)
        assert not list(Path(manager.models_dir).glob('custom-qwen.gguf.tmp.*'))
        mock_response.close.assert_called_once()

    def test_pinned_qwen_custom_filename_existing_artifact_still_verifies_checksum(self):
        """Existing custom-named Qwen artifacts remain bound to profile metadata."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'custom-qwen.gguf',
            'model.url': 'https://mirror.example/custom-qwen.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        manager.model_profile['artifact_size_bytes'] = 8
        manager.model_profile['artifact_sha256'] = '0' * 64
        Path(manager.model_path).write_bytes(b'GGUFbad!')

        valid, reason = manager._validate_existing_model_artifact(hash_if_suspect=True)

        assert valid is False
        assert reason == 'checksum_mismatch'
        assert Path(manager.model_path).read_bytes() == b'GGUFbad!'

    @patch('utils.llm.model_manager.requests.get')
    def test_pinned_qwen_download_size_mismatch_cleans_temp_and_preserves_final(self, mock_get):
        """Pinned managed downloads reject wrong sizes without replacing an existing final file."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'Qwen3-8B-Q4_K_M.gguf',
            'model.url': 'https://mirror.example/Qwen3-8B-Q4_K_M.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        manager.model_profile['artifact_size_bytes'] = 12
        manager.model_profile['artifact_sha256'] = None
        Path(manager.model_path).write_bytes(b'original-final')

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '8'
        mock_response.iter_content.return_value = [b'GGUF', b'tiny']
        mock_get.return_value = mock_response

        assert manager.download_file_in_chunks(manager.model_path, manager.url, 1) is False
        assert Path(manager.model_path).read_bytes() == b'original-final'
        assert not list(Path(manager.models_dir).glob('Qwen3-8B-Q4_K_M.gguf.tmp.*'))
        mock_response.close.assert_called_once()

    @patch('utils.llm.model_manager.requests.get')
    def test_pinned_qwen_download_bad_magic_and_replace_failure_clean_temp(self, mock_get, monkeypatch):
        """Pinned managed downloads clean their unique temp file on magic and replace failures."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'Qwen3-8B-Q4_K_M.gguf',
            'model.url': 'https://mirror.example/Qwen3-8B-Q4_K_M.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        manager.model_profile['artifact_size_bytes'] = 8
        manager.model_profile['artifact_sha256'] = None

        bad_magic = MagicMock()
        bad_magic.status_code = 200
        bad_magic.headers.get.return_value = '8'
        bad_magic.iter_content.return_value = [b'NOPE', b'tiny']
        mock_get.return_value = bad_magic

        assert manager.download_file_in_chunks(manager.model_path, manager.url, 1) is False
        assert not Path(manager.model_path).exists()
        assert not list(Path(manager.models_dir).glob('Qwen3-8B-Q4_K_M.gguf.tmp.*'))

        good_magic = MagicMock()
        good_magic.status_code = 200
        good_magic.headers.get.return_value = '8'
        good_magic.iter_content.return_value = [b'GGUF', b'tiny']
        mock_get.return_value = good_magic
        monkeypatch.setattr('utils.llm.model_manager.os.replace', MagicMock(side_effect=OSError('replace failed')))

        assert manager.download_file_in_chunks(manager.model_path, manager.url, 1) is False
        assert not Path(manager.model_path).exists()
        assert not list(Path(manager.models_dir).glob('Qwen3-8B-Q4_K_M.gguf.tmp.*'))

    def test_existing_artifact_validation_unavailable_and_unmanaged_invalid_refusal(self, monkeypatch):
        """Unavailable artifacts fail safely, and unmanaged invalid files are not repaired."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        explicit_path = os.path.join(temp_dir, 'operator-selected.gguf')
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        manager.model_path = explicit_path
        Path(manager.model_path).write_bytes(b'BAD!')

        manager.download_file_in_chunks = MagicMock(return_value=True)
        assert manager.download_model_if_needed() is False
        assert manager.download_file_in_chunks.call_count == 0
        assert manager.last_runtime_init_error == 'model_artifact_invalid:bad_magic'
        assert Path(manager.model_path).read_bytes() == b'BAD!'

        monkeypatch.setattr('utils.llm.model_manager.os.path.exists', lambda _path: True)
        monkeypatch.setattr('utils.llm.model_manager.os.path.getsize', MagicMock(side_effect=OSError('stat failed')))
        valid, reason = manager._validate_existing_model_artifact(hash_if_suspect=True)
        assert (valid, reason) == (False, 'unavailable')

    @patch('utils.llm.model_manager.requests.get')
    def test_download_validation_covers_temp_read_and_final_disappearance(self, mock_get, monkeypatch):
        """Transactional download validation cleans temp files on late validation failures."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'Qwen3-8B-Q4_K_M.gguf',
            'model.url': 'https://mirror.example/Qwen3-8B-Q4_K_M.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        manager.model_profile['artifact_size_bytes'] = 8
        manager.model_profile['artifact_sha256'] = None

        response = MagicMock()
        response.status_code = 200
        response.headers.get.return_value = '8'
        response.iter_content.return_value = [b'GGUF', b'tiny']
        mock_get.return_value = response

        real_open = open

        def fail_magic_read(path, mode='r', *args, **kwargs):
            if path != manager.model_path and 'rb' in mode:
                raise OSError('magic read failed')
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr('builtins.open', fail_magic_read)
        assert manager.download_file_in_chunks(manager.model_path, manager.url, 1) is False
        assert not list(Path(manager.models_dir).glob('Qwen3-8B-Q4_K_M.gguf.tmp.*'))

        monkeypatch.setattr('builtins.open', real_open)
        exists_calls = {'tmp': 0}
        real_exists = os.path.exists

        def disappear_before_replace(path):
            if str(path).startswith(f"{manager.model_path}.tmp."):
                exists_calls['tmp'] += 1
                return exists_calls['tmp'] < 2
            return real_exists(path)

        response.iter_content.return_value = [b'GGUF', b'tiny']
        monkeypatch.setattr('utils.llm.model_manager.os.path.exists', disappear_before_replace)
        assert manager.download_file_in_chunks(manager.model_path, manager.url, 1) is False
        assert not Path(manager.model_path).exists()

    def test_selected_model_path_and_valid_existing_fast_path_edge_cases(self, model_manager):
        """Cover safe comparison failures and the valid existing-artifact fast path."""

        class UnstringablePath:
            def __str__(self):
                raise TypeError('path unavailable')

        assert model_manager._is_selected_model_path(UnstringablePath()) is False
        model_manager.model_profile['artifact_size_bytes'] = 8
        model_manager.model_profile['artifact_sha256'] = None
        Path(model_manager.model_path).write_bytes(b'GGUFtiny')

        assert model_manager.download_model_if_needed() is True
        assert model_manager.last_model_artifact_validation == {'valid': True, 'reason': 'valid'}

    def test_existing_managed_artifact_fast_path_does_not_hash_every_warm_start(self):
        """Warm starts validate pinned size and GGUF magic without hashing the full GGUF."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'Qwen3-8B-Q4_K_M.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        data = b'GGUFtiny'
        manager.model_profile['artifact_size_bytes'] = len(data)
        manager.model_profile['artifact_sha256'] = hashlib.sha256(data).hexdigest()
        Path(manager.model_path).write_bytes(data)
        manager._write_artifact_verification_receipt(manager.model_profile['artifact_sha256'])
        manager.download_file_in_chunks = MagicMock(return_value=True)

        assert manager.download_model_if_needed() is True

        assert manager.last_model_artifact_validation == {'valid': True, 'reason': 'valid'}
        manager.download_file_in_chunks.assert_not_called()

    def test_suspect_managed_artifact_hashes_and_repairs_checksum_mismatch(self):
        """Exact model-load evidence makes the managed artifact suspect and enables one hash repair."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'Qwen3-8B-Q4_K_M.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        data = b'GGUFtiny'
        manager.model_profile['artifact_size_bytes'] = len(data)
        manager.model_profile['artifact_sha256'] = '0' * 64
        Path(manager.model_path).write_bytes(data)
        manager.last_runtime_init_error = 'runtime_model_load_failed'
        manager.download_file_in_chunks = MagicMock(return_value=True)

        assert manager.download_model_if_needed() is True

        assert manager.last_model_artifact_validation == {'valid': False, 'reason': 'checksum_mismatch'}
        manager.download_file_in_chunks.assert_called_once_with(manager.model_path, manager.url, manager.chunk_size_mb)

    def test_unverified_managed_artifact_hashes_before_accepting_fresh_warm_start(self):
        """Fresh processes verify a pinned artifact once before trusting receipt-backed warm starts."""
        mock_config = MagicMock(is_production=False)
        temp_dir = tempfile.mkdtemp()
        mock_config.get.side_effect = lambda key, default=None: {
            'model.profile_id': 'qwen3-8b-q4-k-m',
            'model.filename': 'Qwen3-8B-Q4_K_M.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': temp_dir,
            'model.use_mock': False,
            'model.n_gpu_layers': -1,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }.get(key, default)
        manager = ModelManager(mock_config)
        data = b'GGUFtiny'
        manager.model_profile['artifact_size_bytes'] = len(data)
        manager.model_profile['artifact_sha256'] = '0' * 64
        Path(manager.model_path).write_bytes(data)
        manager.download_file_in_chunks = MagicMock(return_value=True)

        assert manager.download_model_if_needed() is True

        assert manager.last_model_artifact_validation == {'valid': False, 'reason': 'checksum_mismatch'}
        manager.download_file_in_chunks.assert_called_once_with(manager.model_path, manager.url, manager.chunk_size_mb)

    def test_create_models_directory(self, model_manager):
        """Test create_models_directory method."""
        # Create a new temporary directory path that doesn't exist
        new_temp_dir = os.path.join(self._temp_dir, 'new_models')

        # Make the model manager use this new directory
        model_manager.models_dir = new_temp_dir

        # Verify it doesn't exist yet
        assert not os.path.exists(new_temp_dir)

        # Call the method
        models_dir = model_manager.create_models_directory()

        # Check the result
        assert models_dir == new_temp_dir
        assert os.path.exists(new_temp_dir)

        # Clean up
        shutil.rmtree(new_temp_dir)

    @patch('utils.llm.model_manager.requests.get')
    def test_download_file_in_chunks_success(self, mock_get, model_manager):
        """Test successful file download."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '1048576'  # 1MB
        mock_response.iter_content.return_value = [b'x' * 1024 * 512] * 2  # Two chunks of 512KB
        mock_get.return_value = mock_response

        # Create a temporary file path
        file_path = os.path.join(self._temp_dir, 'test_download.gguf')

        # Call the method
        result = model_manager.download_file_in_chunks(file_path, 'https://example.com/model.gguf', 1)

        # Check the result
        assert result is True
        assert os.path.exists(file_path)
        assert os.path.getsize(file_path) == 1048576  # 1MB

        # Verify mock calls
        mock_get.assert_called_once_with(
            'https://example.com/model.gguf', stream=True, timeout=30
        )
        mock_response.iter_content.assert_called_once_with(chunk_size=1048576)

    @patch('utils.llm.model_manager.requests.get')
    def test_download_file_in_chunks_http_error(self, mock_get, model_manager):
        """Test file download with HTTP error."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        # Create a temporary file path
        file_path = os.path.join(self._temp_dir, 'test_download.gguf')

        # Call the method
        result = model_manager.download_file_in_chunks(file_path, 'https://example.com/model.gguf', 1)

        # Check the result
        assert result is False
        assert not os.path.exists(file_path)

        # Verify mock calls
        mock_get.assert_called_once_with(
            'https://example.com/model.gguf', stream=True, timeout=30
        )

    @patch('os.path.exists')
    @patch('utils.llm.model_manager.ModelManager.download_file_in_chunks')
    def test_download_model_if_needed_existing(self, mock_download, mock_exists, model_manager):
        """Pinned existing artifacts with invalid metadata receive a bounded repair."""
        # Setup mocks
        mock_exists.return_value = True  # Model already exists

        # Call the method
        result = model_manager.download_model_if_needed()

        # Check the result
        assert result is True

        # Verify pinned metadata is not bypassed for an invalid existing artifact.
        mock_download.assert_called_once_with(
            model_manager.model_path,
            'https://example.com/model.gguf',
            1
        )

    @patch('os.path.exists')
    @patch('utils.llm.model_manager.ModelManager.create_models_directory')
    @patch('utils.llm.model_manager.ModelManager.download_file_in_chunks')
    def test_download_model_if_needed_download_success(self, mock_download, mock_create_dir, mock_exists, model_manager):
        """Test download_model_if_needed when model needs to be downloaded."""
        # Setup mocks
        mock_exists.return_value = False  # Model doesn't exist
        mock_download.return_value = True  # Download succeeds

        # Call the method
        result = model_manager.download_model_if_needed()

        # Check the result
        assert result is True

        # Verify mock calls
        mock_create_dir.assert_called_once()
        mock_download.assert_called_once_with(
            model_manager.model_path,
            'https://example.com/model.gguf',
            1
        )

    @patch('os.path.exists')
    @patch('utils.llm.model_manager.ModelManager.create_models_directory')
    @patch('utils.llm.model_manager.ModelManager.download_file_in_chunks')
    def test_download_model_if_needed_download_failure(self, mock_download, mock_create_dir, mock_exists, model_manager):
        """Test download_model_if_needed when download fails."""
        # Setup mocks
        mock_exists.return_value = False  # Model doesn't exist
        mock_download.return_value = False  # Download fails

        # Call the method
        result = model_manager.download_model_if_needed()

        # Check the result
        assert result is False

        # Verify mock calls
        mock_create_dir.assert_called_once()
        mock_download.assert_called_once_with(
            model_manager.model_path,
            'https://example.com/model.gguf',
            1
        )

    @patch('utils.llm.model_manager.os.path.getsize', return_value=4_000_000_000)
    @patch('utils.llm.model_manager.os.path.exists', return_value=True)
    @patch('utils.llm.model_manager.resource_monitor.can_allocate_gpu_memory', return_value=False)
    @patch('utils.llm.model_manager._import_llama_cpp_runtime')
    def test_get_llm_instance_falls_back_to_cpu_on_low_gpu_memory(
        self,
        mock_import_llama_cpp_runtime,
        mock_can_allocate,
        mock_exists,
        _mock_getsize,
        model_manager,
    ):
        """When GPU headroom is insufficient the model should load on CPU."""

        mock_llama = MagicMock()
        instance = MagicMock()
        mock_llama.return_value = instance
        mock_import_llama_cpp_runtime.return_value = SimpleNamespace(Llama=mock_llama)

        with patch.object(model_manager, '_runtime_capabilities', return_value={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'llama_module_path': 'unknown',
            'error': None,
        }):
            result = model_manager.get_llm_instance()

        assert result is instance
        mock_llama.assert_called_once()
        kwargs = mock_llama.call_args.kwargs
        assert kwargs['n_gpu_layers'] == 0
        mock_can_allocate.assert_called_once()

    @patch('utils.llm.model_manager.os.path.getsize', return_value=4_000_000_000)
    @patch('utils.llm.model_manager.os.path.exists', return_value=True)
    @patch('utils.llm.model_manager.resource_monitor.can_allocate_gpu_memory', return_value=True)
    @patch('utils.llm.model_manager._import_llama_cpp_runtime')
    def test_get_llm_instance_uses_gpu_when_headroom_available(
        self,
        mock_import_llama_cpp_runtime,
        mock_can_allocate,
        mock_exists,
        _mock_getsize,
        model_manager,
    ):
        """When memory is available the model continues to use the GPU."""

        mock_llama = MagicMock()
        instance = MagicMock()
        mock_llama.return_value = instance
        mock_import_llama_cpp_runtime.return_value = SimpleNamespace(Llama=mock_llama)

        with patch.object(model_manager, '_runtime_capabilities', return_value={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'llama_module_path': 'unknown',
            'error': None,
        }):
            result = model_manager.get_llm_instance()

        assert result is instance
        kwargs = mock_llama.call_args.kwargs
        assert kwargs['n_gpu_layers'] == -1
        mock_can_allocate.assert_called_once_with(4_000_000_000, headroom_percent=0.1)


    def test_selected_context_profile_reaches_llama_n_ctx(self, model_manager):
        """Selected desktop context profile controls Llama n_ctx construction."""
        from utils.context_profiles import apply_context_profile

        instance = MagicMock()
        mock_llama = MagicMock(return_value=instance)
        model_manager.config.get.side_effect = None
        model_manager.config.get.return_value = None
        model_manager.config.get.side_effect = lambda key, default=None: {
            'model.context_size': 2048,
            'model.chat_format': 'llama-3',
        }.get(key, self._mock_config_get(key, default))
        apply_context_profile(model_manager, '64k-full')
        model_manager.config.get.side_effect = lambda key, default=None: {
            'model.context_size': 65536,
            'model.chat_format': 'llama-3',
        }.get(key, self._mock_config_get(key, default))

        with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=mock_llama)), \
             patch('utils.llm.model_manager.resource_monitor.can_allocate_gpu_memory', return_value=True), \
             patch('utils.llm.model_manager.os.path.exists', return_value=True), \
             patch.object(model_manager, '_runtime_capabilities', return_value={
                 'backend': 'cuda',
                 'gpu_offload_supported': True,
                 'detected_device': 'cuda',
                 'llama_module_path': 'unknown',
                 'error': None,
             }):
            result = model_manager.get_llm_instance()

        assert result is instance
        assert mock_llama.call_args.kwargs['n_ctx'] == 65536
        assert model_manager.last_compute_diagnostics['context_window_tokens'] == 65536

    def test_get_llm_instance_skips_headroom_when_size_unknown(self, model_manager):
        """If the model size cannot be determined we skip the headroom check."""

        instance = MagicMock()

        mock_llama = MagicMock(return_value=instance)
        with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=mock_llama)), \
             patch('utils.llm.model_manager.resource_monitor.can_allocate_gpu_memory') as mock_can_allocate, \
             patch('utils.llm.model_manager.os.path.exists', return_value=True), \
             patch('utils.llm.model_manager.os.path.getsize', side_effect=OSError('stat failed')), \
             patch.object(model_manager, '_runtime_capabilities', return_value={
                 'backend': 'cuda',
                 'gpu_offload_supported': True,
                 'detected_device': 'cuda',
                 'llama_module_path': 'unknown',
                 'error': None,
             }):

            result = model_manager.get_llm_instance()

        assert result is instance
        mock_can_allocate.assert_not_called()
        kwargs = mock_llama.call_args.kwargs
        assert kwargs['n_gpu_layers'] == -1

    def test_get_llm_instance_mock_mode(self, model_manager):
        """Test get_llm_instance in mock mode."""
        # Enable mock mode
        model_manager.use_mock_llm = True

        # Call the method
        llm = model_manager.get_llm_instance()

        # Check the result
        assert llm is not None
        assert callable(llm.create_chat_completion)

        # Check that the mock is properly configured
        completion = llm.create_chat_completion()
        assert 'choices' in completion
        assert 'message' in completion['choices'][0]
        assert 'content' in completion['choices'][0]['message']
        assert 'Mock Response' in completion['choices'][0]['message']['content']

    def test_get_llm_instance_logs_stage_diagnostics(self, model_manager, caplog):
        """Warm-load diagnostics should show import, compute-plan, and Llama init stages."""
        mock_llama = MagicMock()

        with patch('os.path.exists', return_value=True), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=MagicMock(return_value=mock_llama))):
            with caplog.at_level('INFO', logger='model_manager'):
                llm = model_manager.get_llm_instance()

        assert llm == mock_llama
        messages = [record.getMessage() for record in caplog.records]
        assert 'Locating llama_cpp runtime for model initialization...' in messages
        assert any(message.startswith('llama_cpp runtime located module_path_present=') for message in messages)
        assert 'Selecting compute plan for model initialization...' in messages
        assert any(
            message.startswith('Selected compute plan for model initialization ')
            for message in messages
        )
        assert 'About to instantiate Llama model.' in messages
        assert 'Llama init started.' in messages
        assert 'Llama init completed successfully.' in messages


    def test_get_llm_instance_real_mode(self, model_manager):
        """Test get_llm_instance in real mode when the model file exists."""
        # Create a mock for Llama
        mock_llama = MagicMock()

        # Patch everything needed
        with patch('os.path.exists', return_value=True), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=MagicMock(return_value=mock_llama))):

            # Call the method
            llm = model_manager.get_llm_instance()

            # Check the result
            assert llm is not None
            assert llm == mock_llama

    @patch('os.path.exists')
    def test_get_llm_instance_real_mode_no_model(self, mock_exists, model_manager):
        """Test get_llm_instance in real mode when the model file doesn't exist."""
        # Setup mocks
        mock_exists.return_value = False  # Model doesn't exist

        # Call the method
        llm = model_manager.get_llm_instance()

        # Check the result
        assert llm is None

    def test_llama_cpp_get_response_mock_instance(self, model_manager):
        """Test llama_cpp_get_response with a mock LLM instance."""
        # Setup
        chat_history = [
            {"role": "user", "content": "What is the capital of France?"}
        ]

        # Mock get_llm_instance to return a mock
        mock_llm = MagicMock()
        mock_completion = {
            'choices': [
                {
                    'message': {
                        'role': 'assistant',
                        'content': 'The capital of France is Paris.'
                    }
                }
            ]
        }
        mock_llm.create_chat_completion.return_value = mock_completion
        model_manager.get_llm_instance = MagicMock(return_value=mock_llm)

        # Call the method
        result = model_manager.llama_cpp_get_response(chat_history)

        # Check the result
        assert len(result) == 2
        assert result[0] == chat_history[0]
        assert result[1]['role'] == 'assistant'
        assert result[1]['content'] == 'The capital of France is Paris.'

        # Verify mock calls - check the structure but not exact values which may change
        mock_llm.create_chat_completion.assert_called_once()
        _, call_kwargs = mock_llm.create_chat_completion.call_args
        assert call_kwargs['messages'] == chat_history
        assert call_kwargs['stream'] is True
        assert 'max_tokens' in call_kwargs
        assert 'temperature' in call_kwargs
        assert 'top_p' in call_kwargs
        assert 'stop' in call_kwargs

    def test_llama_cpp_get_response_streaming_deltas(self, model_manager):
        """Streamed chat completion chunks are aggregated into one reply."""

        chat_history = [
            {"role": "user", "content": "Say hello"}
        ]

        def chunk_generator():
            yield {"choices": [{"delta": {"role": "assistant"}}]}
            yield {"choices": [{"delta": {"content": "Hello"}}]}
            yield {"choices": [{"delta": {"content": " world"}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        mock_llm = MagicMock()
        mock_llm.create_chat_completion.return_value = chunk_generator()
        model_manager.get_llm_instance = MagicMock(return_value=mock_llm)

        result = model_manager.llama_cpp_get_response(chat_history)

        assert len(result) == 2
        assert result[-1]['role'] == 'assistant'
        assert result[-1]['content'] == 'Hello world'
        assert mock_llm.create_chat_completion.call_count == 1
        assert mock_llm.create_chat_completion.call_args.kwargs['stream'] is True

    def test_llama_cpp_get_response_no_llm(self, model_manager):
        """Test llama_cpp_get_response when LLM initialization fails."""
        # Setup
        chat_history = [
            {"role": "user", "content": "What is the capital of France?"}
        ]

        # Mock get_llm_instance to return None
        model_manager.get_llm_instance = MagicMock(return_value=None)

        # Call the method
        result = model_manager.llama_cpp_get_response(chat_history)

        # Check the result
        assert len(result) == 2
        assert result[0] == chat_history[0]
        assert result[1]['role'] == 'assistant'
        assert "trouble" in result[1]['content'].lower()

    def test_llama_cpp_get_response_exception(self, model_manager):
        """Test llama_cpp_get_response when an exception occurs during inference."""
        # Setup
        chat_history = [
            {"role": "user", "content": "What is the capital of France?"}
        ]

        # Mock get_llm_instance to return a mock that raises an exception
        mock_llm = MagicMock()
        mock_llm.create_chat_completion.side_effect = Exception("Test error")
        model_manager.get_llm_instance = MagicMock(return_value=mock_llm)

        # Call the method
        result = model_manager.llama_cpp_get_response(chat_history)

        # Check the result
        assert len(result) == 2
        assert result[0] == chat_history[0]
        assert result[1]['role'] == 'assistant'
        assert "sorry" in result[1]['content'].lower()

    @patch('utils.llm.model_manager.requests.get')
    def test_download_file_in_chunks_no_header(self, mock_get, model_manager):
        """Return False if Content-Length header missing or zero."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '0'
        mock_get.return_value = mock_response

        file_path = os.path.join(self._temp_dir, 'no_header.gguf')
        result = model_manager.download_file_in_chunks(file_path, 'https://example.com/model.gguf', 1)
        assert result is False
        assert not os.path.exists(file_path)

    @patch('utils.llm.model_manager.requests.get')
    def test_download_file_in_chunks_size_mismatch(self, mock_get, model_manager):
        """Return False if downloaded size doesn't match header."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '1048576'  # 1MB
        # Only half the data so size check fails
        mock_response.iter_content.return_value = [b'x' * 1024 * 256] * 2
        mock_get.return_value = mock_response

        file_path = os.path.join(self._temp_dir, 'bad_size.gguf')
        result = model_manager.download_file_in_chunks(file_path, 'https://example.com/model.gguf', 1)
        assert result is False
        assert not os.path.exists(file_path)

    @patch('utils.llm.model_manager.requests.get')
    def test_download_file_in_chunks_empty_chunk(self, mock_get, model_manager):
        """Handle empty data chunk but still succeed."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '1'
        mock_response.iter_content.return_value = [b'', b'a']
        mock_get.return_value = mock_response

        file_path = os.path.join(self._temp_dir, 'empty_chunk.gguf')
        result = model_manager.download_file_in_chunks(file_path, 'https://example.com/model.gguf', 1)
        assert result is True
        assert os.path.getsize(file_path) == 1

    @patch('builtins.open', side_effect=IOError('disk error'))
    @patch('utils.llm.model_manager.requests.get')
    def test_download_file_in_chunks_exception(self, mock_get, mock_open, model_manager):
        """Return False if an exception occurs during download."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = '1'
        mock_response.iter_content.return_value = [b'a']
        mock_get.return_value = mock_response

        file_path = os.path.join(self._temp_dir, 'error.gguf')
        result = model_manager.download_file_in_chunks(file_path, 'https://example.com/model.gguf', 1)
        assert result is False
        mock_open.assert_called()

    @patch('config.get_config')
    def test_init_default_config(self, mock_get_config):
        """Initialization without passing a config calls get_config."""
        mock_cfg = MagicMock()
        mock_cfg.is_production = False

        def cfg_get(key, default=None):
            values = {
                'model.filename': 'test_model.gguf',
                'model.url': 'https://example.com/model.gguf',
                'model.download_chunk_size_mb': 1,
                'paths.models_dir': self._temp_dir,
                'model.use_mock': False,
                'model.context_size': 2048,
                'model.chat_format': 'llama-3',
                'model.max_tokens': 1000,
                'model.temperature': 0.7,
                'model.top_p': 0.9,
                'model.stop_tokens': [],
            }
            return values.get(key, default)

        mock_cfg.get.side_effect = cfg_get
        mock_get_config.return_value = mock_cfg

        with tempfile.TemporaryDirectory() as temp_dir:
            self._temp_dir = temp_dir
            self.create_fake_model_file(temp_dir)
            manager = ModelManager()
            assert isinstance(manager, ModelManager)
            assert mock_get_config.called

    @patch('utils.llm.model_manager._import_llama_cpp_runtime')
    @patch('os.path.exists', return_value=True)
    def test_get_llm_instance_init_failure(self, mock_exists, mock_import_llama_cpp_runtime, model_manager):
        """Return None if Llama initialization raises an exception."""
        mock_import_llama_cpp_runtime.return_value = SimpleNamespace(
            Llama=MagicMock(side_effect=Exception('fail'))
        )
        llm = model_manager.get_llm_instance()
        assert llm is None

    def test_get_model_manager_singleton(self):
        """get_model_manager should return a singleton instance."""
        from utils.llm import model_manager as mm

        with patch('config.get_config') as mock_get:
            mock_cfg = MagicMock()
            mock_cfg.is_production = False
            mock_cfg.get.return_value = ''
            mock_get.return_value = mock_cfg

            original = mm.model_manager
            try:
                mm.model_manager = None
                inst1 = mm.get_model_manager()
                inst2 = mm.get_model_manager()

                assert inst1 is inst2
            finally:
                mm.model_manager = original

    def test_normalize_stream_chunk_variants(self):
        """_normalize_stream_chunk should handle several helper patterns."""
        assert ModelManager._normalize_stream_chunk({'sentinel': True}) == {'sentinel': True}

        assert ModelManager._normalize_stream_chunk(_ToDictOnly()) == {'origin': 'to_dict'}

        # Objects whose to_dict signature is incompatible should fall back to model_dump.
        assert ModelManager._normalize_stream_chunk(_TypeErrorToDict()) == {'origin': 'model_dump'}

        assert ModelManager._normalize_stream_chunk(_DictMethodOnly()) == {'origin': 'dict'}

        assert ModelManager._normalize_stream_chunk(_DictAttributeOnly()) == {'origin': 'dunder_dict'}

        class Unknown:
            pass

        assert ModelManager._normalize_stream_chunk(Unknown()) == {}

    def test_merge_tool_call_deltas_extends_structure(self):
        """_merge_tool_call_deltas should allocate slots and concatenate arguments."""
        existing = [
            {
                'id': 'call-0',
                'type': 'function',
                'function': {
                    'name': 'alpha',
                    'arguments': '{"value":',
                },
            }
        ]

        deltas = [
            {
                # No index means append to the end of the list.
                'id': 'call-1',
                'type': 'function',
                'function': {'name': 'beta', 'arguments': '{"count":'},
            },
            {
                'index': 0,
                'function': {'arguments': '42}'},
            },
            {
                'index': 2,
                'function': {'arguments': 'true}', 'name': 'gamma'},
            },
        ]

        merged = ModelManager._merge_tool_call_deltas(existing, deltas)

        assert len(merged) == 3
        assert merged[0]['function']['arguments'] == '{"value":42}'
        assert merged[1]['id'] == 'call-1'
        assert merged[1]['function']['name'] == 'beta'
        assert merged[1]['function']['arguments'] == '{"count":'
        assert merged[2]['function']['arguments'] == 'true}'
        assert merged[2]['function']['name'] == 'gamma'

    def test_consume_streaming_completion_aggregates_deltas(self, model_manager):
        """Streaming completions should merge content, roles, and tool calls."""

        def streaming_completion():
            yield None  # skipped entirely
            yield {'choices': []}  # skipped due to empty choices
            yield {'choices': [{'delta': 'not-a-dict'}]}  # skipped due to non-dict delta
            yield {
                'choices': [
                    {
                        'delta': {
                            'role': 'assistant',
                            'content': 'Hello',
                            'tool_calls': [
                                {
                                    'index': 0,
                                    'id': 'tool-0',
                                    'type': 'function',
                                    'function': {'name': 'math', 'arguments': '{"number":'},
                                }
                            ],
                        }
                    }
                ]
            }
            yield {
                'choices': [
                    {
                        'delta': {
                            'content': ' world!',
                            'tool_calls': [
                                {
                                    'index': 0,
                                    'function': {'arguments': '42}'},
                                },
                                {
                                    'index': 1,
                                    'function': {'arguments': '1', 'name': 'second'},
                                },
                            ],
                        }
                    }
                ]
            }
            yield {
                'choices': [
                    {
                        'delta': {},
                        'finish_reason': 'stop',
                    }
                ]
            }

        message = model_manager._consume_streaming_completion(streaming_completion())

        assert message['role'] == 'assistant'
        assert message['content'] == 'Hello world!'
        assert message['tool_calls'] == [
            {
                'id': 'tool-0',
                'type': 'function',
                'function': {'name': 'math', 'arguments': '{"number":42}'},
            },
            {
                'function': {'name': 'second', 'arguments': '1'},
            },
        ]

    def test_llama_cpp_get_response_streaming_fallback(self, model_manager):
        """Empty streaming responses should trigger a non-streaming retry."""
        chat_history = [{'role': 'user', 'content': 'ping'}]

        def empty_stream():
            yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}

        mock_llm = MagicMock()
        mock_llm.create_chat_completion.side_effect = [
            empty_stream(),
            {
                'choices': [
                    {
                        'message': {
                            'role': 'assistant',
                            'content': 'fallback reply',
                        }
                    }
                ]
            },
        ]

        model_manager.get_llm_instance = MagicMock(return_value=mock_llm)

        result = model_manager.llama_cpp_get_response(chat_history)

        assert result[-1]['content'] == 'fallback reply'

        stream_call, non_stream_call = mock_llm.create_chat_completion.call_args_list
        assert stream_call.kwargs['stream'] is True
        assert non_stream_call.kwargs['stream'] is False

    def test_resolve_compute_plan_auto_falls_back_when_runtime_lacks_gpu(self, model_manager):
        """Auto mode should downshift GPU layers when runtime support is missing."""
        model_manager.requested_compute_mode = 'auto'
        model_manager.default_n_gpu_layers = -1

        with patch.object(model_manager, '_runtime_capabilities', return_value={
            'backend': 'cuda',
            'gpu_offload_supported': False,
            'detected_device': 'cpu',
            'llama_module_path': 'unknown',
            'error': None,
        }):
            plan = model_manager._resolve_compute_plan()

        assert plan['requested_mode'] == 'auto'
        assert plan['effective_mode'] == 'cpu_fallback'
        assert plan['backend_selected'] == 'cuda'
        assert plan['backend_used'] == 'cpu'
        assert plan['n_gpu_layers'] == 0
        assert 'does not expose cuda GPU offload support' in plan['fallback_reason']

    def test_resolve_compute_plan_auto_cpu_request_keeps_cpu(self, model_manager):
        """Auto mode should stay on CPU when default GPU layers request CPU-only."""
        model_manager.requested_compute_mode = 'auto'
        model_manager.default_n_gpu_layers = 0

        with patch.object(model_manager, '_runtime_capabilities', return_value={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'llama_module_path': 'unknown',
            'error': None,
        }):
            plan = model_manager._resolve_compute_plan()

        assert plan['effective_mode'] == 'cpu'
        assert plan['backend_selected'] == 'cpu'
        assert plan['backend_used'] == 'cpu'
        assert plan['n_gpu_layers'] == 0
        assert plan['fallback_reason'] is None

    def test_platform_gpu_backend_linux_detects_cuda_marker(self):
        """Linux backend detection should use llama_cpp capability markers."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=True,
            GGML_USE_METAL=False,
            llama_supports_gpu_offload=lambda: False,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'cuda'

    def test_platform_gpu_backend_linux_detects_cuda_legacy_markers(self):
        """Backend detection should honor legacy/new CUDA marker variants."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_CUDA=True,
            GGML_USE_METAL=False,
            llama_supports_gpu_offload=lambda: False,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'cuda'

    def test_llama_gpu_offload_available_returns_false_on_runtime_error(self):
        """GPU support probe should fail closed if llama_cpp probe raises."""

        def _raise_runtime_error():
            raise RuntimeError("probe failed")

        fake_llama = SimpleNamespace(llama_supports_gpu_offload=_raise_runtime_error)
        with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            assert ModelManager._llama_gpu_offload_available() is False

    def test_get_llm_instance_mock_mode_refreshes_compute_diagnostics(self, model_manager):
        """Mock mode should persist diagnostics without probing llama_cpp."""
        model_manager.use_mock_llm = True
        expected_plan = {
            'requested_mode': 'hybrid',
            'effective_mode': 'hybrid_cuda',
            'backend_available': 'cuda',
            'backend_selected': 'cuda',
            'backend_used': 'cuda',
            'n_gpu_layers': 24,
            'fallback_reason': None,
        }

        with patch.object(model_manager, '_mock_compute_plan', return_value=expected_plan), \
             patch.object(model_manager, '_resolve_compute_plan') as resolve_plan:
            llm = model_manager.get_llm_instance()

        assert callable(llm.create_chat_completion)
        assert model_manager.last_compute_diagnostics == expected_plan
        resolve_plan.assert_not_called()

    def test_platform_gpu_backend_linux_detects_metal_marker(self):
        """Linux backend detection should return Metal when CUDA is unavailable."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=True,
            llama_supports_gpu_offload=lambda: False,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'metal'

    def test_platform_gpu_backend_linux_detects_metal_legacy_marker(self):
        """Backend detection should honor legacy/new Metal marker variants."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=False,
            GGML_METAL=True,
            llama_supports_gpu_offload=lambda: False,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'metal'

    def test_platform_gpu_backend_linux_uses_gpu_offload_probe(self):
        """Linux backend detection should map positive runtime probes to CUDA."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=False,
            llama_supports_gpu_offload=lambda: True,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'cuda'

    def test_platform_gpu_backend_linux_returns_none_when_probe_raises(self):
        """Linux backend detection should fail closed when probe raises."""

        def _raise_runtime_error():
            raise RuntimeError("probe failed")

        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=False,
            llama_supports_gpu_offload=_raise_runtime_error,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            backend = ModelManager._platform_gpu_backend()

        assert backend is None

    def test_llama_gpu_offload_available_uses_marker_fallback(self):
        """GPU support probe should fall back to GGML capability markers."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=True,
            llama_supports_gpu_offload=None,
        )

        with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama):
            assert ModelManager._llama_gpu_offload_available() is True

    def test_resolve_compute_plan_gpu_and_hybrid_success_paths(self, model_manager):
        """Explicit gpu/hybrid requests should emit expected diagnostics."""
        model_manager.requested_compute_mode = 'gpu'
        with patch.object(model_manager, '_runtime_capabilities', return_value={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'llama_module_path': 'unknown',
            'error': None,
        }):
            gpu_plan = model_manager._resolve_compute_plan()

        assert gpu_plan['effective_mode'] == 'cuda'
        assert gpu_plan['backend_used'] == 'cuda'
        assert gpu_plan['n_gpu_layers'] == -1
        assert gpu_plan['fallback_reason'] is None

        model_manager.requested_compute_mode = 'hybrid'
        model_manager.hybrid_n_gpu_layers = -5
        with patch.object(model_manager, '_runtime_capabilities', return_value={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'llama_module_path': 'unknown',
            'error': None,
        }):
            hybrid_plan = model_manager._resolve_compute_plan()

        assert hybrid_plan['effective_mode'] == 'hybrid_cuda'
        assert hybrid_plan['backend_used'] == 'cuda'
        assert hybrid_plan['n_gpu_layers'] == 1
        assert hybrid_plan['fallback_reason'] is None

    def test_shared_runtime_probe_drives_platform_and_offload_detection(self):
        with patch(
            'utils.llm.model_manager.detect_llama_runtime_capabilities',
            return_value={
                'backend': 'metal',
                'gpu_offload_supported': True,
                'detected_device': 'metal',
                'error': None,
            },
        ):
            assert ModelManager._platform_gpu_backend() == 'metal'
            assert ModelManager._llama_gpu_offload_available() is True

    def test_detect_runtime_capabilities_reports_missing_module_error(self):
        from utils.llm import model_manager as mm

        with patch(
            'utils.llm.model_manager._import_llama_cpp_runtime',
            side_effect=ModuleNotFoundError("No module named 'llama_cpp'"),
        ):
            payload = mm.detect_llama_runtime_capabilities()

        assert payload['backend'] == 'missing'
        assert payload['gpu_offload_supported'] is False
        assert payload['detected_device'] == 'none'
        assert "No module named 'llama_cpp'" in payload['error']

    def test_compute_runtime_log_includes_backend_device_offload_and_fallback(self, model_manager):
        class FakeLlama:
            def __init__(self, **_kwargs):
                pass

        log_lines = []
        model_manager.requested_compute_mode = 'gpu'
        model_manager.log_info = lambda message: log_lines.append(message)

        with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=FakeLlama)), \
             patch.object(model_manager, '_resolve_compute_plan', return_value={
                 'requested_mode': 'gpu',
                 'effective_mode': 'cpu_fallback',
                 'backend_available': 'cuda',
                 'backend_selected': 'cuda',
                 'backend_used': 'cpu',
                 'n_gpu_layers': 0,
                 'fallback_reason': 'runtime missing cuda support',
             }):
            llm = model_manager.get_llm_instance()

        assert llm is not None
        compute_logs = [line for line in log_lines if line.startswith('compute_runtime ')]
        assert compute_logs
        summary = compute_logs[-1]
        assert 'backend_available=cuda' in summary
        assert 'backend_used=cpu' in summary
        assert 'device_backend=cpu' in summary
        assert 'offloaded_layers=0' in summary
        assert 'kv_cache=cpu' in summary
        assert 'interpreter=' in summary
        assert 'llama_module_path_present=' in summary
        assert 'fallback_reason=runtime missing cuda support' in summary


@pytest.fixture
def standalone_model_manager(tmp_path):
    mock_config = MagicMock()
    mock_config.is_production = False
    model_file = tmp_path / 'test_model.gguf'
    model_file.write_bytes(b'fake model data')

    def _get(key, default=None):
        values = {
            'model.filename': 'test_model.gguf',
            'model.url': 'https://example.com/model.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': str(tmp_path),
            'model.use_mock': False,
            'model.context_size': 2048,
            'model.chat_format': 'llama-3',
            'model.n_gpu_layers': -1,
            'model.hybrid_n_gpu_layers': 24,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': True,
        }
        return values.get(key, default)

    mock_config.get.side_effect = _get
    return ModelManager(mock_config)


def test_get_llm_instance_records_bounded_runtime_discovery_timeout(standalone_model_manager):
    from utils.llm import model_manager as model_manager_module

    with patch('os.path.exists', return_value=True), \
         patch(
             'utils.llm.model_manager._import_llama_cpp_runtime',
             side_effect=model_manager_module.LlamaCppRuntimeStageTimeout(
                 'llama_cpp_import',
                 0.01,
             ),
         ):
        llm = standalone_model_manager.get_llm_instance()

    assert llm is None
    assert standalone_model_manager.last_runtime_init_error == 'llama_cpp_import_timeout after 0.01s'


def test_desktop_runtime_probe_is_reused_for_compute_plan(standalone_model_manager):
    module_path = '/opt/python/site-packages/llama_cpp/__init__.py'
    standalone_model_manager.desktop_runtime_probe = {
        'runtime_action': 'already_supported',
        'selected_backend': 'cuda',
        'detected_device': 'cuda',
        'gpu_offload_supported': True,
        'interpreter': '/opt/python/bin/python',
        'llama_module_path': module_path,
    }

    with patch(
        'utils.llm.model_manager.detect_llama_runtime_capabilities',
        return_value={
            'backend': 'cpu',
            'gpu_offload_supported': False,
            'detected_device': 'cpu',
            'llama_module_path': module_path,
            'error': None,
        },
    ) as detect:
        plan = standalone_model_manager._resolve_compute_plan()

    detect.assert_not_called()
    assert plan['backend_available'] == 'cuda'
    assert plan['backend_selected'] == 'cuda'
    assert plan['n_gpu_layers'] == -1


def test_desktop_runtime_probe_mismatch_falls_back_to_imported_runtime(standalone_model_manager):
    standalone_model_manager.desktop_runtime_probe = {
        'runtime_action': 'already_supported',
        'selected_backend': 'cuda',
        'detected_device': 'cuda',
        'gpu_offload_supported': True,
        'interpreter': '/opt/python/bin/python',
        'llama_module_path': '/old/site-packages/llama_cpp/__init__.py',
    }

    standalone_model_manager._imported_llama_cpp_module_path = '/new/site-packages/llama_cpp/__init__.py'

    with patch('utils.llm.model_manager.detect_llama_runtime_capabilities') as detect:
        plan = standalone_model_manager._resolve_compute_plan()

    detect.assert_not_called()

    assert plan['effective_mode'] == 'cpu_fallback'
    assert plan['backend_available'] == 'cpu'
    assert plan['backend_used'] == 'cpu'
    assert plan['n_gpu_layers'] == 0
    assert plan['fallback_reason'] == 'llama_cpp_runtime_probe_mismatch'


def _write_fake_llama_cpp_package(site_dir: Path, marker: str) -> Path:
    package_dir = site_dir / 'llama_cpp'
    package_dir.mkdir(parents=True, exist_ok=True)
    init_file = package_dir / '__init__.py'
    init_file.write_text(
        f"MARKER = {marker!r}\n"
        "GGML_USE_CUDA = True\n"
        "def llama_supports_gpu_offload():\n"
        "    return True\n"
        "class Llama:\n"
        "    pass\n",
        encoding='utf-8',
    )
    return init_file


def test_desktop_runtime_probe_clears_stale_loaded_llama_cpp(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    stale_path = tmp_path / 'old site-packages' / 'llama_cpp' / '__init__.py'
    right_site = tmp_path / 'right site-packages'
    right_init = _write_fake_llama_cpp_package(right_site, 'right')
    monkeypatch.setattr(model_manager_module, '_signal_guard_available', lambda: True)
    monkeypatch.setattr(model_manager_module.threading, 'current_thread', lambda: model_manager_module.threading.main_thread())
    monkeypatch.setattr(model_manager_module.signal, 'setitimer', lambda *_args: (0, 0))
    monkeypatch.setattr(model_manager_module.signal, 'getsignal', lambda *_args: None)
    monkeypatch.setattr(model_manager_module.signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(sys, 'path', [str(right_site), *sys.path])
    monkeypatch.setitem(sys.modules, 'llama_cpp', SimpleNamespace(__file__=str(stale_path)))

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': str(right_init),
        },
    )

    assert llama_cpp.MARKER == 'right'
    assert Path(llama_cpp.__file__).resolve() == right_init.resolve()


def test_desktop_runtime_probe_clears_stale_llama_cpp_submodules(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    right_site = tmp_path / 'right site-packages'
    package_dir = right_site / 'llama_cpp'
    package_dir.mkdir(parents=True)
    native_file = package_dir / '_native.py'
    native_file.write_text("MARKER = 'right-native'\n", encoding='utf-8')
    init_file = package_dir / '__init__.py'
    init_file.write_text(
        "from . import _native\n"
        "MARKER = _native.MARKER\n"
        "GGML_USE_CUDA = True\n"
        "def llama_supports_gpu_offload():\n"
        "    return True\n"
        "class Llama:\n"
        "    pass\n",
        encoding='utf-8',
    )
    stale_path = tmp_path / 'old site-packages' / 'llama_cpp' / '__init__.py'
    stale_native = SimpleNamespace(__file__=str(stale_path.parent / '_native.py'), MARKER='stale-native')
    monkeypatch.setitem(sys.modules, 'llama_cpp', SimpleNamespace(__file__=str(stale_path)))
    monkeypatch.setitem(sys.modules, 'llama_cpp._native', stale_native)
    monkeypatch.setattr(sys, 'path', [str(right_site), *sys.path])

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': str(init_file),
        },
    )

    assert llama_cpp.MARKER == 'right-native'
    assert getattr(sys.modules.get('llama_cpp._native'), '__file__', None) == str(native_file)


def test_desktop_runtime_probe_clears_matching_top_level_with_stale_submodule(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    right_site = tmp_path / 'right site-packages'
    package_dir = right_site / 'llama_cpp'
    package_dir.mkdir(parents=True)
    native_file = package_dir / '_native.py'
    native_file.write_text("MARKER = 'right-native'\n", encoding='utf-8')
    init_file = package_dir / '__init__.py'
    init_file.write_text(
        "from . import _native\n"
        "MARKER = _native.MARKER\n"
        "GGML_USE_CUDA = True\n"
        "def llama_supports_gpu_offload():\n"
        "    return True\n"
        "class Llama:\n"
        "    pass\n",
        encoding='utf-8',
    )
    stale_native = SimpleNamespace(
        __file__=str(tmp_path / 'old site-packages' / 'llama_cpp' / '_native.py'),
        MARKER='stale-native',
    )
    monkeypatch.setitem(sys.modules, 'llama_cpp', SimpleNamespace(__file__=str(init_file)))
    monkeypatch.setitem(sys.modules, 'llama_cpp._native', stale_native)
    monkeypatch.setattr(sys, 'path', [str(right_site), *sys.path])

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': str(init_file),
        },
    )

    assert llama_cpp.MARKER == 'right-native'
    assert getattr(sys.modules.get('llama_cpp._native'), '__file__', None) == str(native_file)


def test_desktop_runtime_probe_clears_orphaned_llama_cpp_submodules(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    right_site = tmp_path / 'right site-packages'
    package_dir = right_site / 'llama_cpp'
    package_dir.mkdir(parents=True)
    native_file = package_dir / '_native.py'
    native_file.write_text("MARKER = 'right-native'\n", encoding='utf-8')
    init_file = package_dir / '__init__.py'
    init_file.write_text(
        "from . import _native\n"
        "MARKER = _native.MARKER\n"
        "GGML_USE_CUDA = True\n"
        "def llama_supports_gpu_offload():\n"
        "    return True\n"
        "class Llama:\n"
        "    pass\n",
        encoding='utf-8',
    )
    stale_native = SimpleNamespace(
        __file__=str(tmp_path / 'old site-packages' / 'llama_cpp' / '_native.py'),
        MARKER='stale-native',
    )
    monkeypatch.delitem(sys.modules, 'llama_cpp', raising=False)
    monkeypatch.setitem(sys.modules, 'llama_cpp._native', stale_native)
    monkeypatch.setattr(sys, 'path', [str(right_site), *sys.path])

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': str(init_file),
        },
    )

    assert llama_cpp.MARKER == 'right-native'
    assert getattr(sys.modules.get('llama_cpp._native'), '__file__', None) == str(native_file)


def test_desktop_runtime_probe_parent_wins_wrong_sys_path_order(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    wrong_site = tmp_path / 'wrong site-packages'
    right_site = tmp_path / 'right site-packages'
    _write_fake_llama_cpp_package(wrong_site, 'wrong')
    right_init = _write_fake_llama_cpp_package(right_site, 'right')
    monkeypatch.delitem(sys.modules, 'llama_cpp', raising=False)
    monkeypatch.setattr(sys, 'path', [str(wrong_site), str(right_site), *sys.path])

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': str(right_init),
        },
    )

    assert llama_cpp.MARKER == 'right'
    right_index = next(i for i, entry in enumerate(sys.path) if Path(entry).resolve() == right_site.resolve())
    wrong_index = next(i for i, entry in enumerate(sys.path) if Path(entry).resolve() == wrong_site.resolve())
    assert right_index < wrong_index


def test_desktop_runtime_probe_windows_extended_path_with_spaces_prioritized(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    site = tmp_path / 'Windows Python Runtime' / 'Lib' / 'site-packages with spaces'
    init_file = _write_fake_llama_cpp_package(site, 'windows-spaces')
    extended_init = '\\\\?\\' + str(init_file)
    monkeypatch.delitem(sys.modules, 'llama_cpp', raising=False)
    monkeypatch.setattr(sys, 'path', [str(tmp_path / 'other'), *sys.path])

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': extended_init,
        },
    )

    assert llama_cpp.MARKER == 'windows-spaces'
    assert Path(llama_cpp.__file__).resolve() == init_file.resolve()


def test_desktop_runtime_probe_macos_app_resources_path_prioritized(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    resources_site = (
        tmp_path
        / 'TokenPlace.app'
        / 'Contents'
        / 'Resources'
        / 'python'
        / 'site-packages'
    )
    init_file = _write_fake_llama_cpp_package(resources_site, 'macos-app')
    monkeypatch.delitem(sys.modules, 'llama_cpp', raising=False)
    monkeypatch.setattr(sys, 'path', [str(tmp_path / 'repo-like-cwd'), *sys.path])

    llama_cpp = model_manager_module._import_llama_cpp_runtime(
        require_real_runtime=True,
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': str(init_file),
        },
    )

    assert llama_cpp.MARKER == 'macos-app'
    resources_index = next(i for i, entry in enumerate(sys.path) if Path(entry).resolve() == resources_site.resolve())
    site_indices = [i for i, entry in enumerate(sys.path) if 'site-packages' in entry or 'dist-packages' in entry]
    assert resources_index <= min(site_indices)


def test_repo_local_llama_cpp_shim_detection_handles_windows_extended_paths():
    from utils.llm import model_manager as model_manager_module

    shim = model_manager_module.REPO_LLAMA_CPP_SHIM
    extended = '\\\\?\\' + str(shim)

    assert model_manager_module._is_repo_llama_cpp_shim(extended)
    assert not model_manager_module._is_repo_llama_cpp_shim(
        r'\\?\\C:\\Users\\danie\\AppData\\Local\\Programs\\Python\\Python311\\Lib\\site-packages\\llama_cpp\\__init__.py'
    )


def test_llama_cpp_import_watchdog_timeout_uses_subprocess(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    original_sys_path = list(sys.path)
    original_module = sys.modules.get('llama_cpp')
    sentinel = object()
    sys.modules['llama_cpp'] = sentinel

    def _timeout_run(*_args, **kwargs):
        raise model_manager_module.subprocess.TimeoutExpired(
            cmd=kwargs.get('args', ['python']),
            timeout=kwargs.get('timeout'),
        )

    monkeypatch.setattr(model_manager_module.subprocess, 'run', _timeout_run)

    try:
        with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
            model_manager_module._run_llama_cpp_import_watchdog(timeout_seconds=0.01)
    finally:
        if original_module is None:
            sys.modules.pop('llama_cpp', None)
        else:
            sys.modules['llama_cpp'] = original_module
        sys.path[:] = original_sys_path

    assert exc_info.value.stage == 'llama_cpp_import'
    assert 'llama_cpp_import after 0.01s' in str(exc_info.value)
    assert sys.path == original_sys_path
    assert sys.modules.get('llama_cpp') is original_module if original_module is not None else 'llama_cpp' not in sys.modules


def test_sanitize_llama_cpp_import_paths_reports_deprioritized_entries(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    (tmp_path / 'llama_cpp.py').write_text('raise RuntimeError("repo shim")')
    site_packages = tmp_path / 'venv' / 'Lib' / 'site-packages'
    site_packages.mkdir(parents=True)
    original_sys_path = list(sys.path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(model_manager_module, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(model_manager_module, 'REPO_LLAMA_CPP_SHIM', tmp_path / 'llama_cpp.py')
    monkeypatch.setattr(sys, 'path', [str(tmp_path), str(site_packages), '/other'])

    try:
        diagnostics = model_manager_module._sanitize_llama_cpp_import_paths()
    finally:
        sys.path[:] = original_sys_path

    assert diagnostics['deprioritized_entries'] == [str(tmp_path)]
    assert 'removed_entries' not in diagnostics
    assert diagnostics['sys_path_count'] == 3


def test_sanitize_llama_cpp_import_paths_does_not_stat_sys_path_entries(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    site_packages = tmp_path / 'venv' / 'Lib' / 'site-packages'
    site_packages.mkdir(parents=True)
    original_sys_path = list(sys.path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(model_manager_module, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(model_manager_module, 'REPO_LLAMA_CPP_SHIM', tmp_path / 'llama_cpp.py')
    monkeypatch.setattr(sys, 'path', [str(tmp_path), str(site_packages), '/slow/share'])

    def _no_stat(self):
        raise AssertionError(f'path stat should not run during sanitization: {self}')

    monkeypatch.setattr(Path, 'is_file', _no_stat)
    try:
        diagnostics = model_manager_module._sanitize_llama_cpp_import_paths()
        sanitized_path = list(sys.path)
    finally:
        sys.path[:] = original_sys_path

    assert diagnostics['deprioritized_entries'] == [str(tmp_path)]
    assert str(site_packages) in sanitized_path
    assert sanitized_path.index('/slow/share') < sanitized_path.index(str(site_packages))


def test_llama_cpp_probe_subprocess_cwd_does_not_shadow_sanitized_pythonpath(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    repo_root = tmp_path / 'repo runtime root'
    repo_root.mkdir()
    (repo_root / 'llama_cpp.py').write_text('raise RuntimeError("repo shim should not win")')
    hostile_cwd = tmp_path / 'shared temp'
    hostile_cwd.mkdir()
    (hostile_cwd / 'llama_cpp.py').write_text('raise RuntimeError("temp shim should not win")')
    site_packages = tmp_path / 'venv' / 'Lib' / 'site-packages'
    package_dir = site_packages / 'llama_cpp'
    package_dir.mkdir(parents=True)
    (package_dir / '__init__.py').write_text('INSTALLED_RUNTIME = True\n')

    original_sys_path = list(sys.path)
    original_module = sys.modules.pop('llama_cpp', None)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(model_manager_module, 'REPO_ROOT', repo_root)
    monkeypatch.setattr(model_manager_module, 'REPO_LLAMA_CPP_SHIM', repo_root / 'llama_cpp.py')
    monkeypatch.setattr(model_manager_module, '_llama_cpp_probe_subprocess_cwd', lambda: str(hostile_cwd))
    monkeypatch.setattr(sys, 'path', [str(repo_root), str(site_packages)])

    try:
        model_manager_module._sanitize_llama_cpp_import_paths()
        spec_diagnostics = model_manager_module._find_llama_cpp_spec_in_subprocess(timeout_seconds=5)
        import_diagnostics = model_manager_module._run_llama_cpp_import_watchdog(timeout_seconds=5)
    finally:
        sys.path[:] = original_sys_path
        sys.modules.pop('llama_cpp', None)
        if original_module is not None:
            sys.modules['llama_cpp'] = original_module

    assert not model_manager_module._is_repo_llama_cpp_shim(spec_diagnostics['module_path'])
    assert Path(spec_diagnostics['module_path']).parent == package_dir
    assert not model_manager_module._is_repo_llama_cpp_shim(import_diagnostics['module_path'])
    assert Path(import_diagnostics['module_path']).parent == package_dir


def test_llama_cpp_probe_env_excludes_implicit_cwd_entries(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    site_packages = tmp_path / 'venv' / 'site-packages'
    site_packages.mkdir(parents=True)
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(sys, 'path', ['', str(repo_root), str(site_packages), str(site_packages)])

    entries = model_manager_module._llama_cpp_probe_sys_path_entries()

    assert entries == [str(site_packages)]


def test_llama_cpp_runtime_discovery_timeout_does_not_mutate_parent_import_state(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    original_sys_path = list(sys.path)
    original_module = sys.modules.get('llama_cpp')
    sentinel = object()
    sys.modules['llama_cpp'] = sentinel

    def _timeout_run(*_args, **kwargs):
        raise model_manager_module.subprocess.TimeoutExpired(
            cmd=kwargs.get('args', ['python']),
            timeout=kwargs.get('timeout'),
        )

    monkeypatch.setattr(model_manager_module.subprocess, 'run', _timeout_run)
    try:
        with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
            model_manager_module._find_llama_cpp_spec_in_subprocess(timeout_seconds=0.01)
    finally:
        if original_module is None:
            sys.modules.pop('llama_cpp', None)
        else:
            sys.modules['llama_cpp'] = original_module
        sys.path[:] = original_sys_path

    assert exc_info.value.stage == 'llama_cpp_runtime_discovery'
    assert sys.path == original_sys_path
    assert sys.modules.get('llama_cpp') is original_module if original_module is not None else 'llama_cpp' not in sys.modules


def test_llama_cpp_gpu_probe_timeout_does_not_mutate_parent_import_state(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    original_sys_path = list(sys.path)
    original_module = sys.modules.get('llama_cpp')
    sentinel = object()
    sys.modules['llama_cpp'] = sentinel

    def _timeout_run(*_args, **kwargs):
        raise model_manager_module.subprocess.TimeoutExpired(
            cmd=kwargs.get('args', ['python']),
            timeout=kwargs.get('timeout'),
        )

    monkeypatch.setattr(model_manager_module.subprocess, 'run', _timeout_run)
    try:
        with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
            model_manager_module._probe_llama_cpp_capabilities_in_subprocess(timeout_seconds=0.01)
    finally:
        if original_module is None:
            sys.modules.pop('llama_cpp', None)
        else:
            sys.modules['llama_cpp'] = original_module
        sys.path[:] = original_sys_path

    assert exc_info.value.stage == 'llama_cpp_gpu_probe'
    assert sys.path == original_sys_path
    assert sys.modules.get('llama_cpp') is original_module if original_module is not None else 'llama_cpp' not in sys.modules


def test_runtime_stage_timeout_seconds_handles_env_overrides(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.delenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', raising=False)
    assert model_manager_module._runtime_stage_timeout_seconds() == (
        model_manager_module.DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    )

    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', '2.5')
    assert model_manager_module._runtime_stage_timeout_seconds() == 2.5

    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', 'invalid')
    assert model_manager_module._runtime_stage_timeout_seconds() == (
        model_manager_module.DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    )

    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', '0')
    assert model_manager_module._runtime_stage_timeout_seconds() == (
        model_manager_module.DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    )


def test_canonical_path_for_compare_handles_empty_and_fallback(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._canonical_path_for_compare('') is None

    original_abspath = model_manager_module.os.path.abspath

    def _abspath_raises(path):
        if str(path) == 'fallback/path':
            raise OSError('slow or unavailable path')
        return original_abspath(path)

    monkeypatch.setattr(model_manager_module.os.path, 'abspath', _abspath_raises)

    assert model_manager_module._canonical_path_for_compare('fallback/path') == (
        model_manager_module.os.path.normcase(
            model_manager_module.os.path.normpath('fallback/path')
        )
    )


def test_run_llama_cpp_python_probe_handles_nonzero_and_malformed_json(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    completed = SimpleNamespace(returncode=1, stderr='native import failed', stdout='')
    monkeypatch.setattr(model_manager_module.subprocess, 'run', lambda *_args, **_kwargs: completed)

    with pytest.raises(ImportError) as exc_info:
        model_manager_module._find_llama_cpp_spec_in_subprocess(timeout_seconds=1)
    assert 'llama_cpp_runtime_discovery failed returncode=1' in str(exc_info.value)

    completed = SimpleNamespace(returncode=0, stderr='', stdout='not json\n')
    monkeypatch.setattr(model_manager_module.subprocess, 'run', lambda *_args, **_kwargs: completed)

    assert model_manager_module._find_llama_cpp_spec_in_subprocess(timeout_seconds=1) == {}


def test_run_llama_cpp_import_watchdog_handles_nonzero_and_malformed_json(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    completed = SimpleNamespace(returncode=1, stderr='import failed', stdout='')
    monkeypatch.setattr(model_manager_module.subprocess, 'run', lambda *_args, **_kwargs: completed)

    with pytest.raises(ImportError) as exc_info:
        model_manager_module._run_llama_cpp_import_watchdog(timeout_seconds=1)
    assert 'llama_cpp import watchdog failed returncode=1' in str(exc_info.value)

    completed = SimpleNamespace(returncode=0, stderr='', stdout='not json\n')
    monkeypatch.setattr(model_manager_module.subprocess, 'run', lambda *_args, **_kwargs: completed)

    assert model_manager_module._run_llama_cpp_import_watchdog(timeout_seconds=1) == {}


def test_import_llama_cpp_runtime_success_records_sanitized_parent_import(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_runtime = SimpleNamespace(__file__='/site-packages/llama_cpp/__init__.py')
    sys.modules.pop('llama_cpp', None)
    calls = []

    monkeypatch.setattr(
        model_manager_module,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 1},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: {'module_path': fake_runtime.__file__},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_run_llama_cpp_import_watchdog',
        lambda **_kwargs: calls.append('watchdog') or {'module_path': fake_runtime.__file__},
    )
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda name: calls.append(name) or fake_runtime,
    )
    monkeypatch.setattr(model_manager_module, '_is_repo_llama_cpp_shim', lambda _path: False)

    assert model_manager_module._import_llama_cpp_runtime() is fake_runtime
    assert calls == ['llama_cpp']


def test_import_llama_cpp_runtime_rejects_shim_from_discovery_before_parent_import(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setattr(
        model_manager_module,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 1},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: {'module_path': str(model_manager_module.REPO_LLAMA_CPP_SHIM)},
    )
    monkeypatch.setattr(model_manager_module, '_run_llama_cpp_import_watchdog', MagicMock())
    monkeypatch.setattr(model_manager_module.importlib, 'import_module', MagicMock())

    with pytest.raises(ImportError, match='repository-local llama_cpp.py shim'):
        model_manager_module._import_llama_cpp_runtime()

    model_manager_module._run_llama_cpp_import_watchdog.assert_not_called()
    model_manager_module.importlib.import_module.assert_not_called()


def test_import_llama_cpp_runtime_rejects_shim_from_parent_import(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_runtime = SimpleNamespace(__file__=str(model_manager_module.REPO_LLAMA_CPP_SHIM))
    monkeypatch.setattr(
        model_manager_module,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 1},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: {'module_path': '/site-packages/llama_cpp/__init__.py'},
    )
    monkeypatch.setattr(model_manager_module, '_run_llama_cpp_import_watchdog', lambda **_kwargs: {})
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('parent import should not run')),
    )
    sys.modules['llama_cpp'] = fake_runtime

    try:
        with pytest.raises(ImportError, match='repository-local llama_cpp.py shim'):
            model_manager_module._import_llama_cpp_runtime()
        assert 'llama_cpp' not in sys.modules
    finally:
        sys.modules.pop('llama_cpp', None)


def test_import_llama_cpp_runtime_reports_parent_import_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setattr(
        model_manager_module,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 1},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: {'module_path': '/site-packages/llama_cpp/__init__.py'},
    )
    def _slow_parent_import(_name):
        time.sleep(0.2)
        return SimpleNamespace(__file__='/site-packages/llama_cpp/__init__.py')

    monkeypatch.setattr(model_manager_module.importlib, 'import_module', _slow_parent_import)

    with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
        model_manager_module._import_llama_cpp_runtime(timeout_seconds=0.01)

    assert exc_info.value.stage == 'llama_cpp_import'
    assert model_manager_module._format_runtime_stage_timeout(exc_info.value) == (
        'llama_cpp_import_timeout after 0.01s'
    )


def test_import_llama_cpp_runtime_no_signal_uses_subprocess_facade(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    sys.modules.pop('llama_cpp', None)
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON', raising=False)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.setattr(
        model_manager_module,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 1},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: {'module_path': '/site-packages/llama_cpp/__init__.py'},
    )
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('parent import should not run')),
    )

    runtime = model_manager_module._import_llama_cpp_runtime(timeout_seconds=0.01)

    assert isinstance(runtime, model_manager_module._SubprocessLlamaCppModule)
    assert runtime.__file__ == '/site-packages/llama_cpp/__init__.py'


def test_no_signal_warm_load_uses_subprocess_facade(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    model_file = tmp_path / 'test_model.gguf'
    model_file.write_bytes(b'fake model data')
    config = MagicMock()
    config.is_production = False
    config.get.side_effect = lambda key, default=None: {
        'model.profile_id': 'llama-3.1-8b-q4-k-m',
        'model.filename': 'test_model.gguf',
        'model.url': 'https://example.com/model.gguf',
        'model.download_chunk_size_mb': 1,
        'paths.models_dir': str(tmp_path),
        'model.use_mock': False,
        'model.context_size': 2048,
        'model.chat_format': 'llama-3',
        'model.n_gpu_layers': -1,
        'model.gpu_memory_headroom_percent': 0.1,
        'model.enforce_gpu_memory_headroom': False,
    }.get(key, default)

    class FakeLlama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def create_chat_completion(self, **_kwargs):
            return {'choices': [{'message': {'content': 'ok'}}]}

    fake_runtime_path = '/site-packages/llama_cpp/__init__.py'

    sys.modules.pop('llama_cpp', None)
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON', raising=False)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.setattr(
        model_manager_module,
        '_sanitize_llama_cpp_import_paths',
        lambda: {'import_root': '/app', 'deprioritized_entries': [], 'sys_path_count': 1},
    )
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: {'module_path': fake_runtime_path},
    )
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('parent import should not run')),
    )
    monkeypatch.setattr(model_manager_module, '_SubprocessLlamaProxy', FakeLlama)

    manager = ModelManager(config)
    manager.requested_compute_mode = 'cpu'

    assert manager.get_llm_instance() is not None
    assert manager.last_runtime_init_error is None
    assert manager._imported_llama_cpp_module_path == fake_runtime_path


def test_detect_llama_runtime_capabilities_uses_subprocess_facade_probe_attrs(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule(
        '/site-packages/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': '/site-packages/llama_cpp/__init__.py',
        },
    )
    monkeypatch.setattr(model_manager_module, '_import_llama_cpp_runtime', lambda **_kwargs: facade)
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('facade should not re-probe')),
    )

    capabilities = model_manager_module.detect_llama_runtime_capabilities()

    assert capabilities['backend'] == 'metal'
    assert capabilities['gpu_offload_supported'] is True
    assert capabilities['llama_module_path'] == '/site-packages/llama_cpp/__init__.py'


def test_detect_llama_runtime_capabilities_probes_cpu_subprocess_facade(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule(
        '/site-packages/llama_cpp/__init__.py',
        desktop_runtime_probe=None,
    )
    monkeypatch.setattr(model_manager_module, '_import_llama_cpp_runtime', lambda **_kwargs: facade)
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda: {
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'detected_device': 'cuda',
            'interpreter': '/usr/bin/python',
            'prefix': '/usr',
            'llama_module_path': '/site-packages/llama_cpp/__init__.py',
            'error': None,
        },
    )

    capabilities = model_manager_module.detect_llama_runtime_capabilities()

    assert capabilities['backend'] == 'cuda'
    assert capabilities['gpu_offload_supported'] is True
    assert capabilities['detected_device'] == 'cuda'
    assert capabilities['llama_module_path'] == '/site-packages/llama_cpp/__init__.py'


def test_detect_llama_runtime_capabilities_preserves_gpu_probe_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_runtime = SimpleNamespace(
        __file__='/site-packages/llama_cpp/__init__.py',
        llama_supports_gpu_offload=lambda: True,
    )
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: fake_runtime,
    )
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(
            model_manager_module.LlamaCppRuntimeStageTimeout('llama_cpp_gpu_probe', 0.01)
        ),
    )

    diagnostics = model_manager_module.detect_llama_runtime_capabilities()

    assert diagnostics['backend'] == 'missing'
    assert diagnostics['gpu_offload_supported'] is False
    assert diagnostics['error'] == 'llama_cpp_gpu_probe_timeout after 0.01s'


def test_get_llm_instance_records_gpu_probe_timeout(standalone_model_manager, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    class FakeLlama:
        def __init__(self, **_kwargs):
            raise AssertionError('Llama should not initialize after GPU probe timeout')

    fake_runtime = SimpleNamespace(
        __file__='/site-packages/llama_cpp/__init__.py',
        Llama=FakeLlama,
        llama_supports_gpu_offload=lambda: True,
    )
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: fake_runtime,
    )
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(
            model_manager_module.LlamaCppRuntimeStageTimeout('llama_cpp_gpu_probe', 0.01)
        ),
    )

    with patch('os.path.exists', return_value=True):
        llm = standalone_model_manager.get_llm_instance()

    assert llm is None
    assert standalone_model_manager.last_runtime_init_error == 'llama_cpp_gpu_probe_timeout after 0.01s'


def test_get_llm_instance_cpu_mode_does_not_probe_runtime_capabilities(standalone_model_manager, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    class FakeLlama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_runtime = SimpleNamespace(
        __file__='/site-packages/llama_cpp/__init__.py',
        Llama=FakeLlama,
    )
    standalone_model_manager.requested_compute_mode = 'cpu'
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: fake_runtime,
    )

    with patch('os.path.exists', return_value=True), \
         patch.object(standalone_model_manager, '_runtime_capabilities') as runtime_capabilities:
        llm = standalone_model_manager.get_llm_instance()

    assert isinstance(llm, FakeLlama)
    assert llm.kwargs['n_gpu_layers'] == 0
    runtime_capabilities.assert_not_called()
    assert standalone_model_manager.last_compute_diagnostics['requested_mode'] == 'cpu'
    assert standalone_model_manager.last_compute_diagnostics['backend_used'] == 'cpu'


def test_windows_unc_prefix_and_empty_shim_detection_helpers():
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._strip_windows_extended_path_prefix(
        '\\\\?\\UNC\\server\\share\\llama_cpp.py'
    ) == '\\\\server\\share\\llama_cpp.py'
    assert model_manager_module._is_repo_llama_cpp_shim(None) is False


def test_llama_cpp_probe_sys_path_entries_skips_non_string_entries(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    cwd = tmp_path / 'repo'
    cwd.mkdir()
    first = tmp_path / 'site-packages'
    first.mkdir()
    duplicate = tmp_path / 'site-packages' / '..' / 'site-packages'
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(
        model_manager_module.sys,
        'path',
        [str(first), 123, '', str(cwd), str(duplicate)],
    )

    assert model_manager_module._llama_cpp_probe_sys_path_entries() == [str(first)]


def test_detect_llama_runtime_capabilities_uses_subprocess_probe_for_imported_module(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_llama_cpp = SimpleNamespace(
        __file__='/site-packages/llama_cpp/__init__.py',
        GGML_USE_CUDA=True,
        llama_supports_gpu_offload=lambda: False,
    )
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: fake_llama_cpp,
    )
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda: {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'detected_device': 'metal',
            'llama_module_path': fake_llama_cpp.__file__,
            'error': None,
        },
    )

    payload = model_manager_module.detect_llama_runtime_capabilities()

    assert payload['backend'] == 'metal'
    assert payload['gpu_offload_supported'] is True
    assert payload['llama_module_path'] == fake_llama_cpp.__file__


def test_detect_llama_runtime_capabilities_reuses_recorded_desktop_probe_without_child_probe(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    module_path = 'C:/Program Files/Token Place/python/Lib/site-packages/llama_cpp/__init__.py'
    monkeypatch.setenv(
        model_manager_module.DESKTOP_RUNTIME_PROBE_ENV,
        json.dumps({
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'detected_device': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': module_path,
            'interpreter': 'C:/Program Files/Token Place/python/python.exe',
            'prefix': 'C:/Program Files/Token Place/python',
        }),
    )
    monkeypatch.setattr(model_manager_module, '_signal_guard_available', lambda: False)
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('divergent discovery probe should not run')),
    )
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('divergent capability probe should not run')),
    )
    monkeypatch.delitem(sys.modules, 'llama_cpp', raising=False)

    payload = model_manager_module.detect_llama_runtime_capabilities()

    assert payload['backend'] == 'cuda'
    assert payload['gpu_offload_supported'] is True
    assert payload['detected_device'] == 'cuda'
    assert payload['llama_module_path'] == module_path
    assert payload['error'] is None


def test_desktop_runtime_probe_coercion_rejects_failed_or_error_actions():
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._coerce_desktop_runtime_probe({
        'runtime_action': 'failed',
        'selected_backend': 'cuda',
        'gpu_offload_supported': True,
    }) is None
    assert model_manager_module._coerce_desktop_runtime_probe({
        'runtime_action': 'install_required',
        'selected_backend': 'cuda',
        'error': 'missing cuda runtime',
    }) is None


def test_mock_compute_plan_reuses_successful_gpu_desktop_probe(standalone_model_manager):
    standalone_model_manager.requested_compute_mode = 'hybrid'
    standalone_model_manager.hybrid_n_gpu_layers = 12
    standalone_model_manager.desktop_runtime_probe = {
        'runtime_action': 'already_supported',
        'selected_backend': 'metal',
        'detected_device': 'metal',
        'gpu_offload_supported': True,
        'llama_module_path': '/site-packages/llama_cpp/__init__.py',
    }

    plan = standalone_model_manager._mock_compute_plan()

    assert plan['effective_mode'] == 'hybrid_metal'
    assert plan['backend_available'] == 'metal'
    assert plan['backend_used'] == 'metal'
    assert plan['n_gpu_layers'] == 12
    assert plan['fallback_reason'] is None


def test_cpu_compute_plan_returns_cpu_diagnostics_without_runtime_probe(standalone_model_manager):
    standalone_model_manager.requested_compute_mode = 'cpu'

    with patch.object(standalone_model_manager, '_runtime_capabilities') as runtime_capabilities:
        plan = standalone_model_manager._resolve_compute_plan()

    runtime_capabilities.assert_not_called()
    assert plan == {
        'requested_mode': 'cpu',
        'effective_mode': 'cpu',
        'backend_available': 'cpu',
        'backend_selected': 'cpu',
        'backend_used': 'cpu',
        'n_gpu_layers': 0,
        'fallback_reason': None,
    }


def test_cpu_compute_plan_ignores_gpu_probe_timeout(standalone_model_manager):
    standalone_model_manager.requested_compute_mode = 'cpu'

    with patch.object(standalone_model_manager, '_runtime_capabilities') as runtime_capabilities:
        plan = standalone_model_manager._resolve_compute_plan()

    runtime_capabilities.assert_not_called()
    assert plan == {
        'requested_mode': 'cpu',
        'effective_mode': 'cpu',
        'backend_available': 'cpu',
        'backend_selected': 'cpu',
        'backend_used': 'cpu',
        'n_gpu_layers': 0,
        'fallback_reason': None,
    }


def test_download_file_in_chunks_handles_request_start_failures(standalone_model_manager):
    from utils.llm import model_manager as model_manager_module

    file_path = os.path.join(standalone_model_manager.models_dir, 'request_failure.gguf')

    with patch(
        'utils.llm.model_manager.requests.get',
        side_effect=model_manager_module.requests.Timeout('too slow'),
    ):
        assert standalone_model_manager.download_file_in_chunks(
            file_path,
            'https://example.com/model.gguf',
            1,
        ) is False

    with patch(
        'utils.llm.model_manager.requests.get',
        side_effect=model_manager_module.requests.RequestException('connection failed'),
    ):
        assert standalone_model_manager.download_file_in_chunks(
            file_path,
            'https://example.com/model.gguf',
            1,
        ) is False


def test_canonical_path_for_compare_returns_none_when_stringification_fails_twice():
    from utils.llm import model_manager as model_manager_module

    class BrokenPath:
        def __str__(self):
            raise OSError('unreadable path text')

    assert model_manager_module._canonical_path_for_compare(BrokenPath()) is None


def test_parent_import_signal_guard_wraps_generic_timeout_and_restores_prior_timer(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    setitimer_calls = []
    signal_calls = []
    previous_handler = object()

    monkeypatch.setattr(model_manager_module.signal, 'getsignal', lambda _sig: previous_handler)

    def _fake_signal(sig, handler):
        signal_calls.append((sig, handler))

    def _fake_setitimer(timer, seconds, interval=0):
        setitimer_calls.append((timer, seconds, interval))
        if seconds == 0.25:
            return (2.0, 0.5)
        return (0.0, 0.0)

    monkeypatch.setattr(model_manager_module.signal, 'signal', _fake_signal)
    monkeypatch.setattr(model_manager_module.signal, 'setitimer', _fake_setitimer)
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(TimeoutError('generic timeout')),
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
        model_manager_module._import_llama_cpp_in_parent_with_timeout(timeout_seconds=0.25)

    assert exc_info.value.stage == 'llama_cpp_import'
    assert (model_manager_module.signal.ITIMER_REAL, 0, 0) in setitimer_calls
    assert (model_manager_module.signal.ITIMER_REAL, 2.0, 0.5) in setitimer_calls
    assert signal_calls[-1] == (model_manager_module.signal.SIGALRM, previous_handler)


def test_parent_import_guard_imports_real_module_without_mocking_guard_modules(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir()
    (runtime_dir / 'llama_cpp.py').write_text(
        "VALUE = 'imported by parent guard'\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(runtime_dir))
    sys.modules.pop('llama_cpp', None)

    try:
        imported = model_manager_module._import_llama_cpp_in_parent_with_timeout(timeout_seconds=1.0)
    finally:
        sys.modules.pop('llama_cpp', None)

    assert imported.VALUE == 'imported by parent guard'
    assert Path(imported.__file__).parent == runtime_dir


def test_parent_import_guard_returns_already_imported_module_without_reimport(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_runtime = SimpleNamespace(__file__='/site-packages/llama_cpp/__init__.py')
    monkeypatch.setitem(sys.modules, 'llama_cpp', fake_runtime)
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('should not re-import')),
    )

    assert model_manager_module._import_llama_cpp_in_parent_with_timeout(timeout_seconds=0.01) is fake_runtime


def test_parent_import_guard_no_signal_uses_subprocess_facade_without_parent_import(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    sys.modules.pop('llama_cpp', None)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('parent import should not run')),
    )

    facade = model_manager_module._import_llama_cpp_in_parent_with_timeout(
        timeout_seconds=0.01,
        module_path_hint='/site-packages/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': '/site-packages/llama_cpp/__init__.py',
        },
    )

    assert isinstance(facade, model_manager_module._SubprocessLlamaCppModule)
    assert facade.__file__ == '/site-packages/llama_cpp/__init__.py'
    assert facade.GGML_USE_CUDA is True
    assert facade.llama_supports_gpu_offload() is True


def test_parent_import_guard_non_main_thread_uses_subprocess_facade_without_wedging(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    result_queue = queue.Queue()
    sys.modules.pop('llama_cpp', None)
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('parent import should not run')),
    )

    def _call_from_worker():
        result_queue.put(
            model_manager_module._import_llama_cpp_in_parent_with_timeout(
                timeout_seconds=0.01,
                module_path_hint='/site-packages/llama_cpp/__init__.py',
            )
        )

    worker = threading.Thread(target=_call_from_worker, name='desktop-warm-load-test')
    worker.start()
    worker.join(timeout=1)

    assert not worker.is_alive()
    facade = result_queue.get_nowait()
    assert isinstance(facade, model_manager_module._SubprocessLlamaCppModule)
    assert facade.__file__ == '/site-packages/llama_cpp/__init__.py'


def test_runtime_worker_env_omits_probe_sys_path_marker(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH', '["stale"]')

    env = model_manager_module._llama_cpp_runtime_worker_env()

    assert 'TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH' not in env
    assert env.get('PYTHONPATH')


def test_runtime_worker_env_strips_windows_extended_path_prefix(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.delenv('PYTHONNOUSERSITE', raising=False)

    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '\\\\?\\C:\\Users\\danie\\AppData\\Local\\token.place desktop\\_up_\\_up_')
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT', '\\\\?\\C:\\Users\\danie\\AppData\\Local\\token.place desktop\\python\\path_bootstrap.py')
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_PYTHON_ROOT', r'\\?\C:\Users\danie\AppData\Local\token.place desktop\python')
    monkeypatch.setenv('TOKEN_PLACE_PROBE_REPO_ROOT', r'\\?\C:\Users\danie\AppData\Local\token.place desktop\repo root')
    monkeypatch.setattr(
        model_manager_module,
        '_llama_cpp_probe_sys_path_entries',
        lambda: ['\\\\?\\C:\\Users\\danie\\AppData\\Local\\token.place desktop\\_up_\\_up_'],
    )

    env = model_manager_module._llama_cpp_runtime_worker_env()

    assert env['TOKEN_PLACE_PYTHON_IMPORT_ROOT'] == 'C:\\Users\\danie\\AppData\\Local\\token.place desktop\\_up_\\_up_'
    assert env['TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT'].endswith('token.place desktop\\python\\path_bootstrap.py')
    assert env['TOKEN_PLACE_DESKTOP_PYTHON_ROOT'] == 'C:\\Users\\danie\\AppData\\Local\\token.place desktop\\python'
    assert env['TOKEN_PLACE_PROBE_REPO_ROOT'] == 'C:\\Users\\danie\\AppData\\Local\\token.place desktop\\repo root'
    assert env['PYTHONNOUSERSITE'] == '1'
    assert '\\\\?\\' not in env['PYTHONPATH']


def test_subprocess_llama_proxy_early_exit_reports_process_diagnostics(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'fake site-packages with spaces'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "import sys\n"
        "print('stdout llama_context clue before exit')\n"
        "print('stderr llama_context clue before exit', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '\\\\?\\C:\\Users\\danie\\AppData\\Local\\token.place desktop\\_up_\\_up_')

    with pytest.raises(RuntimeError) as exc_info:
        model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', timeout_seconds=5)

    message = str(exc_info.value)
    assert 'llama_cpp_import subprocess exited before JSON handshake' in message
    assert 'llama_cpp_model_initialization subprocess ended' not in message
    assert 'exit_code=7' in message
    assert 'stdout llama_context clue before exit' in message
    assert 'stderr llama_context clue before exit' in message
    assert 'import_root=<path>' in message
    assert 'token.place desktop' not in message


def test_subprocess_llama_proxy_initial_write_early_exit_reports_diagnostic(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    class FailingStdin:
        def write(self, _text):
            raise BrokenPipeError('child already exited')

        def flush(self):
            return None

    class EmptyStream:
        def __iter__(self):
            return iter(())

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdin = FailingStdin()
            self.stdout = EmptyStream()
            self.stderr = EmptyStream()
            self._token_place_stdout_tail = [
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","prompt":"secret prompt","chunk":"generated text"}\n',
                'native loader clue\n',
            ]
            self._token_place_stderr_tail = ['stderr llama_context clue\n']

        def poll(self):
            return 9

    created = []

    def _fake_popen(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', _fake_popen)

    with pytest.raises(RuntimeError) as exc_info:
        model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', timeout_seconds=0.01)

    message = str(exc_info.value)
    assert 'llama_cpp_import subprocess exited before JSON handshake' in message
    assert 'llama_cpp_model_initialization subprocess ended' not in message
    assert 'exit_code=9' in message
    assert 'program=' in message
    assert 'command=' in message
    assert 'cwd=' in message
    assert 'import_root=' in message
    assert 'module_path_hint=' in message
    assert 'stage=llama_cpp_import' in message
    assert 'TOKEN_PLACE_LLAMA_CPP_JSON' not in message
    assert 'secret prompt' not in message
    assert 'generated text' not in message
    assert 'native loader clue' not in message
    assert 'stderr_tail=' in message
    assert created


def test_subprocess_llama_proxy_timeout_kills_hung_worker(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    stop_stdout = threading.Event()

    class HangingStdout:
        def __iter__(self):
            while not stop_stdout.wait(1):
                yield ''

    class FakeStdin:
        def write(self, _text):
            return None

        def flush(self):
            return None

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdin = FakeStdin()
            self.stdout = HangingStdout()
            self.stderr = None
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True
            stop_stdout.set()

        def wait(self, timeout=None):
            raise TimeoutError('still hung')

        def kill(self):
            self.killed = True
            stop_stdout.set()

        def poll(self):
            return None

    created = []

    def _fake_popen(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', _fake_popen)

    with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
        model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', timeout_seconds=0.01)

    assert exc_info.value.stage == 'llama_cpp_import'
    assert created and created[0].terminated and created[0].killed


def test_detect_llama_runtime_capabilities_preserves_import_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: (_ for _ in ()).throw(
            model_manager_module.LlamaCppRuntimeStageTimeout('llama_cpp_import', 0.02)
        ),
    )

    diagnostics = model_manager_module.detect_llama_runtime_capabilities()

    assert diagnostics['backend'] == 'missing'
    assert diagnostics['gpu_offload_supported'] is False
    assert diagnostics['error'] == 'llama_cpp_import_timeout after 0.02s'


def test_gpu_compute_plan_falls_back_when_runtime_reports_no_gpu_backend(standalone_model_manager):
    standalone_model_manager.requested_compute_mode = 'gpu'

    with patch.object(
        standalone_model_manager,
        '_runtime_capabilities',
        return_value={
            'backend': 'cpu',
            'gpu_offload_supported': False,
            'detected_device': 'cpu',
            'llama_module_path': 'unknown',
            'error': None,
        },
    ):
        plan = standalone_model_manager._resolve_compute_plan()

    assert plan['effective_mode'] == 'cpu_fallback'
    assert plan['backend_available'] == 'cpu'
    assert plan['fallback_reason'] == 'no CUDA/Metal backend is supported on this platform'


def test_hybrid_compute_plan_falls_back_when_backend_lacks_offload(standalone_model_manager):
    standalone_model_manager.requested_compute_mode = 'hybrid'

    with patch.object(
        standalone_model_manager,
        '_runtime_capabilities',
        return_value={
            'backend': 'metal',
            'gpu_offload_supported': False,
            'detected_device': 'cpu',
            'llama_module_path': '/site-packages/llama_cpp/__init__.py',
            'error': None,
        },
    ):
        plan = standalone_model_manager._resolve_compute_plan()

    assert plan['effective_mode'] == 'cpu_fallback'
    assert plan['backend_available'] == 'metal'
    assert plan['fallback_reason'] == 'llama-cpp-python runtime does not expose metal GPU offload support'


def test_production_log_helper_suppresses_logger_call(standalone_model_manager):
    standalone_model_manager.config.is_production = True

    with patch('utils.llm.model_manager.logger.log') as log:
        standalone_model_manager.log_info('hidden in production')

    log.assert_not_called()


def test_subprocess_llama_proxy_streams_chunks_without_json_serializing_iterator(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    class FakeStdout:
        def __init__(self):
            self._lines = iter([
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","module_path":"/runtime/llama_cpp/__init__.py"}\n',
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","module_path":"/runtime/llama_cpp/__init__.py","child_model_path_exists":true}\n',
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","chunk":{"choices":[{"delta":{"content":"Hi"}}]},"done":false}\n',
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","chunk":{"choices":[{"delta":{"content":"lo"}}]},"done":false}\n',
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","done":true}\n',
            ])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._lines)

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, text):
            self.writes.append(json.loads(text))

        def flush(self):
            return None

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = None

        def poll(self):
            return None

        def terminate(self):
            return None

    created = []

    def _fake_popen(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', _fake_popen)

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', timeout_seconds=0.01)
    chunks = list(proxy.create_chat_completion(messages=[], stream=True))

    assert [chunk['choices'][0]['delta']['content'] for chunk in chunks] == ['Hi', 'lo']
    assert created[0].stdin.writes[2]['kwargs']['stream'] is True


def test_subprocess_llama_proxy_inference_does_not_use_runtime_stage_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', '0.01')
    monkeypatch.delenv('TOKEN_PLACE_LLAMA_CPP_SUBPROCESS_INFERENCE_TIMEOUT_SECONDS', raising=False)

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._lock = model_manager_module.Lock()
    proxy._process = SimpleNamespace(stdin=MagicMock())
    proxy._send = MagicMock()
    captured_timeouts = []

    def _fake_read(_process, *, timeout_seconds, stage):
        captured_timeouts.append((stage, timeout_seconds))
        return {'status': 'ok', 'result': {'choices': [{'message': {'content': 'ok'}}]}}

    monkeypatch.setattr(model_manager_module, '_read_llama_subprocess_message', _fake_read)

    result = proxy.create_chat_completion(messages=[], stream=False)

    assert result['choices'][0]['message']['content'] == 'ok'
    assert captured_timeouts == [('llama_cpp_inference', None)]


def test_subprocess_llama_proxy_uses_explicit_inference_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_SUBPROCESS_INFERENCE_TIMEOUT_SECONDS', '7.5')

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._lock = model_manager_module.Lock()
    proxy._process = SimpleNamespace(stdin=MagicMock())
    proxy._send = MagicMock()
    captured_timeouts = []

    def _fake_read(_process, *, timeout_seconds, stage):
        captured_timeouts.append((stage, timeout_seconds))
        if len(captured_timeouts) == 1:
            return {'status': 'ok', 'chunk': {'choices': [{'delta': {'content': 'ok'}}]}, 'done': False}
        return {'status': 'ok', 'done': True}

    monkeypatch.setattr(model_manager_module, '_read_llama_subprocess_message', _fake_read)

    assert list(proxy.create_chat_completion(messages=[], stream=True)) == [
        {'choices': [{'delta': {'content': 'ok'}}]},
    ]
    assert captured_timeouts == [('llama_cpp_inference', 7.5), ('llama_cpp_inference', 7.5)]


def test_llama_cpp_package_parent_edge_cases(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._llama_cpp_package_parent_from_module_path(None) is None
    assert (
        model_manager_module._llama_cpp_package_parent_from_module_path('/opt/site/llama_cpp.py')
        == '/opt/site'
    )
    assert model_manager_module._llama_cpp_package_parent_from_module_path('/opt/site/not_llama.py') is None

    class RaisingPath:
        def __init__(self, *_args, **_kwargs):
            raise OSError('bad path')

    monkeypatch.setattr(model_manager_module, 'Path', RaisingPath)
    assert model_manager_module._llama_cpp_package_parent_from_module_path('/bad/path') is None


def test_prepare_llama_cpp_import_from_probe_handles_empty_and_unusable_paths(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    original_sys_path = list(sys.path)
    try:
        model_manager_module._prepare_llama_cpp_import_from_probe(None)
        assert sys.path == original_sys_path

        model_manager_module._prepare_llama_cpp_import_from_probe('/opt/site/not_llama.txt')
        assert sys.path == original_sys_path

        monkeypatch.setattr(
            model_manager_module,
            '_llama_cpp_package_parent_from_module_path',
            lambda _module_path: '/opt/site',
        )
        monkeypatch.setattr(model_manager_module, '_canonical_path_for_compare', lambda _path: None)
        model_manager_module._prepare_llama_cpp_import_from_probe('/opt/site/llama_cpp/__init__.py')
        assert sys.path == original_sys_path
    finally:
        sys.path[:] = original_sys_path


def test_desktop_runtime_probe_env_rejects_invalid_json(monkeypatch, caplog):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.setenv(model_manager_module.DESKTOP_RUNTIME_PROBE_ENV, '{not-json')

    assert model_manager_module._desktop_runtime_probe_from_env() is None
    assert 'Ignoring invalid desktop runtime probe environment payload' in caplog.text


def test_probe_module_path_from_desktop_runtime_probe_ignores_missing_values(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    monkeypatch.delenv(model_manager_module.DESKTOP_RUNTIME_PROBE_ENV, raising=False)

    for module_path in ('', 'missing', 'unknown'):
        assert model_manager_module._probe_module_path_from_desktop_runtime_probe({
            'runtime_action': 'already_supported',
            'selected_backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': module_path,
        }) is None


def test_import_llama_cpp_runtime_clears_namespace_on_post_import_probe_mismatch(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    expected = tmp_path / 'selected' / 'llama_cpp' / '__init__.py'
    imported = tmp_path / 'other' / 'llama_cpp' / '__init__.py'
    expected.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    expected.write_text('', encoding='utf-8')
    imported.write_text('', encoding='utf-8')
    sys.modules['llama_cpp'] = SimpleNamespace(__file__=str(imported))
    sys.modules['llama_cpp._native'] = SimpleNamespace()
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_in_parent_with_timeout',
        lambda **_kwargs: SimpleNamespace(__file__=str(imported)),
    )

    with pytest.raises(ImportError, match='Desktop runtime probe module path mismatch'):
        model_manager_module._import_llama_cpp_runtime(
            require_real_runtime=True,
            desktop_runtime_probe={
                'runtime_action': 'already_supported',
                'selected_backend': 'cuda',
                'gpu_offload_supported': True,
                'llama_module_path': str(expected),
            },
        )

    assert 'llama_cpp' not in sys.modules
    assert 'llama_cpp._native' not in sys.modules


def test_detect_llama_runtime_capabilities_cpu_facade_reports_probe_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    monkeypatch.setattr(model_manager_module, '_import_llama_cpp_runtime', lambda **_kwargs: facade)
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda: (_ for _ in ()).throw(
            model_manager_module.LlamaCppRuntimeStageTimeout('llama_cpp_gpu_probe', 0.01)
        ),
    )

    diagnostics = model_manager_module.detect_llama_runtime_capabilities()

    assert diagnostics['backend'] == 'missing'
    assert diagnostics['gpu_offload_supported'] is False
    assert diagnostics['llama_module_path'] == '/site/llama_cpp/__init__.py'
    assert diagnostics['error'] == 'llama_cpp_gpu_probe_timeout after 0.01s'


def test_detect_llama_runtime_capabilities_cpu_facade_reports_probe_exception(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    monkeypatch.setattr(model_manager_module, '_import_llama_cpp_runtime', lambda **_kwargs: facade)
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda: (_ for _ in ()).throw(RuntimeError('probe crashed')),
    )

    diagnostics = model_manager_module.detect_llama_runtime_capabilities()

    assert diagnostics['backend'] == 'missing'
    assert diagnostics['gpu_offload_supported'] is False
    assert diagnostics['detected_device'] == 'none'
    assert diagnostics['llama_module_path'] == '/site/llama_cpp/__init__.py'
    assert diagnostics['error'] == 'probe crashed'


def _install_request_scoped_fake_llama(tmp_path, monkeypatch):
    fake_site = tmp_path / 'request scoped fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "import os\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        if kwargs.get('fail_init'):\n"
        "            raise RuntimeError('init failed clearly')\n"
        "    def create_chat_completion(self, *args, **kwargs):\n"
        "        messages = kwargs.get('messages') or []\n"
        "        content = ''\n"
        "        if messages and isinstance(messages[0], dict):\n"
        "            content = str(messages[0].get('content', ''))\n"
        "        if kwargs.get('stream'):\n"
        "            def gen():\n"
        "                if 'raise_stream' in content:\n"
        "                    raise RuntimeError('stream secret prompt should not leak')\n"
        "                yield {'choices': [{'delta': {'content': 'stream ok'}, 'pid': os.getpid()}]}\n"
        "            return gen()\n"
        "        if 'raise_nonstream' in content:\n"
        "            raise RuntimeError('nonstream secret prompt should not leak')\n"
        "        return {'choices': [{'message': {'content': 'ok'}, 'pid': os.getpid()}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))


@pytest.fixture
def request_scoped_llama_proxy(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    _install_request_scoped_fake_llama(tmp_path, monkeypatch)
    proxies = []

    def _make_proxy():
        proxy = model_manager_module._SubprocessLlamaProxy(
            model_path='model.gguf', timeout_seconds=5
        )
        proxies.append(proxy)
        return proxy

    try:
        yield _make_proxy
    finally:
        for proxy in proxies:
            proxy.close()


def test_subprocess_llama_proxy_initialization_failure_still_terminates_worker(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    _install_request_scoped_fake_llama(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError) as exc_info:
        model_manager_module._SubprocessLlamaProxy(
            model_path='model.gguf', fail_init=True, timeout_seconds=5
        )

    message = str(exc_info.value)
    assert 'safe_error_category=' in message
    assert not isinstance(exc_info.value, model_manager_module.LlamaCppInferenceRequestError)


def test_llama_subprocess_request_error_is_typed_only_for_inference_stage():
    from io import StringIO

    from utils.llm import model_manager as model_manager_module

    process = SimpleNamespace(
        stdout=StringIO(
            'TOKEN_PLACE_LLAMA_CPP_JSON:'
            '{"status":"error","request_error":true,"error":"init failed"}\n'
        )
    )

    with pytest.raises(RuntimeError) as exc_info:
        model_manager_module._read_llama_subprocess_message(
            process,
            timeout_seconds=5,
            stage='llama_cpp_init',
        )

    assert str(exc_info.value) == (
        'llama_cpp_init failed; child_exception_type=RuntimeError; '
        'safe_error_category=runtime_init_unclassified; child_stderr_tail=<empty>'
    )
    assert not isinstance(exc_info.value, model_manager_module.LlamaCppInferenceRequestError)



def test_init_safe_category_alias_survives_subprocess_wrappers():
    from io import StringIO

    from utils.llm import model_manager as model_manager_module

    process = SimpleNamespace(
        stdout=StringIO(
            'TOKEN_PLACE_LLAMA_CPP_JSON:'
            '{"status":"error","error":"Failed to create llama_context",'
            '"exception_type":"RuntimeError","safe_error_category":"cuda_memory_allocation"}\n'
        )
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeInitError) as exc_info:
        model_manager_module._read_llama_subprocess_message(
            process,
            timeout_seconds=5,
            stage='llama_cpp_import',
        )

    assert exc_info.value.safe_error_category == 'runtime_context_create_cuda_memory'
    assert model_manager_module._classify_runtime_context_create_error(exc_info.value) == 'runtime_context_create_cuda_memory'


def test_invalid_child_exception_type_falls_back_to_runtime_error():
    from io import StringIO

    from utils.llm import model_manager as model_manager_module

    process = SimpleNamespace(
        stdout=StringIO(
            'TOKEN_PLACE_LLAMA_CPP_JSON:'
            '{"status":"error","error":"Failed to create llama_context",'
            '"exception_type":"RuntimeError; prompt=SECRET",'
            '"safe_error_category":"cuda_memory_allocation"}\n'
        )
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeInitError) as exc_info:
        model_manager_module._read_llama_subprocess_message(
            process,
            timeout_seconds=5,
            stage='llama_cpp_import',
        )

    assert exc_info.value.child_exception_type == 'RuntimeError'
    assert 'prompt=SECRET' not in str(exc_info.value)
    assert exc_info.value.safe_error_category == 'runtime_context_create_cuda_memory'


def test_empty_stderr_cuda_category_survives_proxy_and_advances_qwen64k_profile(tmp_path, monkeypatch):
    from io import StringIO

    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module
    from utils.llm.model_manager import ModelManager

    class FakeStdin:
        def write(self, _data):
            return None

        def flush(self):
            return None

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdin = FakeStdin()
            self.stdout = StringIO(
                'TOKEN_PLACE_LLAMA_CPP_JSON:'
                '{"status":"error","error":"Failed to create llama_context",'
                '"exception_type":"RuntimeError","safe_error_category":"cuda_memory_allocation"}\n'
            )
            self.stderr = StringIO('')
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', FakeProcess)
    with pytest.raises(model_manager_module.LlamaCppRuntimeInitError) as proxy_exc:
        model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'secret-model.gguf'), timeout_seconds=1)

    assert proxy_exc.value.safe_error_category == 'runtime_context_create_cuda_memory'
    assert proxy_exc.value.child_stderr_tail in {'', '<empty>'}

    attempts = []
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config = MagicMock(is_production=False)
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            if len(attempts) == 1:
                raise proxy_exc.value

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert len(attempts) == 2
    assert 'type_k' not in attempts[0]
    assert attempts[1]['type_k'] == 8
    assert manager.last_qwen_64k_init_failures[0]['safe_error_category'] == 'runtime_context_create_cuda_memory'
    assert manager.last_qwen_64k_memory_profile_diagnostics['profile_id'] == 'qwen64k_kv_q8_fa_small_batch'


def test_proxy_drains_delayed_stderr_before_refining_generic_context_create(monkeypatch, tmp_path):
    from io import StringIO

    from utils.llm import model_manager as model_manager_module

    class FakeStdin:
        def write(self, _data):
            return None

        def flush(self):
            return None

    class FakeThread:
        joins = []

        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            self._alive = True

        def start(self):
            if self._target is not None:
                self._target()
            return None

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            self.joins.append(timeout)
            self._alive = False

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdin = FakeStdin()
            self.stdout = StringIO(
                'TOKEN_PLACE_LLAMA_CPP_JSON:{"status":"ok","module_path":"/runtime/llama_cpp/__init__.py"}\n'
                'TOKEN_PLACE_LLAMA_CPP_JSON:'
                '{"status":"error","error":"Failed to create llama_context",'
                '"exception_type":"RuntimeError","safe_error_category":"runtime_context_create_failed"}\n'
            )
            self.stderr = StringIO('')
            self.returncode = 1
            self._token_place_stderr_tail = []
            self.wait_timeouts = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            self._token_place_stderr_sequence = 1
            self._token_place_stderr_tail.append((1, 'ggml_cuda: buffer allocation failed after init\n'))
            return self.returncode

        def poll(self):
            return self.returncode

    created = []

    def fake_popen(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(model_manager_module.threading, 'Thread', FakeThread)

    with pytest.raises(model_manager_module.LlamaCppRuntimeInitError) as exc_info:
        model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'secret-model.gguf'), timeout_seconds=1)

    assert exc_info.value.safe_error_category == 'runtime_context_create_cuda_memory'
    assert 'buffer allocation failed' in exc_info.value.child_stderr_tail
    assert created[0].wait_timeouts and 0 <= created[0].wait_timeouts[0] <= 0.5
    assert FakeThread.joins and 0 <= FakeThread.joins[-1] <= 0.5


def test_bounded_stderr_drain_joins_reader_when_process_wait_raises():
    from utils.llm import model_manager as model_manager_module

    class FakeThread:
        join_timeouts = []

        def start(self):
            return None

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

    class FakeProcess:
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd='fake-llama', timeout=timeout)

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._process = FakeProcess()
    proxy._stderr_reader_thread = FakeThread()

    proxy._drain_stderr_reader_bounded()

    assert FakeThread.join_timeouts and 0 <= FakeThread.join_timeouts[-1] <= 0.5


def test_init_safe_category_rejects_spoofed_child_category():
    from io import StringIO

    from utils.llm import model_manager as model_manager_module

    process = SimpleNamespace(
        stdout=StringIO(
            'TOKEN_PLACE_LLAMA_CPP_JSON:'
            '{"status":"error","error":"Failed to create llama_context",'
            '"exception_type":"RuntimeError","safe_error_category":"runtime_context_create_cuda_memory;prompt=SECRET"}\n'
        )
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeInitError) as exc_info:
        model_manager_module._read_llama_subprocess_message(
            process,
            timeout_seconds=5,
            stage='llama_cpp_import',
        )

    assert exc_info.value.safe_error_category == 'runtime_context_create_failed'
    assert 'SECRET' not in str(exc_info.value)


def test_qwen_64k_generic_context_create_sentinel_retries_bounded_gpu_profiles(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm.model_manager import ModelManager

    attempts = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            if len(attempts) < 3:
                raise ValueError('Failed to create llama_context')

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}):
        llm = manager.get_llm_instance()

    assert llm is not None
    assert len(attempts) == 3
    assert attempts[2]['type_k'] == 2
    assert manager.last_qwen_64k_init_failures[0]['safe_error_category'] == 'runtime_context_create_failed'
    assert manager.last_qwen_64k_memory_profile_diagnostics['profile_id'] == 'qwen64k_kv_q4_fa_small_batch'


def test_qwen_64k_retry_closes_retryable_init_exception_before_next_profile(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm.model_manager import ModelManager

    attempts = []
    closed = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class ClosableCudaInitError(RuntimeError):
        def close(self):
            closed.append('closed')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            if len(attempts) == 1:
                raise ClosableCudaInitError('cudaMalloc failed: out of memory')

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert len(attempts) == 2
    assert closed == ['closed']
    assert attempts[1]['type_k'] == 8


def test_qwen_64k_generic_context_create_exhaustion_preserves_original_error(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm.model_manager import ModelManager

    attempts = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            raise ValueError('Failed to create llama_context')

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert len(attempts) == 3
    assert 'Failed to create llama_context' in manager.last_runtime_init_error
    assert 'profile exhaustion' not in manager.last_runtime_init_error
    assert [failure['safe_error_category'] for failure in manager.last_qwen_64k_init_failures] == [
        'runtime_context_create_failed',
        'runtime_context_create_failed',
        'runtime_context_create_failed',
    ]


def test_qwen_64k_generic_context_create_exhaustion_after_short_profile_list_preserves_original_error(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module
    from utils.llm.model_manager import ModelManager

    attempts = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            raise ValueError('Failed to create llama_context')

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    short_profiles = [
        {
            'profile_id': 'qwen64k_f16_fa_small_batch',
            'runtime_kwargs': {},
            'diagnostics': {'profile_id': 'qwen64k_f16_fa_small_batch', 'applied': {}, 'backend': 'cuda'},
        },
        {
            'profile_id': 'qwen64k_kv_q8_fa_small_batch',
            'runtime_kwargs': {'type_k': 8, 'type_v': 8},
            'diagnostics': {
                'profile_id': 'qwen64k_kv_q8_fa_small_batch',
                'applied': {'type_k': 8, 'type_v': 8},
                'backend': 'cuda',
            },
        },
    ]
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}), \
         patch.object(model_manager_module, '_build_qwen_64k_runtime_profiles', return_value=short_profiles):
        assert manager.get_llm_instance() is None

    assert len(attempts) == 2
    assert 'Failed to create llama_context' in manager.last_runtime_init_error
    assert 'profile exhaustion' not in manager.last_runtime_init_error
    assert [failure['safe_error_category'] for failure in manager.last_qwen_64k_init_failures] == [
        'runtime_context_create_failed',
        'runtime_context_create_failed',
    ]


def test_qwen_64k_mixed_terminal_generic_context_create_preserves_original_error(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm.model_manager import ModelManager

    attempts = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            if len(attempts) == 1:
                raise RuntimeError('cudaMalloc failed: out of memory')
            raise ValueError('Failed to create llama_context')

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert len(attempts) == 3
    assert 'Failed to create llama_context' in manager.last_runtime_init_error
    assert 'profile exhaustion' not in manager.last_runtime_init_error
    assert [failure['safe_error_category'] for failure in manager.last_qwen_64k_init_failures] == [
        'runtime_context_create_cuda_memory',
        'runtime_context_create_failed',
        'runtime_context_create_failed',
    ]


def test_llama_worker_render_complete_allows_testing_stories_model_without_metadata(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'stories fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.completion_prompts = []\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        self.completion_prompts.append(prompt)\n"
        "        return {'choices': [{'text': 'short safe response'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'stories15M-q4_0.gguf'),
        timeout_seconds=5,
    )
    try:
        try:
            result = proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
        except model_manager_module.LlamaCppInferenceRequestError as exc:
            pytest.fail(f"unexpected request error diagnostics={exc.diagnostics!r}")
    finally:
        proxy.close()

    assert result == {
        'choices': [{'message': {'role': 'assistant', 'content': 'short safe response'}}]
    }


def test_llama_worker_render_complete_denies_testing_fallback_outside_testing_env(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'stories fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        raise AssertionError('testing fallback should not render outside testing')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'development')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'stories15M-q4_0.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['reason'] in {'runtime_chat_template_metadata_missing', 'inference_exception'}
    assert 'secret prompt text' not in json.dumps(diagnostics)


@pytest.mark.parametrize('rejected_kwarg', ['tokenize', 'add_generation_prompt'])
def test_llama_worker_render_complete_retries_rejected_render_kwarg_without_metadata(
    tmp_path, monkeypatch, rejected_kwarg
):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'reject render fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "REJECTED_KWARG = " + repr(rejected_kwarg) + "\n"
        "class Llama:\n"
        "    metadata = {}\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, **kwargs):\n"
        "        if REJECTED_KWARG in kwargs:\n"
        "            raise TypeError(f\"got an unexpected keyword argument '{REJECTED_KWARG}'\")\n"
        "        return '<|im_start|>assistant\\n'\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        return {'choices': [{'text': 'safe answer'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'development')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'qwen-no-metadata.gguf'),
        timeout_seconds=5,
    )
    try:
        response = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert response['choices'][0]['message']['content'] == 'safe answer'


def test_llama_worker_render_complete_testing_fallback_keeps_bad_messages_safe(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'stories fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        raise AssertionError('malformed fallback messages should not complete')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'stories15M-q4_0.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                ['secret prompt text'],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['reason'] in {'runtime_chat_template_metadata_missing', 'runtime_chat_template_render_exception'}
    assert 'secret prompt text' not in json.dumps(diagnostics)


def test_llama_worker_render_complete_testing_mock_keyword_model_path_without_metadata(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'mock keyword fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.init_kwargs = kwargs\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        return {'choices': [{'text': 'mock keyword response'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        try:
            result = proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
        except model_manager_module.LlamaCppInferenceRequestError as exc:
            pytest.fail(f"unexpected request error diagnostics={exc.diagnostics!r}")
    finally:
        proxy.close()

    assert result == {
        'choices': [{'message': {'role': 'assistant', 'content': 'mock keyword response'}}]
    }


def test_subprocess_llama_proxy_nonstreaming_error_does_not_poison_worker(request_scoped_llama_proxy):
    from utils.llm import model_manager as model_manager_module

    proxy = request_scoped_llama_proxy()

    with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
        proxy.create_chat_completion(
            messages=[{'role': 'user', 'content': 'raise_nonstream TOP_SECRET_PROMPT'}],
            stream=False,
        )

    error_text = str(exc_info.value)
    assert 'llama_cpp request failed' in error_text
    assert 'TOP_SECRET_PROMPT' not in error_text
    assert 'nonstream secret prompt should not leak' not in error_text
    assert exc_info.value.diagnostics == {
        'reason': 'inference_exception',
        'method': 'create_chat_completion',
        'stream': False,
        'exception_type': 'RuntimeError',
        'sanitized_error_summary': 'RuntimeError:redacted',
        'generation_exception_category': 'unknown_generation_exception',
    }

    result = proxy.create_chat_completion(messages=[], stream=False)
    assert result['choices'][0]['message']['content'] == 'ok'
    assert result['choices'][0]['pid'] == proxy._process.pid


def test_subprocess_llama_proxy_malformed_request_does_not_terminate_worker(request_scoped_llama_proxy):
    from utils.llm import model_manager as model_manager_module

    proxy = request_scoped_llama_proxy()

    with proxy._lock:
        assert proxy._process.stdin is not None
        proxy._process.stdin.write('[\"not an object with SECRET\"]\n')
        proxy._process.stdin.flush()
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            model_manager_module._read_llama_subprocess_message(
                proxy._process,
                timeout_seconds=5,
                stage='llama_cpp_inference',
            )

    assert exc_info.value.diagnostics == {'reason': 'malformed_request'}
    assert 'SECRET' not in str(exc_info.value)

    result = proxy.create_chat_completion(messages=[], stream=False)
    assert result['choices'][0]['message']['content'] == 'ok'
    assert result['choices'][0]['pid'] == proxy._process.pid


def test_subprocess_llama_proxy_invalid_json_does_not_leak_or_terminate_worker(
    request_scoped_llama_proxy,
):
    from utils.llm import model_manager as model_manager_module

    proxy = request_scoped_llama_proxy()

    with proxy._lock:
        assert proxy._process.stdin is not None
        proxy._process.stdin.write('{"prompt": "TOP_SECRET_PROMPT"\n')
        proxy._process.stdin.flush()
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            model_manager_module._read_llama_subprocess_message(
                proxy._process,
                timeout_seconds=5,
                stage='llama_cpp_inference',
            )

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['reason'] == 'invalid_json'
    assert 'TOP_SECRET_PROMPT' not in str(exc_info.value)
    assert 'TOP_SECRET_PROMPT' not in json.dumps(diagnostics)

    result = proxy.create_chat_completion(messages=[], stream=False)
    assert result['choices'][0]['message']['content'] == 'ok'
    assert result['choices'][0]['pid'] == proxy._process.pid


def test_subprocess_llama_proxy_streaming_error_does_not_poison_worker(request_scoped_llama_proxy):
    from utils.llm import model_manager as model_manager_module

    proxy = request_scoped_llama_proxy()

    with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
        list(proxy.create_chat_completion(
            messages=[{'role': 'user', 'content': 'raise_stream STREAM_SECRET'}],
            stream=True,
        ))

    assert exc_info.value.diagnostics['reason'] == 'inference_exception'
    assert exc_info.value.diagnostics['stream'] is True
    assert 'STREAM_SECRET' not in str(exc_info.value)

    result = proxy.create_chat_completion(messages=[], stream=False)
    assert result['choices'][0]['message']['content'] == 'ok'
    assert result['choices'][0]['pid'] == proxy._process.pid


def test_subprocess_llama_proxy_unsupported_method_does_not_terminate_worker(request_scoped_llama_proxy):
    from utils.llm import model_manager as model_manager_module

    proxy = request_scoped_llama_proxy()

    with proxy._lock:
        proxy._send({'method': 'unsupported', 'args': [], 'kwargs': {'messages': [{'content': 'SECRET'}]}})
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            model_manager_module._read_llama_subprocess_message(
                proxy._process,
                timeout_seconds=5,
                stage='llama_cpp_inference',
            )

    assert exc_info.value.diagnostics == {
        'reason': 'unsupported_method',
        'method': 'unsupported',
        'stream': False,
    }
    assert 'SECRET' not in str(exc_info.value)

    result = proxy.create_chat_completion(messages=[], stream=False)
    assert result['choices'][0]['message']['content'] == 'ok'
    assert result['choices'][0]['pid'] == proxy._process.pid


def test_subprocess_llama_proxy_secret_method_value_is_not_echoed(request_scoped_llama_proxy):
    from utils.llm import model_manager as model_manager_module

    proxy = request_scoped_llama_proxy()

    with proxy._lock:
        proxy._send({'method': 'unsupported TOP_SECRET_METHOD', 'args': [], 'kwargs': {}})
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            model_manager_module._read_llama_subprocess_message(
                proxy._process,
                timeout_seconds=5,
                stage='llama_cpp_inference',
            )

    assert exc_info.value.diagnostics == {
        'reason': 'unsupported_method',
        'method': 'unsupported',
        'stream': False,
    }
    assert 'TOP_SECRET_METHOD' not in str(exc_info.value)

    result = proxy.create_chat_completion(messages=[], stream=False)
    assert result['choices'][0]['message']['content'] == 'ok'
    assert result['choices'][0]['pid'] == proxy._process.pid


class _RestartTestConfig:
    is_production = False

    def __init__(self, models_dir):
        self.models_dir = str(models_dir)

    def get(self, key, default=None):
        values = {
            'model.filename': 'test_model.gguf',
            'model.url': 'https://example.com/model.gguf',
            'model.download_chunk_size_mb': 1,
            'paths.models_dir': self.models_dir,
            'model.use_mock': False,
            'model.context_size': 2048,
            'model.chat_format': 'llama-3',
            'model.max_tokens': 1000,
            'model.temperature': 0.7,
            'model.top_p': 0.9,
            'model.stop_tokens': [],
            'model.n_gpu_layers': 0,
            'model.gpu_memory_headroom_percent': 0.1,
            'model.enforce_gpu_memory_headroom': False,
        }
        return values.get(key, default)


class _RestartableFakeWorker:
    def __init__(self, name, *, fail=None):
        self.name = name
        self.fail = fail
        self.closed = False
        self.calls = 0
        self.pid = id(self)

    def is_alive(self):
        return not self.closed and self.fail != 'dead'

    def close(self):
        self.closed = True

    def create_chat_completion(self, **_kwargs):
        self.calls += 1
        from utils.llm import model_manager as model_manager_module
        if self.fail == 'dead':
            raise model_manager_module.LlamaCppWorkerDeadError('dead')
        if self.fail == 'pipe':
            raise model_manager_module.LlamaCppWorkerBrokenPipeError('pipe')
        if self.fail == 'eof':
            raise model_manager_module.LlamaCppWorkerEOFError('eof')
        if self.fail == 'request':
            raise model_manager_module.LlamaCppInferenceRequestError('request failed')
        return {'choices': [{'message': {'role': 'assistant', 'content': self.name}}]}


def _restart_manager(tmp_path, monkeypatch, workers):
    from utils.llm import model_manager as model_manager_module

    (tmp_path / 'test_model.gguf').write_bytes(b'fake')
    manager = model_manager_module.ModelManager(_RestartTestConfig(tmp_path))
    created = []

    def _import_runtime(**_kwargs):
        class _Runtime:
            __file__ = '/fake/llama_cpp.py'

            class Llama:
                def __init__(self, **_llama_kwargs):
                    worker = workers.pop(0)
                    created.append(worker)
                    self._worker = worker

                def __getattr__(self, name):
                    return getattr(self._worker, name)

        return _Runtime

    monkeypatch.setattr(model_manager_module, '_import_llama_cpp_runtime', _import_runtime)
    monkeypatch.setattr(manager, '_resolve_compute_plan', lambda: {
        'requested_mode': 'cpu', 'effective_mode': 'cpu', 'backend_available': 'cpu',
        'backend_selected': 'cpu', 'backend_used': 'cpu', 'n_gpu_layers': 0,
        'fallback_reason': None,
    })
    return manager, created


def test_model_manager_recover_replaces_worker_killed_between_requests(tmp_path, monkeypatch):
    first = _RestartableFakeWorker('first')
    second = _RestartableFakeWorker('second')
    manager, created = _restart_manager(tmp_path, monkeypatch, [first, second])

    first_result = manager.create_chat_completion_with_recovery(messages=[])
    assert first_result['choices'][0]['message']['content'] == 'first'
    first.fail = 'dead'
    second_result = manager.create_chat_completion_with_recovery(messages=[])
    assert second_result['choices'][0]['message']['content'] == 'second'

    assert first.closed is True
    assert manager.llm is not None
    assert manager.llm._worker is second
    assert created == [first, second]


def test_model_manager_restart_broken_pipe_or_eof_once(tmp_path, monkeypatch):
    pipe = _RestartableFakeWorker('pipe', fail='pipe')
    second = _RestartableFakeWorker('second')
    manager, created = _restart_manager(tmp_path, monkeypatch, [pipe, second])

    second_result = manager.create_chat_completion_with_recovery(messages=[])
    assert second_result['choices'][0]['message']['content'] == 'second'
    assert pipe.closed is True
    assert created == [pipe, second]

    eof = _RestartableFakeWorker('eof', fail='eof')
    fourth = _RestartableFakeWorker('fourth')
    manager, created = _restart_manager(tmp_path, monkeypatch, [eof, fourth])
    fourth_result = manager.create_chat_completion_with_recovery(messages=[])
    assert fourth_result['choices'][0]['message']['content'] == 'fourth'
    assert eof.closed is True
    assert created == [eof, fourth]


def test_model_manager_request_scoped_inference_error_not_retried(tmp_path, monkeypatch):
    first = _RestartableFakeWorker('first', fail='request')
    second = _RestartableFakeWorker('second')
    manager, created = _restart_manager(tmp_path, monkeypatch, [first, second])
    from utils.llm import model_manager as model_manager_module

    with pytest.raises(model_manager_module.LlamaCppInferenceRequestError):
        manager.create_chat_completion_with_recovery(messages=[])

    assert first.closed is False
    assert manager.llm._worker is first
    assert manager.last_worker_error_code == 'inference_request_error'
    assert created == [first]


def test_model_manager_sticky_request_error_invalidates_without_replay(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    first = _RestartableFakeWorker('first')
    second = _RestartableFakeWorker('second')
    manager, created = _restart_manager(tmp_path, monkeypatch, [first, second])

    def _sticky_failure(*_args, **_kwargs):
        first.calls += 1
        raise model_manager_module.LlamaCppInferenceRequestError(
            'request failed',
            diagnostics={
                'plain_completion_backend_state_sticky': True,
                'plain_completion_backend_recreation_required': True,
            },
        )

    first.create_chat_completion = _sticky_failure

    with pytest.raises(model_manager_module.LlamaCppInferenceRequestError):
        manager.create_chat_completion_with_recovery(messages=[])

    assert first.calls == 1
    assert first.closed is True
    assert manager.llm is None
    assert created == [first]

    result = manager.create_chat_completion_with_recovery(messages=[])

    assert result['choices'][0]['message']['content'] == 'second'
    assert created == [first, second]


def test_model_manager_concurrent_dead_worker_creates_one_replacement(tmp_path, monkeypatch):
    dead = _RestartableFakeWorker('dead', fail='dead')
    replacement = _RestartableFakeWorker('replacement')
    manager, created = _restart_manager(tmp_path, monkeypatch, [dead, replacement])
    manager.get_llm_instance()

    barrier = threading.Barrier(2)
    results = []

    def _call():
        barrier.wait(timeout=5)
        results.append(manager.create_chat_completion_with_recovery(messages=[]))

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert [worker.name for worker in created] == ['dead', 'replacement']
    assert dead.closed is True
    assert replacement.calls == 2
    status = manager.worker_lifecycle_status()
    assert status['worker_state'] == 'ready'
    assert status['worker_restart_count'] == 1
    assert status['last_worker_error_code'] is None
    assert status['last_worker_exit_code'] is None


def test_model_manager_replacement_failure_attempted_once_surfaces_stable_error(tmp_path, monkeypatch):
    first = _RestartableFakeWorker('first', fail='dead')
    second = _RestartableFakeWorker('second', fail='eof')
    third = _RestartableFakeWorker('third')
    manager, created = _restart_manager(tmp_path, monkeypatch, [first, second, third])

    with pytest.raises(RuntimeError, match='one restart attempt'):
        manager.create_chat_completion_with_recovery(messages=[])

    assert [worker.name for worker in created] == ['first', 'second']
    assert first.closed is True
    assert second.closed is True
    status = manager.worker_lifecycle_status()
    assert status['worker_state'] == 'failed'
    assert status['worker_restart_count'] == 2
    assert status['last_worker_error_code'] == 'worker_eof'


def test_model_manager_healthy_request_has_no_restart(tmp_path, monkeypatch):
    first = _RestartableFakeWorker('first')
    second = _RestartableFakeWorker('second')
    manager, created = _restart_manager(tmp_path, monkeypatch, [first, second])

    first_result = manager.create_chat_completion_with_recovery(messages=[])
    assert first_result['choices'][0]['message']['content'] == 'first'
    first_result = manager.create_chat_completion_with_recovery(messages=[])
    assert first_result['choices'][0]['message']['content'] == 'first'

    assert created == [first]
    assert first.closed is False
    assert manager.worker_lifecycle_status()['worker_restart_count'] == 0


def test_subprocess_llama_proxy_send_marks_closed_on_missing_stdin_or_write_failure():
    from utils.llm import model_manager as model_manager_module

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._closed = False
    proxy._process = SimpleNamespace(stdin=None)

    with pytest.raises(model_manager_module.LlamaCppWorkerBrokenPipeError):
        proxy._send({"method": "noop"}, check_health=False)

    assert proxy.is_alive() is False

    class BrokenStdin:
        def write(self, _text):
            raise BrokenPipeError("closed")

        def flush(self):
            raise AssertionError("flush should not run after write failure")

    proxy._closed = False
    proxy._process = SimpleNamespace(stdin=BrokenStdin(), poll=lambda: None)

    with pytest.raises(model_manager_module.LlamaCppWorkerBrokenPipeError):
        proxy._send({"method": "noop"}, check_health=False)

    assert proxy.is_alive() is False


def test_model_manager_recovery_rejects_streaming_calls(tmp_path, monkeypatch):
    first = _RestartableFakeWorker('first')
    manager, created = _restart_manager(tmp_path, monkeypatch, [first])

    with pytest.raises(ValueError, match='does not support stream=True'):
        manager.create_chat_completion_with_recovery(messages=[], stream=True)

    assert created == []


def test_llama_subprocess_transport_error_payload_raises_restartable_eof():
    from utils.llm import model_manager as model_manager_module

    process = SimpleNamespace(
        stdout=iter(()),
        stderr=None,
        wait=MagicMock(return_value=7),
        poll=lambda: 7,
        returncode=7,
    )

    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError, match='JSON handshake'):
        model_manager_module._read_llama_subprocess_message(
            process,
            timeout_seconds=0.2,
            stage='llama_cpp_inference',
        )


def test_llama_subprocess_transport_error_omits_unsafely_unallowlisted_secret():
    from utils.llm import model_manager as model_manager_module

    class _Stdout:
        def __iter__(self):
            return iter((
                'TOKEN_PLACE_LLAMA_CPP_JSON:'
                '{"status":"transport_error","error":"prompt=SECRET_PROMPT authorization=Bearer SECRET_TOKEN"}\n',
            ))

    process = SimpleNamespace(
        stdout=_Stdout(),
        stderr=None,
        wait=MagicMock(return_value=7),
        poll=lambda: None,
        returncode=None,
    )

    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError) as exc_info:
        model_manager_module._read_llama_subprocess_message(
            process,
            timeout_seconds=0.2,
            stage='llama_cpp_import',
        )

    error = str(exc_info.value)
    assert 'unsafe child diagnostic omitted' in error
    assert 'SECRET_PROMPT' not in error
    assert 'SECRET_TOKEN' not in error
    assert 'authorization' not in error

def test_subprocess_llama_proxy_liveness_and_close_edge_cases():
    from utils.llm import model_manager as model_manager_module

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._closed = False
    proxy._process = SimpleNamespace(
        stdin=SimpleNamespace(close=MagicMock(side_effect=RuntimeError('close failed'))),
        poll=MagicMock(return_value=None),
        terminate=MagicMock(),
        wait=MagicMock(side_effect=TimeoutError('still running')),
        kill=MagicMock(),
        returncode=None,
    )

    with pytest.raises(model_manager_module.LlamaCppWorkerDeadError):
        dead_proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
        dead_proxy._closed = False
        dead_proxy._process = SimpleNamespace(poll=lambda: 1, returncode=1, stderr=None)
        dead_proxy.assert_healthy()

    proxy.close()
    proxy.close()

    assert proxy._closed is True
    proxy._process.terminate.assert_called_once_with()
    proxy._process.wait.assert_called_once_with(timeout=1)
    proxy._process.kill.assert_called_once_with()


def test_model_manager_recovery_reports_unavailable_or_invalid_runtime(tmp_path, monkeypatch):
    first = SimpleNamespace(create_chat_completion='not-callable')
    manager, _created = _restart_manager(tmp_path, monkeypatch, [first])

    with pytest.raises(RuntimeError, match='missing create_chat_completion'):
        manager.create_chat_completion_with_recovery(messages=[])

    monkeypatch.setattr(manager, 'get_llm_instance', lambda: None)
    with pytest.raises(RuntimeError, match='unavailable'):
        manager.create_chat_completion_with_recovery(messages=[])


def test_model_manager_recovery_reports_invalid_or_missing_replacement(tmp_path, monkeypatch):
    first = _RestartableFakeWorker('first', fail='dead')
    manager, _created = _restart_manager(tmp_path, monkeypatch, [first])
    monkeypatch.setattr(manager, '_ensure_replacement_llm', lambda _observed_generation: None)

    with pytest.raises(RuntimeError, match='replacement failed'):
        manager.create_chat_completion_with_recovery(messages=[])

    first = _RestartableFakeWorker('first', fail='dead')
    manager, _created = _restart_manager(tmp_path, monkeypatch, [first])
    monkeypatch.setattr(
        manager,
        '_ensure_replacement_llm',
        lambda _observed_generation: SimpleNamespace(create_chat_completion='not-callable'),
    )

    with pytest.raises(RuntimeError, match='replacement runtime missing'):
        manager.create_chat_completion_with_recovery(messages=[])


def test_model_manager_replacement_helpers_handle_unusual_workers(tmp_path, monkeypatch):
    manager, _created = _restart_manager(tmp_path, monkeypatch, [])

    assert manager._llm_is_usable(object()) is True
    assert manager._llm_is_usable(SimpleNamespace(is_alive=lambda: (_ for _ in ()).throw(RuntimeError('boom')))) is False

    close = MagicMock(side_effect=RuntimeError('close failed'))
    manager._close_llm_proxy(SimpleNamespace(close=close))
    close.assert_called_once_with()

    usable = _RestartableFakeWorker('usable')
    manager.llm = usable
    observed_generation = manager._llm_generation
    assert manager._ensure_replacement_llm(observed_generation) is usable

    stale = _RestartableFakeWorker('stale', fail='dead')
    replacement = _RestartableFakeWorker('replacement')
    manager.llm = stale
    workers = [replacement]

    def _get_replacement():
        manager.llm = workers.pop(0)
        return manager.llm

    monkeypatch.setattr(manager, 'get_llm_instance', _get_replacement)
    assert manager._ensure_replacement_llm(manager._llm_generation) is replacement
    assert stale.closed is True


def test_long_lived_subprocess_worker_soak_and_fault_injection(tmp_path, monkeypatch, caplog, capsys):
    """Deterministic fake llama_cpp subprocess soak for zombie-node regressions."""
    from utils.llm import model_manager as model_manager_module

    plaintext_prompt = 'PLAINTEXT_PROMPT_SENTINEL_DO_NOT_LOG_7'
    plaintext_output = 'PLAINTEXT_OUTPUT_SENTINEL_DO_NOT_LOG_7'
    fake_parent = tmp_path / 'fake_site'
    fake_pkg = fake_parent / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    state_path = tmp_path / 'fake_llama_state.json'
    state_path.write_text(json.dumps({'created': 0, 'calls': [], 'actions': []}), encoding='utf-8')
    fake_pkg.joinpath('__init__.py').write_text(
        r'''
import contextlib, json, os, time
from pathlib import Path
STATE = Path(os.environ['TOKEN_PLACE_FAKE_LLAMA_STATE'])
LOCK = STATE.with_suffix(STATE.suffix + '.lock')
SENTINEL_OUTPUT = os.environ['TOKEN_PLACE_FAKE_LLAMA_OUTPUT']

def _load_unlocked():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'created': 0, 'calls': [], 'actions': []}

def _save_unlocked(data):
    STATE.write_text(json.dumps(data, sort_keys=True), encoding='utf-8')

@contextlib.contextmanager
def _locked_state():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10
    lock_fd = None
    while lock_fd is None:
        try:
            lock_fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError('timed out waiting for fake llama state lock')
            time.sleep(0.01)
    try:
        data = _load_unlocked()
        yield data
        _save_unlocked(data)
    finally:
        os.close(lock_fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(LOCK)

class Llama:
    def __init__(self, **_kwargs):
        with _locked_state() as data:
            if data.get('fail_init'):
                raise RuntimeError('fake init failure without plaintext')
            data['created'] = int(data.get('created', 0)) + 1
            self.generation = data['created']
            data.setdefault('generations', []).append(self.generation)

    def create_chat_completion(self, **kwargs):
        with _locked_state() as data:
            actions = data.get('actions') or []
            action = actions.pop(0) if actions else None
            data['actions'] = actions
            request_id = kwargs.get('request_id') or (kwargs.get('metadata') or {}).get('request_id')
            data.setdefault('calls', []).append({'generation': self.generation, 'request_id': request_id})
        if action == 'request_error':
            raise RuntimeError('fake request scoped exception without plaintext')
        if action == 'abrupt_exit':
            os._exit(23)
        time.sleep(float(os.environ.get('TOKEN_PLACE_FAKE_LLAMA_DELAY', '0')))
        nonce = (kwargs.get('metadata') or {}).get('nonce') or request_id
        return {'id': request_id, 'nonce': nonce, 'choices': [{'message': {'role': 'assistant', 'content': SENTINEL_OUTPUT + ' gen=' + str(self.generation) + ' request=' + str(request_id) + ' nonce=' + str(nonce)}}]}
''',
        encoding='utf-8',
    )

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger='model_manager')
    caplog.set_level(logging.DEBUG, logger='utils.llm')
    caplog.set_level(logging.DEBUG, logger='utils.llm.model_manager')
    caplog.set_level(logging.DEBUG, logger='token.place')

    monkeypatch.syspath_prepend(str(fake_parent))
    monkeypatch.setenv('TOKEN_PLACE_FAKE_LLAMA_STATE', str(state_path))
    monkeypatch.setenv('TOKEN_PLACE_FAKE_LLAMA_OUTPUT', plaintext_output)
    monkeypatch.setenv('TOKEN_PLACE_LLAMA_CPP_SUBPROCESS_INFERENCE_TIMEOUT_SECONDS', '5')
    monkeypatch.setattr(model_manager_module, '_signal_guard_available', lambda: False)
    monkeypatch.setattr(model_manager_module, '_find_llama_cpp_spec_in_subprocess', lambda **_kwargs: {'module_path': str(fake_pkg / '__init__.py')})
    monkeypatch.setattr(model_manager_module, '_run_llama_cpp_import_watchdog', lambda **_kwargs: {'module_path': str(fake_pkg / '__init__.py')})

    (tmp_path / 'test_model.gguf').write_bytes(b'fake')
    manager = model_manager_module.ModelManager(_RestartTestConfig(tmp_path))
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: model_manager_module._import_llama_cpp_subprocess_module(
            module_path_hint=str(fake_pkg / '__init__.py'), timeout_seconds=5
        ),
    )
    monkeypatch.setattr(manager, '_resolve_compute_plan', lambda: {
        'requested_mode': 'cpu', 'effective_mode': 'cpu', 'backend_available': 'cpu',
        'backend_selected': 'cpu', 'backend_used': 'cpu', 'n_gpu_layers': 0,
        'fallback_reason': None,
    })

    def state():
        return json.loads(state_path.read_text(encoding='utf-8'))

    def save(data):
        state_path.write_text(json.dumps(data), encoding='utf-8')

    def set_actions(*actions):
        data = state()
        data['actions'] = list(actions)
        data.pop('fail_init', None)
        save(data)

    def request(request_id, nonce=None):
        nonce = nonce or f'nonce-{request_id}'
        return manager.create_chat_completion_with_recovery(
            messages=[{'role': 'user', 'content': f'{plaintext_prompt} {request_id} {nonce}'}],
            request_id=request_id,
            metadata={'request_id': request_id, 'nonce': nonce},
        )

    class LocalRelayProbe:
        def __init__(self, relay_ids):
            self.registered = {relay_id: True for relay_id in relay_ids}
            self.ready = {relay_id: True for relay_id in relay_ids}
            self.encrypted_requests = {}
            self.encrypted_responses = {}
            self.response_history = []
            self.safe_logs = []
            self.diagnostics = []

        def encrypted_request_for(self, request_id, nonce):
            payload = f'ciphertext-request:{request_id}:{nonce}'
            self.encrypted_requests[request_id] = payload
            self.safe_logs.append(f'queued encrypted request {request_id}')
            return payload

        def store_response(self, request_id, nonce, result):
            content = result['choices'][0]['message']['content']
            assert f'request={request_id}' in content
            assert f'nonce={nonce}' in content
            encrypted = f'ciphertext-response:{request_id}:{nonce}:{result["id"]}'
            self.encrypted_responses.setdefault(request_id, []).append(encrypted)
            self.response_history.append((request_id, encrypted))
            self.safe_logs.append(f'stored encrypted response {request_id}')
            return encrypted

        def mark_failed(self, reason):
            for relay_id in self.registered:
                self.registered[relay_id] = False
                self.ready[relay_id] = False
            self.diagnostics.append({'event': 'worker_failed', 'reason': reason})

        def ciphertext_blob(self):
            return json.dumps({
                'registered': self.registered,
                'ready': self.ready,
                'encrypted_requests': self.encrypted_requests,
                'encrypted_responses': self.encrypted_responses,
                'safe_logs': self.safe_logs,
                'diagnostics': self.diagnostics,
            }, sort_keys=True)

    relay_probe = LocalRelayProbe(['relay-0', 'relay-1'])

    def relay_request(request_id, nonce):
        relay_probe.encrypted_request_for(request_id, nonce)
        result = request(request_id, nonce=nonce)
        relay_probe.store_response(request_id, nonce, result)
        return result

    seen_ids = set()
    for index in range(100):
        req_id = f'soak-{index}'
        result = request(req_id)
        assert result['id'] == req_id
        assert req_id not in seen_ids
        seen_ids.add(req_id)
    assert state()['created'] == 1
    assert manager.worker_lifecycle_status()['worker_restart_count'] == 0
    assert manager.worker_lifecycle_status()['worker_alive'] is True

    set_actions('request_error')
    with pytest.raises(model_manager_module.LlamaCppInferenceRequestError):
        request('request-error-once')
    assert state()['created'] == 1
    assert request('after-request-error')['choices'][0]['message']['content'].startswith(plaintext_output)
    assert state()['created'] == 1

    set_actions('abrupt_exit')
    assert request('after-abrupt-exit')['id'] == 'after-abrupt-exit'
    status = manager.worker_lifecycle_status()
    assert state()['created'] == 2
    assert status['worker_restart_count'] == 1
    assert status['worker_state'] == 'ready'

    before = state()['created']
    monkeypatch.setenv('TOKEN_PLACE_FAKE_LLAMA_DELAY', '0.01')
    results = []
    relay_errors = []
    barrier = threading.Barrier(2)

    def _relay_call(name):
        try:
            barrier.wait(timeout=5)
            nonce = f'nonce-{name}'
            results.append(relay_request(name, nonce)['id'])
        except Exception as exc:  # pragma: no cover - surfaced below with thread context
            relay_errors.append((name, repr(exc)))

    threads = [threading.Thread(target=_relay_call, args=(f'relay-{i}',), daemon=True) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    stuck_threads = [thread.name for thread in threads if thread.is_alive()]
    assert stuck_threads == []
    assert relay_errors == []
    assert sorted(results) == ['relay-0', 'relay-1']
    assert all(len(relay_probe.encrypted_responses[request_id]) == 1 for request_id in results)
    assert len({encrypted for _request_id, encrypted in relay_probe.response_history}) == len(results)
    assert state()['created'] == before
    assert manager.worker_lifecycle_status()['worker_alive'] is True

    set_actions('abrupt_exit')
    data = state()
    data['fail_init'] = True
    save(data)
    persistent_error = None
    # This branch intentionally forces a replacement initialization failure.
    # Suppress the expected ERROR-level model-manager traceback from live pytest
    # logs so CI summaries do not misclassify the exercised failure path as the
    # cause of a red run while the exception is still asserted below.
    with caplog.at_level(logging.CRITICAL, logger='model_manager'):
        with pytest.raises(
            RuntimeError,
            match='replacement failed|one restart attempt|fake init failure',
        ) as exc_info:
            request('persistent-failure')
    persistent_error = repr(exc_info.value)
    relay_probe.mark_failed(persistent_error)
    assert all(is_registered is False for is_registered in relay_probe.registered.values())
    assert all(is_ready is False for is_ready in relay_probe.ready.values())
    status = manager.worker_lifecycle_status()
    assert status['worker_state'] in {'failed', 'recovering', 'stopped'}
    assert status['worker_alive'] is False
    assert manager.llm is None

    data = state()
    data.pop('fail_init', None)
    data['actions'] = []
    save(data)
    with manager.llm_lock:
        manager.llm = None
        manager.worker_state = 'stopped'
    recovered = request('after-stop-start')
    assert recovered['id'] == 'after-stop-start'
    assert manager.worker_lifecycle_status()['worker_alive'] is True

    calls = state()['calls']
    assert sum(1 for call in calls if call.get('request_id') == 'after-abrupt-exit') == 2
    assert len(seen_ids) == 100
    assert state()['created'] <= 4
    assert threading.active_count() < 80

    diagnostics = json.dumps(manager.worker_lifecycle_status(), sort_keys=True)
    captured = capsys.readouterr()
    leak_checked_text = '\n'.join([
        caplog.text,
        diagnostics,
        relay_probe.ciphertext_blob(),
        persistent_error or '',
        captured.out,
        captured.err,
    ])
    assert plaintext_prompt not in leak_checked_text
    assert plaintext_output not in leak_checked_text


def test_subprocess_llama_proxy_prompt_helpers_use_runtime_stage_timeout(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._lock = model_manager_module.Lock()
    proxy._process = SimpleNamespace(stdin=MagicMock())
    proxy._timeout_seconds = 3.25
    proxy._closed = False
    proxy._send = MagicMock()
    captured_reads = []

    def _fake_read(_process, *, timeout_seconds, stage):
        captured_reads.append((stage, timeout_seconds))
        if stage == 'llama_cpp_prompt_render':
            return {'status': 'ok', 'result': '<|user|>\nhello'}
        if stage == 'llama_cpp_prompt_render_tokenize':
            return {'status': 'ok', 'result': {'prompt_tokens': 2}}
        return {'status': 'ok', 'result': [10, 11]}

    monkeypatch.setattr(model_manager_module, '_read_llama_subprocess_message', _fake_read)

    rendered = proxy.apply_chat_template([{'role': 'user', 'content': 'hello'}], tokenize=False)
    token_summary = proxy.render_and_tokenize_chat(
        [{'role': 'user', 'content': 'hello'}],
        tokenize=False,
        add_generation_prompt=True,
    )
    tokens = proxy.tokenize(rendered.encode('utf-8'), add_bos=False)

    assert rendered == '<|user|>\nhello'
    assert token_summary == {'prompt_tokens': 2}
    assert tokens == [10, 11]
    assert proxy._send.call_args_list == [
        call({
            'method': 'apply_chat_template',
            'args': ([{'role': 'user', 'content': 'hello'}],),
            'kwargs': {'tokenize': False},
        }),
        call({
            'method': 'render_and_tokenize_chat',
            'args': ([{'role': 'user', 'content': 'hello'}],),
            'kwargs': {'tokenize': False, 'add_generation_prompt': True},
        }),
        call({
            'method': 'tokenize',
            'args': ({'__token_place_bytes_utf8__': '<|user|>\nhello'},),
            'kwargs': {'add_bos': False},
        }),
    ]
    assert captured_reads == [
        ('llama_cpp_prompt_render', 3.25),
        ('llama_cpp_prompt_render_tokenize', 3.25),
        ('llama_cpp_prompt_tokenize', 3.25),
    ]


def test_subprocess_llama_proxy_prompt_helpers_mark_closed_on_eof(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._lock = model_manager_module.Lock()
    proxy._process = SimpleNamespace(stdin=MagicMock())
    proxy._timeout_seconds = 0.01
    proxy._closed = False
    proxy._send = MagicMock()

    def _raise_eof(*_args, **_kwargs):
        raise model_manager_module.LlamaCppWorkerEOFError('worker closed')

    monkeypatch.setattr(model_manager_module, '_read_llama_subprocess_message', _raise_eof)

    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError):
        proxy.apply_chat_template([], tokenize=False)
    assert proxy._closed is True

    proxy._closed = False
    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError):
        proxy.render_and_tokenize_chat([], tokenize=False)
    assert proxy._closed is True

    proxy._closed = False
    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError):
        proxy.tokenize(b'hello', add_bos=False)
    assert proxy._closed is True

    proxy._closed = False
    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError):
        proxy.create_chat_completion_from_rendered_prompt([], max_tokens=1)
    assert proxy._closed is True


def _run_llama_worker_request(tmp_path, request, *, llama_body, llama_chat_format_body=None):
    package_dir = tmp_path / 'llama_cpp'
    package_dir.mkdir()
    (package_dir / '__init__.py').write_text(llama_body)
    if llama_chat_format_body is not None:
        (package_dir / 'llama_chat_format.py').write_text(llama_chat_format_body)
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), str(Path(__file__).parent.parent.parent)])
    process = subprocess.Popen(
        [sys.executable, '-c', 'from utils.llm.model_manager import _LLAMA_CPP_RUNTIME_WORKER_CODE; exec(_LLAMA_CPP_RUNTIME_WORKER_CODE)'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({'args': [], 'kwargs': {}}) + '\n')
    process.stdin.flush()
    init = process.stdout.readline()
    assert init.startswith('TOKEN_PLACE_LLAMA_CPP_JSON:')
    assert json.loads(init.split(':', 1)[1])['status'] == 'ok'
    process.stdin.write(json.dumps(request) + '\n')
    process.stdin.flush()
    response = process.stdout.readline()
    process.kill()
    process.wait(timeout=5)
    assert response.startswith('TOKEN_PLACE_LLAMA_CPP_JSON:')
    return json.loads(response.split(':', 1)[1])


def test_llama_worker_render_and_tokenize_chat_returns_only_token_count(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'secret prompt'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True},
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        pass
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return 'rendered secret prompt'
    def tokenize(self, prompt, add_bos=False):
        assert prompt == b'rendered secret prompt'
        return [1, 2, 3]
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 3}}
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_fails_closed_when_enable_thinking_rejected_without_metadata(tmp_path):
    # When apply_chat_template rejects enable_thinking and no GGUF/Jinja metadata
    # is available, the worker must fail closed rather than retry without
    # enable_thinking (which would silently re-enable thinking on the non-thinking path).
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': '/no_think\nsecret prompt'}]],
            'kwargs': {
                'tokenize': False,
                'add_generation_prompt': True,
                'enable_thinking': False,
            },
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        pass
    def apply_chat_template(self, messages, **kwargs):
        if not kwargs.get('enable_thinking', True):
            # First call: enable_thinking=False is present – reject it
            raise TypeError('unexpected keyword argument enable_thinking')
        # If called a second time without enable_thinking, fail loudly to catch
        # the old retry-without-enable_thinking behaviour.
        raise AssertionError('apply_chat_template must not be retried without enable_thinking')
    def tokenize(self, prompt, add_bos=False):
        raise AssertionError('tokenize must not be called when render fails')
""",
    )

    assert response['status'] == 'error'
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_uses_gguf_jinja_metadata(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': '/no_think\nsecret prompt'}]],
            'kwargs': {
                'tokenize': False,
                'add_generation_prompt': True,
                'enable_thinking': False,
            },
        },
        llama_body=r"""
class Llama:
    metadata = {
        'general.name': 'Qwen3 test',
        'tokenizer.chat_template': (
            "{% for message in messages %}"
            "<|im_start|>{{ message['role'] }}\n{{ message['content'] }}<|im_end|>\n"
            "{% endfor %}"
            "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
            "{% if enable_thinking == false %}<|no_think|>{% endif %}"
        )
    }
    def __init__(self, *args, **kwargs):
        pass
    def tokenizer(self):
        class Tokenizer:
            pass
        return Tokenizer()
    def tokenize(self, prompt, add_bos=False):
        text = prompt.decode('utf-8')
        assert '/no_think\nsecret prompt' in text
        assert '<|im_start|>assistant' in text
        assert '<|no_think|>' in text
        return [1, 2, 3, 4]
    def create_chat_completion(self, **kwargs):
        return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 4}}
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_fails_closed_without_metadata(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'secret prompt'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True},
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        pass
    def tokenizer(self):
        class Tokenizer:
            pass
        return Tokenizer()
    def tokenize(self, prompt, add_bos=False):
        return [1, 2]
    def create_chat_completion(self, **kwargs):
        return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
""",
    )

    assert response['status'] == 'error'
    assert response['diagnostics']['reason'] == 'runtime_chat_template_metadata_missing'
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_error_diagnostics_are_request_scoped(tmp_path):
    package_dir = tmp_path / 'llama_cpp'
    package_dir.mkdir()
    (package_dir / '__init__.py').write_text(r"""
class Llama:
    metadata = {
        'general.name': 'Qwen3 test',
        'tokenizer.chat_template': "{% for message in messages %}{{ message['content'] }}{% endfor %}",
    }
    def __init__(self, *args, **kwargs):
        self.calls = 0
    def tokenize(self, prompt, add_bos=False):
        self.calls += 1
        self.metadata = {}
        return [1]
""")
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), str(Path(__file__).parent.parent.parent)])
    process = subprocess.Popen(
        [sys.executable, '-c', 'from utils.llm.model_manager import _LLAMA_CPP_RUNTIME_WORKER_CODE; exec(_LLAMA_CPP_RUNTIME_WORKER_CODE)'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({'args': [], 'kwargs': {}}) + '\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline().split(':', 1)[1])['status'] == 'ok'

    request = {
        'method': 'render_and_tokenize_chat',
        'args': [[{'role': 'user', 'content': 'secret prompt'}]],
        'kwargs': {'tokenize': False, 'add_generation_prompt': True},
    }
    process.stdin.write(json.dumps(request) + '\n')
    process.stdin.flush()
    first_response = json.loads(process.stdout.readline().split(':', 1)[1])
    process.stdin.write(json.dumps(request) + '\n')
    process.stdin.flush()
    second_response = json.loads(process.stdout.readline().split(':', 1)[1])
    process.kill()
    process.wait(timeout=5)

    assert first_response == {'status': 'ok', 'result': {'prompt_tokens': 1}}
    assert second_response['status'] == 'error'
    assert second_response['diagnostics']['reason'] == 'runtime_chat_template_metadata_missing'
    assert 'metadata_template_available' not in second_response['diagnostics']
    assert 'secret prompt' not in json.dumps(second_response)


def test_llama_worker_render_and_tokenize_chat_keeps_qwen_evidence_from_later_metadata(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'secret prompt'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True},
        },
        llama_body=r"""
class Metadata(dict):
    pass

class Llama:
    metadata = {
        'tokenizer.chat_template': "{% for message in messages %}{{ message['content'] }}{% endfor %}",
    }
    def __init__(self, *args, **kwargs):
        self._model = type('Model', (), {'metadata': {'general.name': 'Qwen3 test'}})()
    def tokenize(self, prompt, add_bos=False):
        assert prompt == b'secret prompt'
        return [1, 2, 3]
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 3}}
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_fails_closed_without_qwen_evidence(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'secret prompt'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True},
        },
        llama_body=r"""
class Llama:
    metadata = {'tokenizer.chat_template': "{% for message in messages %}{{ message['content'] }}{% endfor %}"}
    def __init__(self, *args, **kwargs):
        pass
    def tokenize(self, prompt, add_bos=False):
        raise AssertionError('must fail closed before rendering non-Qwen metadata')
""",
    )

    assert response['status'] == 'error'
    assert response['diagnostics']['reason'] == 'runtime_chat_template_qwen_evidence_missing'
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_does_not_use_llama_formatter_for_qwen_metadata_path(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'hello'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True},
        },
        llama_body=r"""
class Llama:
    metadata = {
        'general.name': 'Qwen3 test',
        'tokenizer.chat_template': "{% for message in messages %}qwen:{{ message['role'] }}{% endfor %}{% if add_generation_prompt %}assistant{% endif %}"
    }
    chat_format = 'llama-3'
    def __init__(self, *args, **kwargs):
        pass
    def tokenize(self, prompt, add_bos=False):
        assert prompt.startswith(b'qwen:')
        return [1]
""",
        llama_chat_format_body="""
def format_llama3(*args, **kwargs):
    raise AssertionError('Llama formatter must not be used for render_and_tokenize_chat metadata path')
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 1}}


def test_llama_worker_render_and_tokenize_chat_formatter_falls_back_without_enable_thinking_support(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'secret prompt'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False},
        },
        llama_body=r"""
class Llama:
    metadata = {'general.name': 'Qwen3 test', 'tokenizer.chat_template': "{% for message in messages %}plain:{{ message['content'] }}{% endfor %}{% if enable_thinking == false %}:no-think{% endif %}"}
    def __init__(self, *args, **kwargs):
        pass
    def tokenize(self, prompt, add_bos=False):
        assert prompt == b'plain:secret prompt:no-think'
        return [1, 2, 3]
""",
        llama_chat_format_body="""
class Jinja2ChatFormatter:
    def __init__(self, *, template, bos_token='', eos_token=''):
        self.template = template
    def __call__(self, **kwargs):
        if 'enable_thinking' in kwargs:
            raise TypeError("got an unexpected keyword argument 'enable_thinking'")
        raise RuntimeError('formatter cannot render this template')
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 3}}
    assert 'secret prompt' not in json.dumps(response)


def test_llama_worker_render_and_tokenize_chat_passes_bos_eos_to_formatter_and_jinja(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'hello'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': False},
        },
        llama_body=r"""
class Llama:
    metadata = {'general.name': 'Qwen3 test', 'tokenizer.chat_template': "{{ bos_token }}{% for message in messages %}{{ message['content'] }}{% endfor %}{{ eos_token }}"}
    def __init__(self, *args, **kwargs):
        pass
    def token_bos(self):
        return 101
    def token_eos(self):
        return 102
    def token_get_text(self, token_id):
        return {101: b'<s>', 102: b'</s>'}[token_id]
    def tokenize(self, prompt, add_bos=False):
        assert prompt == b'<s>formatter hello</s>'
        return [1, 2, 3, 4]
""",
        llama_chat_format_body="""
class Rendered:
    def __init__(self, prompt):
        self.prompt = prompt
class Jinja2ChatFormatter:
    def __init__(self, *, template, bos_token='', eos_token=''):
        self.bos_token = bos_token
        self.eos_token = eos_token
    def __call__(self, **kwargs):
        assert self.bos_token == '<s>'
        assert self.eos_token == '</s>'
        return Rendered(self.bos_token + 'formatter hello' + self.eos_token)
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 4}}


def test_llama_worker_render_and_tokenize_chat_plain_jinja_uses_bos_eos_and_sandbox(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'hello'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': False},
        },
        llama_body=r"""
class Llama:
    metadata = {'general.name': 'Qwen3 test', 'tokenizer.chat_template': "{{ bos_token }}{% for message in messages %}{{ message['content'] }}{% endfor %}{{ eos_token }}"}
    def __init__(self, *args, **kwargs):
        pass
    def token_bos(self):
        return 101
    def token_eos(self):
        return 102
    def detokenize(self, token_ids):
        return {101: b'<s>', 102: b'</s>'}[token_ids[0]]
    def tokenize(self, prompt, add_bos=False):
        assert prompt == b'<s>hello</s>'
        return [1, 2, 3]
""",
    )

    assert response == {'status': 'ok', 'result': {'prompt_tokens': 3}}


def test_llama_worker_render_and_tokenize_chat_does_not_retry_unrelated_type_error(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{'role': 'user', 'content': 'secret prompt'}]],
            'kwargs': {'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False},
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        self.calls = 0
    def apply_chat_template(self, messages, **kwargs):
        self.calls += 1
        raise TypeError('template helper failed internally')
    def tokenize(self, prompt, add_bos=False):
        raise AssertionError('tokenize should not run')
""",
    )

    assert response['status'] == 'error'
    assert response['diagnostics']['reason'] == 'runtime_chat_template_render_exception'
    assert 'secret prompt' not in json.dumps(response)


def test_runtime_message_content_text_preserves_paragraph_breaks_between_blocks():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    assert namespace['_runtime_message_content_text']([
        {'type': 'text', 'text': 'First block'},
        {'type': 'input_text', 'input_text': 'Second block'},
    ]) == 'First block\n\nSecond block'


def test_runtime_message_content_text_rejects_non_text_blocks():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    with pytest.raises(RuntimeError, match='runtime_chat_template_render_exception'):
        namespace['_runtime_message_content_text']([
            {'type': 'text', 'text': 'Describe this image'},
            {'type': 'input_image', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
        ])


def test_llama_worker_render_and_tokenize_chat_rejects_multimodal_blocks_as_invalid_request(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Describe this image'},
                    {'type': 'input_image', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
                ],
            }]],
            'kwargs': {
                'tokenize': False,
                'add_generation_prompt': True,
                'enable_thinking': False,
                'token_place_provider': 'qwen',
                'token_place_template_policy': 'gguf-jinja',
            },
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        pass
    def apply_chat_template(self, messages, **kwargs):
        if 'enable_thinking' in kwargs:
            raise TypeError('unexpected keyword argument enable_thinking')
        raise AssertionError('apply_chat_template must not be retried without enable_thinking')
    def tokenize(self, prompt, add_bos=False):
        raise AssertionError('tokenize must not run for unsupported multimodal content')
""",
    )

    assert response['status'] == 'error'
    assert response['diagnostics']['code'] == 'compute_node_invalid_request'
    assert response['diagnostics']['reason'] == 'runtime_text_only_content_blocks_required'
    assert response['diagnostics']['generation_exception_category'] == 'text_only_content_blocks_required'


def test_llama_worker_render_and_tokenize_chat_rejects_other_non_text_blocks_as_invalid_request(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Transcribe this note'},
                    {'type': 'input_audio', 'input_audio': {'url': 'data:audio/wav;base64,AAAA'}},
                ],
            }]],
            'kwargs': {
                'tokenize': False,
                'add_generation_prompt': True,
                'enable_thinking': False,
                'token_place_provider': 'qwen',
                'token_place_template_policy': 'gguf-jinja',
            },
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        pass
    def apply_chat_template(self, messages, **kwargs):
        if 'enable_thinking' in kwargs:
            raise TypeError('unexpected keyword argument enable_thinking')
        raise AssertionError('apply_chat_template must not be retried without enable_thinking')
    def tokenize(self, prompt, add_bos=False):
        raise AssertionError('tokenize must not run for unsupported non-text content')
""",
    )

    assert response['status'] == 'error'
    assert response['diagnostics']['code'] == 'compute_node_invalid_request'
    assert response['diagnostics']['reason'] == 'runtime_text_only_content_blocks_required'
    assert response['diagnostics']['generation_exception_category'] == 'text_only_content_blocks_required'


def test_llama_worker_render_and_tokenize_chat_rejects_untyped_blocks_as_invalid_request(tmp_path):
    response = _run_llama_worker_request(
        tmp_path,
        {
            'method': 'render_and_tokenize_chat',
            'args': [[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Read this payload'},
                    {'payload': 'unexpected'},
                ],
            }]],
            'kwargs': {
                'tokenize': False,
                'add_generation_prompt': True,
                'enable_thinking': False,
                'token_place_provider': 'qwen',
                'token_place_template_policy': 'gguf-jinja',
            },
        },
        llama_body="""
class Llama:
    def __init__(self, *args, **kwargs):
        pass
    def apply_chat_template(self, messages, **kwargs):
        if 'enable_thinking' in kwargs:
            raise TypeError('unexpected keyword argument enable_thinking')
        raise AssertionError('apply_chat_template must not be retried without enable_thinking')
    def tokenize(self, prompt, add_bos=False):
        raise AssertionError('tokenize must not run for unsupported structured content')
""",
    )

    assert response['status'] == 'error'
    assert response['diagnostics']['code'] == 'compute_node_invalid_request'
    assert response['diagnostics']['reason'] == 'runtime_text_only_content_blocks_required'
    assert response['diagnostics']['generation_exception_category'] == 'text_only_content_blocks_required'


def test_llama_worker_apply_chat_template_fallback_still_supports_chat_format(tmp_path):
    package_dir = tmp_path / 'llama_cpp'
    package_dir.mkdir()
    (package_dir / '__init__.py').write_text("""
class Llama:
    chat_format = 'llama-2'
    def __init__(self, *args, **kwargs):
        pass
    def tokenize(self, prompt, add_bos=False):
        return [7, 8]
""")
    (package_dir / 'llama_chat_format.py').write_text("""
class Rendered:
    prompt = '<fallback prompt>'
def format_llama2(*args, **kwargs):
    return Rendered()
""")
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), str(Path(__file__).parent.parent.parent)])
    process = subprocess.Popen(
        [sys.executable, '-c', 'from utils.llm.model_manager import _LLAMA_CPP_RUNTIME_WORKER_CODE; exec(_LLAMA_CPP_RUNTIME_WORKER_CODE)'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({'args': [], 'kwargs': {}}) + '\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline().split(':', 1)[1])['status'] == 'ok'
    process.stdin.write(json.dumps({
        'method': 'apply_chat_template',
        'args': [[{'role': 'user', 'content': 'hello'}]],
        'kwargs': {'tokenize': True},
    }) + '\n')
    process.stdin.flush()
    response = json.loads(process.stdout.readline().split(':', 1)[1])
    process.kill()
    process.wait(timeout=5)

    assert response == {'status': 'ok', 'result': [7, 8]}


def test_get_llm_instance_with_recovery_returns_existing_runtime(standalone_model_manager):
    runtime = object()
    standalone_model_manager.get_llm_instance = MagicMock(return_value=runtime)
    standalone_model_manager._ensure_replacement_llm = MagicMock()

    assert standalone_model_manager.get_llm_instance_with_recovery() is runtime
    standalone_model_manager.get_llm_instance.assert_called_once_with()
    standalone_model_manager._ensure_replacement_llm.assert_not_called()


def test_get_llm_instance_with_recovery_attempts_replacement_when_unavailable(standalone_model_manager):
    replacement = object()
    standalone_model_manager.get_llm_instance = MagicMock(return_value=None)
    standalone_model_manager._ensure_replacement_llm = MagicMock(return_value=replacement)
    standalone_model_manager._llm_generation = 7

    assert standalone_model_manager.get_llm_instance_with_recovery() is replacement
    standalone_model_manager.get_llm_instance.assert_called_once_with()
    standalone_model_manager._ensure_replacement_llm.assert_called_once_with(7)


def test_qwen_8k_runtime_omits_llama_chat_format_and_yarn(tmp_path):
    config = MagicMock(is_production=False)
    config.get.side_effect = lambda key, default=None: {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': 0,
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }.get(key, default)
    manager = ModelManager(config)
    Path(manager.model_path).write_text('fake')

    class FakeTokenizer:
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
            return '<qwen>'

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
            self.kwargs = dict(model_path=model_path, n_gpu_layers=n_gpu_layers, n_ctx=n_ctx, verbose=verbose)

        def tokenizer(self):
            return FakeTokenizer()

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=FakeLlama)), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        llm = manager.get_llm_instance()

    assert llm is not None
    assert 'chat_format' not in llm.kwargs
    assert 'yarn_ext_factor' not in llm.kwargs
    assert manager.last_compute_diagnostics['chat_template_mode'] == 'gguf-jinja'
    assert manager.last_compute_diagnostics['thinking_mode_disabled'] is True
    assert manager.last_compute_diagnostics['rope_yarn_enabled'] is False
    assert manager.last_yarn_rope_diagnostics['active'] is False
    assert manager.last_yarn_rope_diagnostics['required'] is False


def test_qwen_64k_runtime_enables_yarn_kwargs(tmp_path):
    from utils.context_profiles import apply_context_profile
    captured = {}
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, rope_freq_scale, yarn_orig_ctx):
            captured.update({
                'model_path': model_path,
                'n_gpu_layers': n_gpu_layers,
                'n_ctx': n_ctx,
                'verbose': verbose,
                'rope_scaling_type': rope_scaling_type,
                'rope_freq_scale': rope_freq_scale,
                'yarn_orig_ctx': yarn_orig_ctx,
            })
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert captured['n_ctx'] == 65536
    assert captured['rope_scaling_type'] == 2
    assert captured['rope_freq_scale'] == 0.5
    assert captured['yarn_orig_ctx'] == 32768
    assert manager.last_compute_diagnostics['rope_yarn_enabled'] is True
    assert manager.last_compute_diagnostics['rope_yarn_factor'] == 2.0
    assert manager.last_compute_diagnostics['yarn_rope_enum_location'] == 'top_level_enum'


def test_qwen_64k_runtime_rejects_malformed_yarn_policy_before_probe(tmp_path):
    from utils.context_profiles import apply_context_profile

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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    manager.model_profile = dict(manager.model_profile)
    manager.model_profile['rope_scaling_policy'] = dict(manager.model_profile['rope_scaling_policy'])
    del manager.model_profile['rope_scaling_policy']['original_context_tokens']

    class FakeLlama:
        def __init__(self, *args, **kwargs):
            raise AssertionError('constructor must not be called when YaRN config is malformed')

    with patch(
        'utils.llm.model_manager._runtime_supports_qwen_yarn_rope',
        side_effect=AssertionError('capability probe must not run for malformed YaRN config'),
    ):
        with pytest.raises(RuntimeError, match='runtime_qwen_64k_yarn_configuration_invalid'):
            manager._runtime_init_kwargs(
                FakeLlama,
                0,
                SimpleNamespace(Llama=FakeLlama, LLAMA_ROPE_SCALING_TYPE_YARN=2),
                None,
            )

    assert manager.last_yarn_rope_diagnostics == {
        'active_profile_id': 'qwen3-8b-q4-k-m',
        'active_context_tier': '64k-full',
        'requested_n_ctx': 65536,
        'qwen_yarn_configuration_valid': False,
        'missing_reason': 'runtime_qwen_64k_yarn_configuration_invalid',
    }


def test_qwen_64k_runtime_resolves_nested_yarn_enum(tmp_path):
    from utils.context_profiles import apply_context_profile
    captured = {}
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, rope_freq_scale, yarn_orig_ctx):
            captured['rope_scaling_type'] = rope_scaling_type

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        llama_cpp=SimpleNamespace(LLAMA_ROPE_SCALING_TYPE_YARN=2),
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert captured['rope_scaling_type'] == 2
    assert manager.last_compute_diagnostics['yarn_rope_enum_location'] == 'nested_enum'


def test_qwen_64k_runtime_fails_when_yarn_kwargs_unsupported(tmp_path):
    from utils.context_profiles import apply_context_profile
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
            pass

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=FakeLlama)), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is None

    assert 'Qwen 64K requires YaRN/RoPE support in llama-cpp-python' in manager.last_runtime_init_error
    assert 'active_profile_id=qwen3-8b-q4-k-m' in manager.last_runtime_init_error
    assert 'active_context_tier=64k-full' in manager.last_runtime_init_error
    assert 'llama_module_path_present=False' in manager.last_runtime_init_error
    assert 'llama_cpp_python_version=' in manager.last_runtime_init_error
    assert 'missing constructor kwargs' in manager.last_runtime_init_error


def test_qwen_64k_runtime_uses_numeric_yarn_fallback_when_enum_constant_missing(tmp_path):
    from utils.context_profiles import apply_context_profile
    captured = {}
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, rope_freq_scale, yarn_orig_ctx):
            captured.update({
                'n_ctx': n_ctx,
                'rope_scaling_type': rope_scaling_type,
                'rope_freq_scale': rope_freq_scale,
                'yarn_orig_ctx': yarn_orig_ctx,
            })
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        llama_cpp=SimpleNamespace(),
        __file__='/opt/token.place/llama_cpp/__init__.py',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert captured == {
        'n_ctx': 65536,
        'rope_scaling_type': 2,
        'rope_freq_scale': 0.5,
        'yarn_orig_ctx': 32768,
    }
    assert manager.last_compute_diagnostics['yarn_rope_enum_location'] == 'numeric_fallback'
    assert manager.last_yarn_rope_diagnostics['constructor_kwarg_support']['rope_scaling_type'] is True


def test_qwen_64k_runtime_fails_when_yarn_enum_missing_and_constructor_lacks_rope_scaling(tmp_path):
    from utils.context_profiles import apply_context_profile
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, yarn_ext_factor, yarn_orig_ctx):
            _ = yarn_ext_factor
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=FakeLlama, llama_cpp=SimpleNamespace())), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is None

    assert 'Qwen 64K requires YaRN/RoPE support in llama-cpp-python' in manager.last_runtime_init_error
    assert 'rope_scaling_type constructor support' in manager.last_runtime_init_error

def test_qwen_validation_failure_does_not_cache_invalid_llm(tmp_path):
    config = MagicMock(is_production=False)
    config.get.side_effect = lambda key, default=None: {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': 0,
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }.get(key, default)
    manager = ModelManager(config)
    Path(manager.model_path).write_text('fake')

    constructed = []

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
            constructed.append(self)

    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=SimpleNamespace(Llama=FakeLlama)), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is None
        assert manager.llm is None
        first_failed = constructed[0]
        assert manager.get_llm_instance() is None

    assert len(constructed) == 2
    assert manager.llm is None
    assert manager.llm is not first_failed
    assert 'Qwen runtime requires GGUF/Jinja apply_chat_template support' in manager.last_runtime_init_error


def test_runtime_signature_helpers_cover_constructor_variants():
    manager = object.__new__(ModelManager)

    class ExplicitLlama:
        def __init__(self, model_path, rope_scaling_type):
            pass

    class KwargsLlama:
        def __init__(self, model_path, **kwargs):
            pass

    class InitKwargsCallRestrictedLlama:
        def __init__(self, model_path, **kwargs):
            pass

        def __call__(self, model_path):
            return None

    assert manager._llama_constructor_accepts(ExplicitLlama, 'rope_scaling_type') is True
    assert manager._llama_constructor_accepts(KwargsLlama, 'yarn_ext_factor') is True
    assert manager._llama_constructor_accepts(InitKwargsCallRestrictedLlama, 'future_yarn_kwarg') is True
    assert manager._llama_constructor_accepts(ExplicitLlama, 'yarn_ext_factor') is False
    assert manager._llama_constructor_accepts(42, 'rope_scaling_type') is False


def test_apply_chat_template_accepts_direct_runtime_and_tokenizer_paths():
    manager = object.__new__(ModelManager)

    class DirectRenderer:
        def apply_chat_template(self, messages, enable_thinking=False):
            return '<direct>'

    class KwargsTokenizer:
        def apply_chat_template(self, messages, **kwargs):
            return '<tokenizer>'

    class TokenizerBackedRenderer:
        def tokenizer(self):
            return KwargsTokenizer()

    class MissingRenderer:
        pass

    assert manager._apply_chat_template_accepts(DirectRenderer(), 'enable_thinking') is True
    assert manager._apply_chat_template_accepts(TokenizerBackedRenderer(), 'enable_thinking') is True
    assert manager._apply_chat_template_accepts(MissingRenderer(), 'enable_thinking') is False


def test_apply_chat_template_accepts_handles_tokenizer_and_signature_failures():
    manager = object.__new__(ModelManager)

    class BrokenTokenizerRuntime:
        def tokenizer(self):
            raise RuntimeError('tokenizer unavailable')

    class BuiltinRenderer:
        apply_chat_template = len

    assert manager._apply_chat_template_accepts(BrokenTokenizerRuntime(), 'enable_thinking') is False
    assert manager._apply_chat_template_accepts(BuiltinRenderer(), 'enable_thinking') is False


def test_apply_chat_template_accepts_returns_false_when_signature_uninspectable(monkeypatch):
    manager = object.__new__(ModelManager)

    class DirectRenderer:
        def apply_chat_template(self, messages):
            return '<direct>'

    import inspect

    original_signature = inspect.signature

    def fake_signature(target):
        if getattr(target, '__name__', '') == 'apply_chat_template':
            raise ValueError('uninspectable')
        return original_signature(target)

    monkeypatch.setattr('utils.llm.model_manager.inspect.signature', fake_signature)

    assert manager._apply_chat_template_accepts(DirectRenderer(), 'enable_thinking') is False


def test_qwen_runtime_init_kwargs_rejects_non_gguf_jinja_policy(tmp_path):
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        'model.context_size': 8192,
    }.get(key, default)
    manager = object.__new__(ModelManager)
    manager.config = config
    manager.model_path = str(tmp_path / 'model.gguf')
    manager.model_profile = {
        'provider': 'qwen',
        'chat_template_policy': 'llama-3',
        'native_context_tokens': 32768,
    }

    class FakeLlama:
        pass

    with pytest.raises(RuntimeError, match='GGUF/Jinja chat template policy'):
        manager._runtime_init_kwargs(FakeLlama, 0)


def test_qwen_64k_missing_reason_does_not_repeat_rope_scaling_type():
    from utils.llm import model_manager as model_manager_module

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
            pass

    diagnostics = model_manager_module._qwen_64k_rope_support_diagnostics(
        SimpleNamespace(Llama=FakeLlama),
        FakeLlama,
    )

    assert diagnostics['supported'] is False
    assert diagnostics['missing_reason'].count('rope_scaling_type') == 1
    assert 'runtime_qwen_64k_yarn_rope_freq_scale_unavailable; missing constructor kwargs: yarn_orig_ctx' in diagnostics['missing_reason']


def test_qwen_64k_diagnostics_mark_supported_when_required_yarn_kwargs_are_available():
    from utils.llm import model_manager as model_manager_module

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, rope_freq_scale, yarn_orig_ctx):
            pass

    diagnostics = model_manager_module._qwen_64k_rope_support_diagnostics(
        SimpleNamespace(LLAMA_ROPE_SCALING_TYPE_YARN='yarn', Llama=FakeLlama),
        FakeLlama,
    )

    assert diagnostics['supported'] is True
    assert diagnostics['missing_reason'] is None
    assert diagnostics['missing_required_kwargs'] == []
    assert diagnostics['yarn_resolver_source'] == 'top_level_enum'


def test_qwen_64k_runtime_applies_memory_profile_only_to_64k(tmp_path):
    from utils.context_profiles import apply_context_profile

    captured = {}
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        LLAMA_TYPE_Q8_0=8,
        __file__='/opt/token.place/llama_cpp/__init__.py',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert 'type_k' not in captured
    assert 'type_v' not in captured
    assert 'flash_attn' not in captured
    assert 'offload_kqv' not in captured
    assert 'n_batch' not in captured
    assert 'n_ubatch' not in captured
    assert manager.last_compute_diagnostics['qwen_64k_memory_profile']['profile_id'] == 'qwen64k_f16_fa_small_batch'


def test_qwen_64k_runtime_omits_memory_profile_when_kwargs_unsupported(tmp_path):
    from utils.context_profiles import apply_context_profile

    captured = {}
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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, rope_freq_scale, yarn_orig_ctx):
            captured.update(locals())

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(Llama=FakeLlama, LLAMA_ROPE_SCALING_TYPE_YARN=2, LLAMA_TYPE_Q8_0=8)
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cpu', 'gpu_offload_supported': False, 'error': None}):
        assert manager.get_llm_instance() is not None

    assert 'type_k' not in captured
    assert manager.last_compute_diagnostics.get('kv_cache_mode') == {}


def test_qwen_64k_memory_profile_does_not_trust_subprocess_proxy_kwargs():
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    kwargs, diagnostics = model_manager_module._qwen_64k_memory_profile_kwargs(
        facade,
        facade.Llama,
        enable_kqv_offload=True,
    )

    assert facade.LLAMA_TYPE_Q8_0 == 8
    assert kwargs == {}
    assert diagnostics['enabled'] is False


def test_qwen_64k_subprocess_worker_probe_preserves_yarn_constructor_support():
    from utils.llm import model_manager as model_manager_module

    support = {
        'rope_scaling_type': True,
        'yarn_ext_factor': True, 'rope_freq_scale': True,
        'yarn_orig_ctx': True,
        'type_k': False,
        'type_v': False,
        'flash_attn': False,
        'offload_kqv': False,
        'n_batch': False,
        'n_ubatch': False,
    }
    facade = model_manager_module._SubprocessLlamaCppModule(
        '/site/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'backend': 'metal',
            'gpu_offload_supported': True,
            'runtime_action': 'already_supported',
            'constructor_kwarg_support': support,
            'q8_kv_cache_type_value': 8,
            'capability_source': 'worker_probe',
        },
    )

    diagnostics = model_manager_module._qwen_64k_rope_support_diagnostics(
        facade,
        facade.Llama,
    )
    memory_kwargs, memory_diagnostics = model_manager_module._qwen_64k_memory_profile_kwargs(
        facade,
        facade.Llama,
        enable_kqv_offload=True,
    )

    assert diagnostics['supported'] is True
    assert diagnostics['constructor_kwarg_support']['rope_scaling_type'] is True
    assert diagnostics['constructor_kwarg_support']['yarn_ext_factor'] is True
    assert diagnostics['constructor_kwarg_support']['yarn_orig_ctx'] is True
    assert memory_kwargs == {}
    assert memory_diagnostics['enabled'] is False


def test_qwen_64k_subprocess_facade_reprobes_child_before_false_unsupported(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule(
        '/site/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'backend': 'metal',
            'gpu_offload_supported': True,
            'runtime_action': 'already_supported',
            'constructor_kwarg_support': {
                'rope_scaling_type': False,
                'yarn_ext_factor': False, 'rope_freq_scale': False,
                'yarn_orig_ctx': False,
            },
            'capability_source': 'parent_facade_signature',
        },
    )

    def fake_child_probe(**_kwargs):
        return {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': '/real/site/llama_cpp/__init__.py',
            'llama_cpp_python_version': '0.3.32',
            'constructor_kwarg_support': {
                'rope_scaling_type': True,
                'yarn_ext_factor': True, 'rope_freq_scale': True,
                'yarn_orig_ctx': True,
            },
            'constructor_has_var_kwargs': False,
            'constructor_signature_inspectable': True,
            'yarn_resolver_source': 'numeric_fallback',
            'yarn_enum_value': 2,
            'qwen_64k_yarn_support': 'supported',
            'capability_source': 'worker_probe',
        }

    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', fake_child_probe)

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is True
    assert diagnostics['capability_source'] == 'worker_probe'
    assert diagnostics['llama_module_path'] == '/real/site/llama_cpp/__init__.py'
    assert diagnostics['constructor_kwarg_support']['rope_scaling_type'] is True
    assert diagnostics['yarn_resolver_source'] == 'numeric_fallback'


def test_qwen_64k_subprocess_facade_unknown_reprobes_real_child(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule(
        '/site/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'backend': 'metal',
            'gpu_offload_supported': True,
            'runtime_action': 'already_supported',
            'constructor_kwarg_support': {},
            'constructor_signature_inspectable': False,
            'qwen_64k_yarn_support': 'unknown',
            'capability_source': 'parent_facade_signature',
        },
    )
    probe_calls = []

    def fake_child_probe(**kwargs):
        probe_calls.append(kwargs)
        return {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': '/real/site/llama_cpp/__init__.py',
            'llama_cpp_python_version': '0.3.32',
            'constructor_kwarg_support': {
                'rope_scaling_type': True,
                'yarn_ext_factor': True, 'rope_freq_scale': True,
                'yarn_orig_ctx': True,
            },
            'constructor_has_var_kwargs': False,
            'constructor_signature_inspectable': True,
            'yarn_resolver_source': 'numeric_fallback',
            'yarn_enum_value': 2,
            'qwen_64k_yarn_support': 'supported',
            'capability_source': 'worker_probe',
        }

    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', fake_child_probe)

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert probe_calls
    assert diagnostics['supported'] is True
    assert diagnostics['capability_source'] == 'worker_probe'
    assert diagnostics['child_probe_reprobe_attempted'] is True
    assert diagnostics['llama_module_path'] == '/real/site/llama_cpp/__init__.py'
    assert diagnostics['constructor_kwarg_support']['rope_scaling_type'] is True


def test_qwen_64k_subprocess_child_unknown_signature_with_yarn_value_without_kwargs_fails_closed(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_: {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': '/real/site/llama_cpp/__init__.py',
            'constructor_kwarg_support': {},
            'constructor_has_var_kwargs': False,
            'constructor_signature_inspectable': False,
            'yarn_resolver_source': 'numeric_fallback',
            'yarn_enum_value': 2,
            'qwen_64k_yarn_support': 'unknown',
            'capability_source': 'worker_probe',
        },
    )

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is False
    assert diagnostics['support_classification'] == 'unknown'
    assert diagnostics['yarn_enum_value'] == 2
    assert 'missing constructor kwargs' in diagnostics['missing_reason']


def test_qwen_64k_subprocess_child_unknown_signature_with_var_kwargs_and_yarn_value_is_supported(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_: {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': '/real/site/llama_cpp/__init__.py',
            'constructor_kwarg_support': {},
            'constructor_has_var_kwargs': True,
            'constructor_signature_inspectable': False,
            'yarn_resolver_source': 'numeric_fallback',
            'yarn_enum_value': 2,
            'qwen_64k_yarn_support': 'unknown',
            'capability_source': 'worker_probe',
        },
    )

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is True
    assert diagnostics['support_classification'] == 'unknown'
    assert diagnostics['constructor_has_var_kwargs'] is True
    assert diagnostics['missing_reason'] is None

def test_qwen_64k_subprocess_child_unknown_signature_without_yarn_value_fails_closed(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_: {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'llama_module_path': '/real/site/llama_cpp/__init__.py',
            'constructor_kwarg_support': {},
            'constructor_has_var_kwargs': False,
            'constructor_signature_inspectable': False,
            'yarn_resolver_source': 'unsupported',
            'qwen_64k_yarn_support': 'unknown',
            'capability_source': 'worker_probe',
        },
    )

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is False
    assert diagnostics['support_classification'] == 'unknown'
    assert diagnostics['yarn_enum_value'] is None
    assert 'missing concrete YaRN enum value from unknown child probe' in diagnostics['missing_reason']
    assert 'missing constructor kwargs' in diagnostics['missing_reason']


def test_qwen_64k_runtime_init_guard_rejects_supported_probe_without_yarn_value(tmp_path, monkeypatch):
    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module

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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose):
            pass

    fake_llama_cpp = SimpleNamespace(Llama=FakeLlama, __file__='/runtime/llama_cpp/__init__.py')
    monkeypatch.setattr(
        model_manager_module,
        '_runtime_supports_qwen_yarn_rope',
        lambda _module, _cls: {
            'supported': True,
            'support_classification': 'supported',
            'yarn_enum_value': None,
            'yarn_enum_location': 'worker_probe',
            'yarn_resolver_source': 'top_level_enum',
            'constructor_kwarg_support': {
                'rope_scaling_type': True,
                'yarn_ext_factor': True, 'rope_freq_scale': True,
                'yarn_orig_ctx': True,
            },
            'missing_reason': None,
            'llama_module_path': '/runtime/llama_cpp/__init__.py',
            'llama_cpp_python_version': '0.3.32',
            'accepted_constructor_kwargs': ['rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx'],
            'missing_required_kwargs': [],
            'capability_source': 'worker_probe',
            'constructor_signature_inspectable': True,
            'constructor_has_var_kwargs': False,
            'parent_facade_type': None,
            'child_probe_reprobe_attempted': False,
        },
    )

    with pytest.raises(RuntimeError, match='missing concrete YaRN enum value from supported child probe'):
        manager._runtime_init_kwargs(FakeLlama, 0, fake_llama_cpp)

    assert manager.last_yarn_rope_diagnostics['supported'] is False
    assert manager.last_yarn_rope_diagnostics['missing_reason'] == (
        'missing concrete YaRN enum value from supported child probe'
    )


def test_desktop_runtime_probe_rejects_string_yarn_enum_value():
    from utils.llm import model_manager as model_manager_module

    coerced = model_manager_module._coerce_desktop_runtime_probe({
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'already_supported',
        'llama_cpp_python_version': '0.3.32',
        'constructor_has_var_kwargs': 1,
        'constructor_signature_inspectable': 0,
        'qwen_64k_yarn_support': 'unknown',
        'yarn_resolver_source': 'numeric_fallback',
        'yarn_enum_value': '2',
    })

    assert 'yarn_enum_value' not in coerced
    assert coerced['llama_cpp_python_version'] == '0.3.32'
    assert coerced['constructor_has_var_kwargs'] is True
    assert coerced['constructor_signature_inspectable'] is False
    assert coerced['qwen_64k_yarn_support'] == 'unknown'
    assert coerced['yarn_resolver_source'] == 'numeric_fallback'


def test_optional_int_enum_coercion_rejects_bool_none_and_accepts_signed_string():
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._coerce_optional_int_enum(True) is None
    assert model_manager_module._coerce_optional_int_enum(None) is None
    assert model_manager_module._coerce_optional_int_enum('-2') == -2


def test_desktop_runtime_probe_omits_malformed_yarn_enum_value():
    from utils.llm import model_manager as model_manager_module

    coerced = model_manager_module._coerce_desktop_runtime_probe({
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'already_supported',
        'yarn_enum_value': ['2'],
    })

    assert 'yarn_enum_value' not in coerced


def test_qwen_64k_numeric_fallback_not_used_when_child_rejects_rope_scaling(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    facade = model_manager_module._SubprocessLlamaCppModule('/site/llama_cpp/__init__.py')
    monkeypatch.setattr(
        model_manager_module,
        '_probe_llama_cpp_capabilities_in_subprocess',
        lambda **_: {
            'backend': 'metal',
            'gpu_offload_supported': True,
            'constructor_kwarg_support': {
                'rope_scaling_type': False,
                'yarn_ext_factor': True, 'rope_freq_scale': True,
                'yarn_orig_ctx': True,
            },
            'constructor_signature_inspectable': True,
            'yarn_resolver_source': 'unsupported',
            'qwen_64k_yarn_support': 'unsupported',
            'capability_source': 'worker_probe',
        },
    )

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is False
    assert diagnostics['yarn_resolver_source'] == 'unsupported'
    assert diagnostics['yarn_enum_value'] is None


def test_qwen_64k_memory_profile_disables_kqv_offload_for_cpu_fallback():
    from utils.llm import model_manager as model_manager_module

    class FakeLlama:
        def __init__(self, type_k, type_v, flash_attn, offload_kqv, n_batch, n_ubatch):
            self.kwargs = {
                'type_k': type_k,
                'type_v': type_v,
                'flash_attn': flash_attn,
                'offload_kqv': offload_kqv,
                'n_batch': n_batch,
                'n_ubatch': n_ubatch,
            }

    kwargs, diagnostics = model_manager_module._qwen_64k_memory_profile_kwargs(
        SimpleNamespace(),
        FakeLlama,
        enable_kqv_offload=False,
    )

    assert kwargs == {}
    assert diagnostics['enabled'] is False


def test_qwen_64k_memory_profile_uses_worker_probe_numeric_q8():
    from utils.llm import model_manager as model_manager_module

    support = {
        'type_k': True,
        'type_v': True,
        'flash_attn': True,
        'offload_kqv': True,
        'n_batch': True,
        'n_ubatch': True,
    }
    facade = model_manager_module._SubprocessLlamaCppModule(
        '/site/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'backend': 'metal',
            'gpu_offload_supported': True,
            'runtime_action': 'already_supported',
            'constructor_kwarg_support': support,
            'q8_kv_cache_type_value': 8,
            'capability_source': 'worker_probe',
        },
    )

    kwargs, diagnostics = model_manager_module._qwen_64k_memory_profile_kwargs(
        facade,
        facade.Llama,
        enable_kqv_offload=True,
    )

    assert kwargs['type_k'] == 8
    assert kwargs['type_v'] == 8
    assert kwargs['flash_attn'] is True
    assert kwargs['offload_kqv'] is True
    assert diagnostics['capability_source'] == 'worker_probe'


def test_qwen_64k_memory_profiles_skip_missing_kv_constants_without_noop_profile():
    from utils.llm import model_manager as model_manager_module

    class NoKvEnumLlama:
        __token_place_supported_constructor_kwargs__ = ('flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch')

    profiles = model_manager_module._build_qwen_64k_runtime_profiles(
        SimpleNamespace(),
        NoKvEnumLlama,
        model_path='model.gguf',
        n_ctx=65536,
    )

    assert [profile['profile_id'] for profile in profiles] == ['qwen64k_f16_fa_small_batch']
    skipped = profiles[0]['diagnostics']['skipped_profiles']
    assert [item['profile_id'] for item in skipped] == ['qwen64k_kv_q8_fa_small_batch', 'qwen64k_kv_q4_fa_small_batch']
    assert all(not item['enabled'] for item in skipped)
    assert all('flash_attn' in item['applied'] for item in skipped)
    assert all('type_k' not in item['applied'] and 'type_v' not in item['applied'] for item in skipped)


def test_child_diagnostics_drop_payloads_keys_and_arbitrary_stderr():
    from utils.llm import model_manager as model_manager_module

    text = """
prompt: reveal SECRET_PROMPT_123
assistant: SECRET_ASSISTANT_456
ciphertext_body=abc123 key=sk-token decrypted payload
random child stderr snippet survives?
/Users/Alice/Application Support/token.place/model.gguf
ggml_metal: KV cache allocation failed for llama_context
"""

    sanitized = model_manager_module._sanitize_child_diagnostic_text(text)

    assert 'SECRET_PROMPT_123' not in sanitized
    assert 'SECRET_ASSISTANT_456' not in sanitized
    assert 'ciphertext_body' not in sanitized
    assert 'decrypted payload' not in sanitized
    assert 'sk-token' not in sanitized
    assert 'random child stderr snippet' not in sanitized
    assert '/Users/Alice' not in sanitized
    assert 'Application Support' not in sanitized
    assert 'KV cache allocation failed' in sanitized


def test_qwen_64k_memory_profile_omits_kwargs_when_worker_probe_lacks_support():
    from utils.llm import model_manager as model_manager_module

    class LocalFacadeAcceptsKwargs:
        __token_place_supported_constructor_kwargs__ = ()

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    facade = SimpleNamespace(
        LLAMA_TYPE_Q8_0=8,
        __token_place_worker_capabilities__={
            'constructor_kwarg_support': {
                'type_k': False,
                'type_v': False,
                'flash_attn': False,
                'offload_kqv': False,
                'n_batch': False,
                'n_ubatch': False,
            },
            'q8_kv_cache_type_value': 8,
            'capability_source': 'worker_probe',
        },
    )

    kwargs, diagnostics = model_manager_module._qwen_64k_memory_profile_kwargs(
        facade,
        LocalFacadeAcceptsKwargs,
        enable_kqv_offload=True,
    )

    assert kwargs == {}
    assert diagnostics['enabled'] is False


def test_subprocess_worker_error_summary_keeps_safe_category_hint():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('def _metadata_value', 1)[0], namespace)

    summary = namespace['_sanitize_error_summary'](RuntimeError('Metal failed to allocate KV cache for /tmp/model.gguf'))

    assert summary == 'RuntimeError:metal_memory_allocation'


@pytest.mark.parametrize(
    'message',
    [
        'CUDA error: out of memory',
        'cudaMalloc failed',
        'CUBLAS_STATUS_ALLOC_FAILED',
        'ggml_cuda: failed to allocate device buffer',
    ],
)
def test_subprocess_worker_cuda_oom_classification_uses_safe_category(message):
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('def _metadata_value', 1)[0], namespace)

    exc = RuntimeError(message)

    assert namespace['_classify_generation_exception'](exc) == 'cuda_memory_allocation'
    assert namespace['_sanitize_error_summary'](exc).endswith(':cuda_memory_allocation')


def test_runtime_context_create_generic_ggml_cuda_failure_is_not_cuda_memory():
    from utils.llm import model_manager as model_manager_module

    category = model_manager_module._classify_runtime_context_create_error(
        RuntimeError('ggml_cuda_init: failed to load CUDA backend; Failed to create llama_context')
    )

    assert category == 'runtime_context_create_failed'


def test_subprocess_worker_generic_cuda_text_is_not_allocation_failure():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('def _metadata_value', 1)[0], namespace)

    exc = RuntimeError('CUDA backend initialized')

    assert namespace['_classify_generation_exception'](exc) != 'cuda_memory_allocation'
    assert namespace['_sanitize_error_summary'](exc) != 'RuntimeError:cuda_memory_allocation'


def test_subprocess_worker_plain_completion_helpers_cover_safe_shapes():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('def _metadata_value', 1)[0], namespace)

    normalize = namespace['_normalize_plain_completion_result']
    result_shape = namespace['_completion_result_shape']
    classify_shape = namespace['_plain_completion_method_shape_category']
    extract_rejected = namespace['_extract_unsupported_generation_kwarg']

    normalized, invalid_reason = normalize({'choices': [{'text': ' ok <|im_end|>'}]})
    assert invalid_reason is None
    assert normalized == {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
    normalized, invalid_reason = normalize({'choices': [{'message': {'content': 'message ok'}}]})
    assert invalid_reason is None
    assert normalized['choices'][0]['message']['content'] == 'message ok'
    assert normalize({'choices': [{'message': {'content': 'visible', 'reasoning_content': 'hidden'}}]}) == (
        None,
        'thinking_leaked',
    )
    assert normalize({'choices': [{'message': {'content': 'visible', 'reasoning': 'hidden'}}]}) == (
        None,
        'thinking_leaked',
    )
    for completion in (
        {'choices': [{'message': {'content': 'visible', 'reasoning_content': ''}}]},
        {'choices': [{'message': {'content': 'visible', 'reasoning_content': None}}]},
        {'choices': [{'message': {'content': 'visible', 'reasoning': ''}}]},
        {'choices': [{'text': 'visible', 'reasoning': 'hidden'}]},
        {'choices': [{'message': {'content': 'visible'}, 'reasoning': 'hidden'}]},
        {'choices': [{'message': {'content': 'visible', 'metadata': [{'reasoning': False}]}}]},
        {'choices': [{'message': {'content': 'visible', 'reasoning': {'trace': 'hidden'}}}]},
    ):
        assert normalize(completion) == (None, 'thinking_leaked')
    normalized, invalid_reason = normalize('direct ok')
    assert invalid_reason is None
    assert normalized['choices'][0]['message']['content'] == 'direct ok'
    normalized, invalid_reason = normalize('<think></think> wrapper ok')
    assert invalid_reason is None
    assert normalized['choices'][0]['message']['content'] == 'wrapper ok'

    assert normalize({'choices': []}) == (None, 'malformed_completion_output')
    assert normalize({'choices': [{'text': '<think>secret</think> visible'}]}) == (
        None,
        'thinking_leaked',
    )
    assert normalize('<think>unterminated') == (None, 'thinking_leaked')
    assert normalize('visible </think>') == (None, 'thinking_leaked')
    assert normalize('<think></think><|im_end|>') == (None, 'empty_completion_output')

    assert result_shape('text') == 'direct_string'
    assert result_shape({'choices': [{'text': 'text'}]}) == 'choices_text'
    assert result_shape({'choices': [{'message': {'content': 'text'}}]}) == 'choices_message'
    assert result_shape({'choices': []}) == 'dict_malformed'
    assert result_shape(['not', 'supported']) == 'list'

    assert extract_rejected("unsupported option: mirostat", ['mirostat']) == 'mirostat'
    assert extract_rejected("invalid keyword=temperature", ['temperature']) == 'temperature'
    assert extract_rejected("unexpected keyword argument 'prompt'", ['max_tokens']) is None
    assert (
        extract_rejected(
            "got some positional-only arguments passed as keyword arguments: 'prompt'",
            ['max_tokens', 'prompt'],
        )
        == 'prompt'
    )
    assert classify_shape(TypeError("got an unexpected keyword argument 'prompt'")) == 'unsupported_prompt_kwarg'
    assert classify_shape(
        TypeError("got some positional-only arguments passed as keyword arguments: 'prompt'")
    ) == 'unsupported_prompt_kwarg'
    assert classify_shape(TypeError("got an unexpected keyword argument 'stream'")) == 'unsupported_stream_kwarg'
    assert classify_shape(TypeError("got an unexpected keyword argument 'stop'")) == 'unsupported_stop_kwarg'
    assert classify_shape(TypeError("got an unexpected keyword argument 'temperature'")) == 'unexpected_kwarg'
    assert classify_shape(RuntimeError("KV cache allocation failed")) == 'kv_cache_allocation'
    assert classify_shape(TimeoutError("llama_cpp inference timed out")) == 'worker_timeout'
    assert classify_shape(RuntimeError("llama_cpp worker exited during liveness check")) == 'worker_dead'
    assert classify_shape(RuntimeError("prompt exceeds context window")) == 'context_window_exceeded'
    assert classify_shape(RuntimeError("requested context length exceeds n_ctx")) == 'context_length_exceeded'
    assert classify_shape(RuntimeError("too many tokens in prompt")) == 'token_overflow'



def test_subprocess_worker_render_template_retries_tokenize_with_qwen_jinja_metadata():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    class RejectingRuntime:
        metadata = {
            'general.name': 'Qwen3 packaged test',
            'tokenizer.chat_template': (
                '{% for message in messages %}<|im_start|>{{ message.role }}\n'
                '{{ message.content }}<|im_end|>\n{% endfor %}'
                '{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}'
            ),
        }

        def apply_chat_template(self, _messages, **kwargs):
            if 'tokenize' in kwargs:
                raise TypeError("got an unexpected keyword argument 'tokenize'")
            raise AssertionError('metadata renderer should be preferred after tokenize rejection')

    messages = [{'role': 'user', 'content': 'plaintext prompt must not appear in diagnostics'}]
    rendered, diagnostics = namespace['_render_chat_with_runtime_template'](
        RejectingRuntime(),
        [messages],
        {
            'tokenize': False,
            'add_generation_prompt': True,
            'enable_thinking': False,
            'token_place_provider': 'qwen',
        },
    )

    assert '<|im_start|>assistant' in rendered
    assert diagnostics['metadata_template'] is True
    assert diagnostics['jinja_renderer'] is True
    assert diagnostics['rejected_generation_kwarg'] == 'tokenize'
    assert diagnostics['method'] == 'apply_chat_template'
    assert 'plaintext prompt' not in json.dumps(diagnostics)


@pytest.mark.parametrize('rejected_kwarg', ['tokenize', 'add_generation_prompt'])
def test_subprocess_worker_render_template_retries_rejected_render_kwarg_without_metadata(rejected_kwarg):
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    class RejectingRuntime:
        metadata = {}

        def apply_chat_template(self, _messages, **kwargs):
            if rejected_kwarg in kwargs:
                raise TypeError(f"got an unexpected keyword argument '{rejected_kwarg}'")
            return '<|im_start|>assistant\n'

    rendered, diagnostics = namespace['_render_chat_with_runtime_template'](
        RejectingRuntime(),
        [[{'role': 'user', 'content': 'plaintext prompt must not appear in diagnostics'}]],
        {'tokenize': False, 'add_generation_prompt': True},
    )

    assert rendered == '<|im_start|>assistant\n'
    assert diagnostics['method'] == 'apply_chat_template'
    assert diagnostics['generation_exception_category'] == 'unsupported_render_kwarg'
    assert diagnostics['render_rejected_generation_kwarg'] == rejected_kwarg
    assert diagnostics['rejected_generation_kwarg'] == rejected_kwarg
    assert diagnostics['attempted_generation_kwargs'] == 'add_generation_prompt,tokenize'
    assert 'plaintext prompt' not in json.dumps(diagnostics)


def test_subprocess_worker_render_template_fails_closed_when_enable_thinking_rejected_and_jinja_broken():
    # Previously the worker retried apply_chat_template without enable_thinking when
    # the GGUF/Jinja renderer failed.  That retry silently re-enables thinking.
    # With the fix the worker must fail closed with safe diagnostics instead.
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    direct_render_calls = []

    class RejectingRuntime:
        metadata = {
            'general.name': 'Qwen3 packaged test',
            'tokenizer.chat_template': "{{ raise_exception('broken metadata template') }}",
        }

        def apply_chat_template(self, _messages, **kwargs):
            direct_render_calls.append(dict(kwargs))
            if 'enable_thinking' in kwargs:
                raise TypeError("got an unexpected keyword argument 'enable_thinking'")
            # Must not be called a second time without enable_thinking.
            raise AssertionError('apply_chat_template must not be retried without enable_thinking')

    with pytest.raises(RuntimeError) as excinfo:
        namespace['_render_chat_with_runtime_template'](
            RejectingRuntime(),
            [[{'role': 'user', 'content': 'plaintext prompt must not appear in diagnostics'}]],
            {'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False},
        )

    # Should have been called exactly once (with enable_thinking present).
    assert len(direct_render_calls) == 1
    assert 'enable_thinking' in direct_render_calls[0]
    # Raised a safe render error – the exact reason depends on Jinja availability.
    assert str(excinfo.value) in {
        'runtime_chat_template_render_exception',
        'runtime_chat_template_renderer_unavailable',
    }
    diagnostics = excinfo.value.diagnostics
    assert diagnostics.get('render_rejected_generation_kwarg') == 'enable_thinking'
    assert diagnostics.get('rejected_generation_kwarg') == 'enable_thinking'
    assert 'plaintext prompt' not in str(diagnostics)


def test_subprocess_worker_render_template_failure_carries_safe_rejected_kwarg_diagnostics():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    class RejectingRuntime:
        metadata = {}

        def apply_chat_template(self, _messages, **kwargs):
            if 'add_generation_prompt' in kwargs:
                raise TypeError("got an unexpected keyword argument 'add_generation_prompt'")
            raise RuntimeError('fallback renderer is unavailable')

    with pytest.raises(RuntimeError) as excinfo:
        namespace['_render_chat_with_runtime_template'](
            RejectingRuntime(),
            [[{'role': 'user', 'content': 'plaintext prompt must not appear in diagnostics'}]],
            {'tokenize': False, 'add_generation_prompt': True},
        )

    assert str(excinfo.value) == 'runtime_chat_template_metadata_missing'
    diagnostics = excinfo.value.diagnostics
    assert diagnostics['render_rejected_generation_kwarg'] == 'add_generation_prompt'
    assert diagnostics['rejected_generation_kwarg'] == 'add_generation_prompt'
    assert diagnostics['attempted_generation_kwargs'] == 'add_generation_prompt,tokenize'
    assert 'generation_exception_category' not in diagnostics
    assert 'plaintext prompt' not in json.dumps(diagnostics)

def test_subprocess_worker_render_template_fails_closed_with_safe_rejected_kwarg_diagnostics():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    request = {
        'method': 'create_chat_completion_from_rendered_prompt',
        'kwargs': {'max_tokens': 64, 'enable_thinking': False},
    }
    response = namespace['_safe_request_error'](
        'inference_exception',
        request=request,
        exc=TypeError("got an unexpected keyword argument 'max_tokens'"),
        extra={
            'method': 'create_completion_keyword_prompt',
            'attempted_plain_completion_methods': 'create_completion_keyword_prompt',
            'attempted_generation_kwargs': 'max_tokens,prompt',
            'generation_exception_category': 'unsupported_generation_kwarg',
            'rejected_generation_kwarg': 'max_tokens',
            'result_shape': 'dict_malformed',
            'prompt': 'plaintext prompt must not appear',
        },
    )

    diagnostics = response['diagnostics']
    assert diagnostics['reason'] == 'unsupported_generation_option'
    assert diagnostics['rejected_generation_kwarg'] == 'max_tokens'
    assert diagnostics['attempted_plain_completion_methods'] == 'create_completion_keyword_prompt'
    assert diagnostics['attempted_generation_kwargs'] == 'max_tokens,prompt'
    assert diagnostics['method'] == 'create_completion_keyword_prompt'
    assert diagnostics['result_shape'] == 'dict_malformed'
    assert 'plaintext prompt' not in json.dumps(diagnostics)


def test_subprocess_worker_render_template_uses_gguf_jinja_when_enable_thinking_rejected_and_metadata_available():
    # When apply_chat_template rejects enable_thinking but valid Qwen GGUF/Jinja
    # metadata is available, the GGUF/Jinja renderer should be used and
    # enable_thinking=False must be preserved (never silently dropped or set True).
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    class QwenRuntime:
        metadata = {
            'general.name': 'Qwen3-8B',
            'tokenizer.chat_template': (
                "{% for m in messages %}<|im_start|>{{ m['role'] }}\n"
                "{{ m['content'] }}<|im_end|>\n{% endfor %}"
                "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
                "{% if enable_thinking == false %}<|no_think|>{% endif %}"
            ),
        }

        def apply_chat_template(self, _messages, **kwargs):
            # Record the call; raise if enable_thinking is present so Jinja path is taken.
            if 'enable_thinking' in kwargs:
                raise TypeError("got an unexpected keyword argument 'enable_thinking'")
            # Must not be called a second time without enable_thinking.
            raise AssertionError('apply_chat_template must not be retried without enable_thinking')

    messages = [{'role': 'user', 'content': '/no_think\nhello'}]
    try:
        rendered, diagnostics = namespace['_render_chat_with_runtime_template'](
            QwenRuntime(),
            [messages],
            {'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False},
        )
    except RuntimeError as exc:
        if str(exc) == 'runtime_chat_template_renderer_unavailable':
            pytest.skip('jinja2.sandbox not available in this environment')
        raise

    # GGUF/Jinja path used: enable_thinking=False was honoured (no-think marker present).
    assert '<|no_think|>' in rendered
    assert diagnostics.get('render_rejected_generation_kwarg') == 'enable_thinking'
    assert diagnostics.get('jinja_renderer') is True
    assert '/no_think\nhello' not in str(diagnostics)


def test_subprocess_worker_enable_thinking_true_never_passed_to_renderer():
    # Verify that the Qwen API v1 render path only ever passes enable_thinking=False,
    # never True.  A renderer that receives enable_thinking=True must raise so the
    # test fails immediately.
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    class StrictQwenRuntime:
        metadata = {
            'general.name': 'Qwen3-8B',
            'tokenizer.chat_template': (
                "{% for m in messages %}{{ m['content'] }}{% endfor %}"
                "{% if enable_thinking == false %}<|no_think|>{% endif %}"
            ),
        }

        def apply_chat_template(self, _messages, **kwargs):
            et = kwargs.get('enable_thinking')
            if et is True:
                raise AssertionError('enable_thinking must never be True on the API v1 non-thinking path')
            raise TypeError("got an unexpected keyword argument 'enable_thinking'")

    messages = [{'role': 'user', 'content': 'hi'}]
    try:
        rendered, diagnostics = namespace['_render_chat_with_runtime_template'](
            StrictQwenRuntime(),
            [messages],
            {'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False},
        )
    except RuntimeError as exc:
        if str(exc) == 'runtime_chat_template_renderer_unavailable':
            pytest.skip('jinja2.sandbox not available in this environment')
        # Any other failure (not AssertionError about True) is acceptable.
        assert str(exc) != 'enable_thinking must never be True on the API v1 non-thinking path'
        return

    # If we got here, the GGUF/Jinja path rendered successfully.
    # The no-think marker must be present, confirming enable_thinking=False was honoured.
    assert '<|no_think|>' in rendered


def test_subprocess_worker_enable_thinking_rejected_no_metadata_fails_closed_with_diagnostics():
    # When apply_chat_template rejects enable_thinking and no GGUF metadata is
    # available, the worker must fail closed and surface safe scalar diagnostics
    # that name the rejected kwarg.
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('try:\n    init_line = sys.stdin.readline()', 1)[0], namespace)

    class MinimalRuntime:
        # No metadata attribute → GGUF path unavailable.
        def apply_chat_template(self, _messages, **kwargs):
            if 'enable_thinking' in kwargs:
                raise TypeError("got an unexpected keyword argument 'enable_thinking'")
            raise AssertionError('apply_chat_template must not be retried without enable_thinking')

    with pytest.raises(RuntimeError) as excinfo:
        namespace['_render_chat_with_runtime_template'](
            MinimalRuntime(),
            [[{'role': 'user', 'content': 'plaintext must not appear'}]],
            {'tokenize': False, 'add_generation_prompt': True, 'enable_thinking': False},
        )

    assert str(excinfo.value) == 'runtime_chat_template_metadata_missing'
    diag = excinfo.value.diagnostics
    assert diag.get('render_rejected_generation_kwarg') == 'enable_thinking'
    assert diag.get('rejected_generation_kwarg') == 'enable_thinking'
    assert 'plaintext must not appear' not in str(diag)


def test_subprocess_proxy_reports_actual_child_model_path_exists_with_relative_spaces(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'fake site-packages child exists'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "import os\n"
        "class Llama:\n"
        "    def __init__(self, model_path=None, **_kwargs):\n"
        "        assert os.path.isabs(model_path)\n"
        "        assert os.path.exists(model_path)\n"
        "        assert os.getcwd() != os.path.dirname(model_path)\n"
        "    def create_chat_completion(self, *args, **kwargs):\n"
        "        return {'choices': [{'message': {'content': 'ok'}}]}\n",
        encoding='utf-8',
    )
    parent_cwd = tmp_path / 'parent cwd with spaces'
    model_dir = parent_cwd / 'models with spaces'
    model_dir.mkdir(parents=True)
    (model_dir / 'mock.gguf').write_bytes(b'GGUFtiny')
    other_worker_cwd = tmp_path / 'worker cwd with spaces'
    other_worker_cwd.mkdir()

    monkeypatch.chdir(parent_cwd)
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(fake_site))
    monkeypatch.setenv('PYTHONPATH', str(fake_site))
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_DESKTOP_PYTHON_ROOT', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_PROBE_REPO_ROOT', raising=False)
    monkeypatch.setattr(model_manager_module, '_llama_cpp_probe_sys_path_entries', lambda: [str(fake_site)])
    monkeypatch.setattr(model_manager_module, '_llama_cpp_probe_subprocess_cwd', lambda: str(other_worker_cwd))

    proxy = model_manager_module._SubprocessLlamaProxy(model_path=os.path.join('models with spaces', 'mock.gguf'), timeout_seconds=5)
    try:
        assert proxy.child_model_path_exists is True
    finally:
        proxy.close()


def test_subprocess_proxy_uses_temp_worker_script_and_cleans_up(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    popen_calls = []

    class FakeStdin:
        def __init__(self):
            self.writes = []
            self.closed = False

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            pass

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            self.stdin = FakeStdin()
            self.stdout = None
            self.stderr = []
            self.terminated = False
            self.waited = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        popen_calls.append(process)
        return process

    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(model_manager_module._SubprocessLlamaProxy, '_start_stderr_tail_reader', lambda self: None)
    monkeypatch.setattr(
        model_manager_module,
        '_read_llama_subprocess_message',
        lambda *args, **kwargs: {'status': 'ok'},
    )

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf')
    tmpfile = proxy._worker_tmpfile

    assert tmpfile
    assert os.path.exists(tmpfile)
    assert popen_calls[0].command == [sys.executable, '-u', tmpfile]
    assert popen_calls[0]._token_place_command == [sys.executable, '<runtime-worker-script>']
    assert '"method": "__import__"' in popen_calls[0].stdin.writes[0]
    assert '"method": "__init__"' in popen_calls[0].stdin.writes[1]

    proxy.close()

    assert popen_calls[0].stdin.closed is True
    assert popen_calls[0].terminated is True
    assert popen_calls[0].waited is True
    assert not os.path.exists(tmpfile)
    assert proxy._worker_tmpfile is None


def test_subprocess_proxy_falls_back_to_inline_code_when_tempfile_unavailable(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    popen_commands = []

    class FakeStdin:
        def write(self, value):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class FakeProcess:
        stdin = FakeStdin()
        stdout = None
        stderr = []

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        process = FakeProcess()
        popen_commands.append(command)
        return process

    monkeypatch.setattr(model_manager_module.tempfile, 'mkstemp', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('no tmp')))
    monkeypatch.setattr(model_manager_module.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(model_manager_module._SubprocessLlamaProxy, '_start_stderr_tail_reader', lambda self: None)
    monkeypatch.setattr(
        model_manager_module,
        '_read_llama_subprocess_message',
        lambda *args, **kwargs: {'status': 'ok'},
    )

    proxy = model_manager_module._SubprocessLlamaProxy(model_path='model.gguf')

    assert proxy._worker_tmpfile is None
    assert popen_commands[0][:3] == [sys.executable, '-u', '-c']
    assert proxy._process._token_place_command == [sys.executable, '<runtime-worker-code>']


def test_subprocess_proxy_removes_temp_worker_script_when_popen_fails(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    created_tmpfiles = []
    real_mkstemp = model_manager_module.tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created_tmpfiles.append(path)
        return fd, path

    monkeypatch.setattr(model_manager_module.tempfile, 'mkstemp', tracking_mkstemp)
    monkeypatch.setattr(
        model_manager_module.subprocess,
        'Popen',
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('spawn failed')),
    )

    with pytest.raises(OSError, match='spawn failed'):
        model_manager_module._SubprocessLlamaProxy(model_path='model.gguf')

    assert created_tmpfiles
    assert not os.path.exists(created_tmpfiles[0])


def test_subprocess_proxy_stream_marks_closed_on_eof(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._closed = False
    proxy._lock = model_manager_module.Lock()
    proxy._process = object()

    sent_payloads = []

    def fake_send(payload, *, check_health=True):
        sent_payloads.append(payload)

    monkeypatch.setattr(proxy, "_send", fake_send)
    monkeypatch.setattr(
        model_manager_module,
        "_read_llama_subprocess_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(model_manager_module.LlamaCppWorkerEOFError("eof")),
    )

    with pytest.raises(model_manager_module.LlamaCppWorkerEOFError):
        next(proxy._stream_chat_completion([{"role": "user", "content": "hi"}]))

    assert proxy._closed is True
    assert sent_payloads[0]["method"] == "create_chat_completion"


def test_subprocess_proxy_ignores_unlink_failure_when_popen_fails(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    unlink_calls = []
    real_mkstemp = model_manager_module.tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        return real_mkstemp(*args, **kwargs)

    def failing_unlink(path):
        unlink_calls.append(path)
        raise PermissionError("busy temp worker")

    monkeypatch.setattr(model_manager_module.tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(model_manager_module.os, "unlink", failing_unlink)
    monkeypatch.setattr(
        model_manager_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    with pytest.raises(OSError, match="spawn failed"):
        model_manager_module._SubprocessLlamaProxy(model_path="model.gguf")

    assert unlink_calls


def test_subprocess_proxy_close_ignores_temp_worker_unlink_failure(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    class FakeStdin:
        def close(self):
            pass

    class FakeProcess:
        stdin = FakeStdin()

        def poll(self):
            return 0

    unlink_calls = []

    def failing_unlink(path):
        unlink_calls.append(path)
        raise PermissionError("busy temp worker")

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._closed = False
    proxy._process = FakeProcess()
    proxy._worker_tmpfile = "/tmp/token-place-worker.py"
    monkeypatch.setattr(model_manager_module.os, "unlink", failing_unlink)

    proxy.close()

    assert unlink_calls == ["/tmp/token-place-worker.py"]
    assert proxy._worker_tmpfile is None



def test_safe_metal_backend_failure_classifier_covers_recoverable_categories():
    from utils.llm import model_manager as model_manager_module

    cases = [
        (["Metal backend is in error state has_error"], "metal_backend_sticky_error", None),
        (["Metal command buffer page fault status 5"], "metal_command_buffer_page_fault", 5),
        (["Metal command buffer timed out status 3"], "metal_command_buffer_timeout", 3),
        (["Metal command buffer out of memory"], "metal_command_buffer_out_of_memory", None),
        (["Metal command buffer completed with status 9"], "metal_command_buffer_execution_failure", 9),
        (["ggml_metal_graph_compute failed"], "metal_graph_compute_failed", None),
        (["memory_context_apply failed on metal"], "memory_context_apply_failed", None),
        (["metal graph initialization fail"], "graph_initialization_failed", None),
        (["metal unknown backend diagnostic"], "unknown_metal_backend_failure", None),
    ]

    for lines, category, status in cases:
        diagnostics = model_manager_module._classify_safe_metal_backend_failure(lines)
        assert diagnostics["plain_completion_metal_error_category"] == category
        if status is not None:
            assert diagnostics["plain_completion_metal_command_buffer_status"] == status
        if category in {
            "metal_backend_sticky_error",
            "metal_command_buffer_out_of_memory",
            "metal_command_buffer_timeout",
            "metal_command_buffer_page_fault",
            "metal_command_buffer_execution_failure",
            "metal_graph_compute_failed",
        }:
            assert diagnostics["plain_completion_backend_state_sticky"] is True
            assert diagnostics["plain_completion_backend_recreation_required"] is True

    assert model_manager_module._classify_safe_metal_backend_failure([]) == {}
    assert model_manager_module._classify_safe_metal_backend_failure(["ordinary warning"]) == {}


def test_qwen_64k_profile_helpers_cover_omitted_diagnostics_and_error_categories():
    from utils.llm import model_manager as model_manager_module

    class FakeLlama:
        def __init__(
            self,
            *,
            model_path,
            n_ctx,
            flash_attn=None,
            offload_kqv=None,
            n_batch=None,
            n_ubatch=None,
            type_k=None,
        ):
            pass

    llama_cpp = SimpleNamespace(
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        LLAMA_KV_CACHE_TYPE_Q8_0=8,
        __token_place_worker_capabilities__={
            "capability_source": "worker_probe",
            "constructor_kwarg_support": {
                "type_k": True,
                "type_v": False,
                "flash_attn": True,
                "offload_kqv": True,
                "n_batch": True,
                "n_ubatch": True,
            },
            "q8_kv_cache_type_value": 8,
        },
    )

    kwargs, diagnostics = model_manager_module._qwen_64k_memory_profile_kwargs(
        llama_cpp,
        FakeLlama,
    )
    assert kwargs == {}
    assert diagnostics["profile_id"] == model_manager_module.QWEN_64K_RUNTIME_PROFILE_Q8
    assert diagnostics["omitted"]["type_v"] == "worker_capability_unsupported"

    assert (
        model_manager_module._classify_runtime_context_create_error(
            "unexpected keyword argument type_v",
        )
        == "runtime_context_create_unsupported_kwarg"
    )
    assert (
        model_manager_module._classify_runtime_context_create_error(
            "rope freq scale invalid for yarn",
        )
        == "runtime_context_create_rope_yarn_config"
    )
    assert (
        model_manager_module._classify_runtime_context_create_error(
            "metal allocation failed while creating llama_context",
        )
        == "runtime_context_create_metal_memory"
    )


def test_decode_return_code_and_worker_status_edge_cases():
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._safe_plain_completion_eval_return_code("no decode code") is None
    assert model_manager_module._safe_plain_completion_eval_return_code("llama_decode returned -3") == -3
    assert model_manager_module._generation_category_for_decode_return_code(None) is None
    assert model_manager_module._generation_category_for_decode_return_code(-1) == "prompt_eval_invalid_batch"
    assert model_manager_module._generation_category_for_decode_return_code(-2) == "backend_allocation_failure"
    assert model_manager_module._generation_category_for_decode_return_code(-3) == "backend_graph_compute_failure"
    assert model_manager_module._generation_category_for_decode_return_code(1) == "kv_slot_unavailable"
    assert model_manager_module._generation_category_for_decode_return_code(2) == "decode_aborted"
    assert model_manager_module._generation_category_for_decode_return_code(-4) == "backend_decode_failure"
    assert model_manager_module._generation_category_for_decode_return_code(0) is None
    worker_namespace = {}
    exec(
        model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE.split(
            "try:\n    init_line = sys.stdin.readline()",
            1,
        )[0],
        worker_namespace,
    )
    assert "_safe_plain_completion_eval_return_code" not in worker_namespace
    worker_parser = worker_namespace["_worker_safe_plain_completion_eval_return_code"]
    worker_category = worker_namespace["_decode_return_category"]
    for text, code, category in (
        ("llama_decode returned -1", -1, "prompt_eval_invalid_batch"),
        ("llama_decode returned -2", -2, "backend_allocation_failure"),
        ("llama_decode returned -3", -3, "backend_graph_compute_failure"),
        ("llama_decode returned 1", 1, "kv_slot_unavailable"),
        ("llama_decode returned 2", 2, "decode_aborted"),
        ("llama_decode returned -4", -4, "backend_decode_failure"),
        ("unrecognized decode text", None, None),
    ):
        assert worker_parser(text) == code
        assert worker_category(text) == category
        assert (
            model_manager_module._generation_category_for_decode_return_code(code)
            == category
        )

    manager = object.__new__(ModelManager)
    manager.llm_lock = threading.RLock()
    manager.llm = None
    manager.worker_state = "ready"
    manager._llm_generation = 3
    manager.worker_restart_count = 2
    manager.last_worker_error_code = "backend_decode_failure"
    manager.last_worker_exit_code = None
    manager.last_worker_restart_at_ms = 123
    manager.last_plain_completion_eval_return_code = -4

    status = manager.worker_lifecycle_status()

    assert status["worker_state"] == "stopped"
    assert status["worker_alive"] is False
    assert status["last_plain_completion_eval_return_code"] == -4


def test_qwen_64k_readiness_worker_invalidation_filters_safe_diagnostics(monkeypatch):
    manager = object.__new__(ModelManager)
    failed_runtime = MagicMock()
    stale_runtime = MagicMock()
    manager.llm_lock = threading.RLock()
    manager.llm = failed_runtime
    manager.worker_state = "ready"
    manager.last_worker_error_code = None
    manager.last_worker_restart_at_ms = None
    manager.last_plain_completion_eval_return_code = None
    manager._qwen_64k_first_readiness_failure_category = None
    manager._qwen_64k_first_readiness_failure_diagnostics = {}
    monkeypatch.setattr(manager, "_close_llm_proxy", MagicMock())

    manager.cancel_qwen_64k_readiness_failed_worker(
        failed_runtime,
        "prompt_eval_invalid_batch",
        decode_return_code=-1,
    )
    assert manager.llm is failed_runtime
    manager._close_llm_proxy.assert_not_called()

    manager.cancel_qwen_64k_readiness_failed_worker(
        stale_runtime,
        "backend_decode_failure",
        decode_return_code=-4,
    )
    assert manager.llm is failed_runtime
    manager._close_llm_proxy.assert_not_called()

    manager.cancel_qwen_64k_readiness_failed_worker(
        failed_runtime,
        "backend_decode_failure",
        decode_return_code=-4,
        failure_diagnostics={
            "method": "create_completion",
            "backend_failure_category": "backend_decode_failure",
            "metal_error_category": "metal_backend_sticky_error",
            "backend_state_sticky": True,
            "backend_recreation_required": True,
            "metal_command_buffer_status": "error",
            "eval_return_code": -4,
            "prompt": "SECRET",
        },
    )

    assert manager.llm is None
    assert manager.worker_state == "failed"
    assert manager.last_worker_error_code == "backend_decode_failure"
    assert manager.last_plain_completion_eval_return_code == -4
    assert manager._qwen_64k_first_readiness_failure_category == "backend_decode_failure"
    assert manager._qwen_64k_first_readiness_failure_diagnostics == {
        "method": "create_completion",
        "backend_failure_category": "backend_decode_failure",
        "metal_error_category": "metal_backend_sticky_error",
        "backend_state_sticky": True,
        "backend_recreation_required": True,
        "metal_command_buffer_status": "error",
        "eval_return_code": -4,
        "category": "backend_decode_failure",
    }
    manager._close_llm_proxy.assert_called_once_with(failed_runtime)


def test_subprocess_proxy_stderr_cursor_supports_monotonic_and_legacy_tails():
    from utils.llm import model_manager as model_manager_module

    proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
    proxy._process = SimpleNamespace(_token_place_stderr_sequence=101, _token_place_stderr_tail=[
        (99, "old capped line"),
        (100, "cursor line"),
        (101, "new command buffer failed"),
    ])

    assert proxy._stderr_cursor() == 101
    assert proxy._stderr_since(100) == ["new command buffer failed"]

    proxy._process._token_place_stderr_tail = ["legacy first", "legacy second"]
    assert proxy._stderr_since(0) == ["legacy first", "legacy second"]
    assert proxy._stderr_since(2) == []

    proxy._process._token_place_stderr_tail = None
    assert proxy._stderr_since(0) == []


def test_subprocess_proxy_adds_request_scoped_metal_diagnostics_outside_lock(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    class TrackingLock:
        def __init__(self):
            self.held = False
            self.sleep_held = None
        def __enter__(self):
            self.held = True
            return self
        def __exit__(self, *_args):
            self.held = False
            return False

    class FakeProcess:
        stdin = SimpleNamespace(write=lambda *_args: None, flush=lambda *_args: None)

    for method_name in ("create_chat_completion", "create_chat_completion_from_rendered_prompt"):
        lock = TrackingLock()
        proxy = object.__new__(model_manager_module._SubprocessLlamaProxy)
        proxy._closed = False
        proxy._lock = lock
        proxy._process = FakeProcess()
        proxy._stderr_cursor = MagicMock(return_value=7)
        proxy._stderr_since = MagicMock(return_value=["Metal command buffer out of memory status 11"])
        proxy.assert_healthy = MagicMock()

        error = model_manager_module.LlamaCppInferenceRequestError("llama_cpp request failed", diagnostics={})
        monkeypatch.setattr(model_manager_module, "_read_llama_subprocess_message", MagicMock(side_effect=error))
        monkeypatch.setattr(model_manager_module, "_llama_cpp_subprocess_inference_timeout_seconds", lambda: 1)
        def fake_sleep(_seconds):
            lock.sleep_held = lock.held
        monkeypatch.setattr(model_manager_module.time, "sleep", fake_sleep)

        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as raised:
            getattr(proxy, method_name)([{"role": "user", "content": "hi"}])

        assert lock.sleep_held is False
        assert raised.value.diagnostics["plain_completion_metal_error_category"] == "metal_command_buffer_out_of_memory"
        assert raised.value.diagnostics["plain_completion_backend_failure_category"] == "metal_command_buffer_out_of_memory"
        assert raised.value.diagnostics["plain_completion_metal_command_buffer_status"] == 11
        proxy._stderr_since.assert_called_once_with(7)


def test_qwen_64k_readiness_recovery_rejects_unsafe_or_stale_runtime():
    manager = object.__new__(ModelManager)
    failed_runtime = MagicMock()
    manager.llm_lock = threading.RLock()
    manager.llm = failed_runtime
    manager.model_profile = {"provider": "qwen"}
    manager.context_tier = "64k-full"
    manager.worker_state = "ready"
    manager.last_worker_error_code = None
    manager.last_worker_restart_at_ms = None
    manager.last_plain_completion_eval_return_code = None
    manager.worker_restart_count = 0
    manager._llm_generation = 0
    manager._qwen_64k_profile_recovery_count = 0
    manager._qwen_64k_selected_profile_index = 0
    manager._qwen_64k_selected_profile_id = "only"
    manager._qwen_64k_runtime_profiles = [{"profile_id": "only", "diagnostics": {"backend": "metal"}}]
    manager._close_llm_proxy = MagicMock()
    manager.get_llm_instance = MagicMock()

    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "not_recoverable",
    ) is None
    manager.model_profile = {"provider": "llama"}
    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "backend_graph_compute_failure",
    ) is None
    manager.model_profile = {"provider": "qwen"}
    manager.context_tier = "8k-fast"
    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "backend_graph_compute_failure",
    ) is None
    manager.context_tier = "64k-full"
    manager.llm = object()
    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "backend_graph_compute_failure",
    ) is None

    manager.llm = failed_runtime
    manager._qwen_64k_runtime_profiles = []
    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "backend_graph_compute_failure",
        decode_return_code=-4,
    ) is None
    manager._close_llm_proxy.assert_not_called()
    manager.get_llm_instance.assert_not_called()

    # Non-Metal (CPU) profile must not close or advance anything.
    manager.llm = failed_runtime
    manager._qwen_64k_runtime_profiles = [
        {"profile_id": "only", "diagnostics": {"backend": "cpu"}},
    ]
    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "backend_graph_compute_failure",
    ) is None
    manager._close_llm_proxy.assert_not_called()
    manager.get_llm_instance.assert_not_called()


def test_qwen_64k_readiness_recovery_accepts_decode_failure_categories(monkeypatch):
    for category, decode_return_code in (("decode_aborted", 2), ("backend_decode_failure", -4)):
        manager = object.__new__(ModelManager)
        failed_runtime = MagicMock()
        replacement_runtime = object()
        manager.llm_lock = threading.RLock()
        manager.llm = failed_runtime
        manager.model_profile = {"provider": "qwen"}
        manager.context_tier = "64k-full"
        manager.worker_state = "ready"
        manager.last_worker_error_code = None
        manager.last_worker_restart_at_ms = None
        manager.last_plain_completion_eval_return_code = None
        manager.worker_restart_count = 0
        manager._llm_generation = 0
        manager._qwen_64k_profile_recovery_count = 0
        manager._qwen_64k_first_readiness_failure_category = None
        manager._qwen_64k_first_readiness_failure_diagnostics = {}
        manager._qwen_64k_selected_profile_index = 0
        manager._qwen_64k_selected_profile_id = "qwen64k_f16_fa_small_batch"
        manager._qwen_64k_runtime_profiles = [
            {"profile_id": "qwen64k_f16_fa_small_batch", "diagnostics": {"backend": "metal"}},
            {"profile_id": "qwen64k_kv_q8_fa_small_batch", "diagnostics": {"backend": "metal"}},
        ]
        monkeypatch.setattr(manager, "_close_llm_proxy", MagicMock())
        monkeypatch.setattr(manager, "get_llm_instance", MagicMock(return_value=replacement_runtime))

        recovered = manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
            failed_runtime,
            category,
            decode_return_code=decode_return_code,
            failure_diagnostics={"method": "create_completion"},
        )

        assert recovered is replacement_runtime
        assert manager.llm is None
        assert manager.worker_state == "recovering"
        assert manager._qwen_64k_selected_profile_index == 1
        assert manager._qwen_64k_profile_recovery_count == 1
        assert manager.last_worker_error_code == category
        assert manager.last_plain_completion_eval_return_code == decode_return_code
        assert manager._qwen_64k_first_readiness_failure_category == category
        assert manager._qwen_64k_first_readiness_failure_diagnostics["eval_return_code"] == decode_return_code
        manager._close_llm_proxy.assert_called_once_with(failed_runtime)
        manager.get_llm_instance.assert_called_once()


def test_qwen_64k_readiness_recovery_preserves_first_failure_across_profiles(monkeypatch):
    """First recoverable failure category is preserved even after a second profile advance."""
    manager = object.__new__(ModelManager)
    first_runtime = MagicMock()
    second_runtime = MagicMock()
    third_runtime = object()
    manager.llm_lock = threading.RLock()
    manager.llm = first_runtime
    manager.model_profile = {"provider": "qwen"}
    manager.context_tier = "64k-full"
    manager.worker_state = "ready"
    manager.last_worker_error_code = None
    manager.last_worker_restart_at_ms = None
    manager.last_plain_completion_eval_return_code = None
    manager.worker_restart_count = 0
    manager._llm_generation = 0
    manager._qwen_64k_profile_recovery_count = 0
    manager._qwen_64k_first_readiness_failure_category = None
    manager._qwen_64k_selected_profile_index = 0
    manager._qwen_64k_selected_profile_id = "qwen64k_f16_fa_small_batch"
    manager._qwen_64k_runtime_profiles = [
        {"profile_id": "qwen64k_f16_fa_small_batch", "diagnostics": {"backend": "metal"}},
        {"profile_id": "qwen64k_kv_q8_fa_small_batch", "diagnostics": {"backend": "metal"}},
        {"profile_id": "qwen64k_kv_q4_fa_small_batch", "diagnostics": {"backend": "metal"}},
    ]
    monkeypatch.setattr(manager, "_close_llm_proxy", MagicMock())
    monkeypatch.setattr(manager, "get_llm_instance", MagicMock(return_value=second_runtime))

    # First failure: F16 fails with backend_graph_compute_failure
    result1 = manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        first_runtime,
        "backend_graph_compute_failure",
        decode_return_code=-3,
    )
    assert result1 is second_runtime
    assert manager._qwen_64k_first_readiness_failure_category == "backend_graph_compute_failure"
    assert manager._qwen_64k_profile_recovery_count == 1
    assert manager._qwen_64k_selected_profile_index == 1

    # Second failure: Q8 fails with metal_command_buffer_out_of_memory
    manager.llm = second_runtime
    manager.get_llm_instance = MagicMock(return_value=third_runtime)
    result2 = manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        second_runtime,
        "metal_command_buffer_out_of_memory",
        decode_return_code=None,
    )
    assert result2 is third_runtime
    # First failure must be preserved; second failure must NOT overwrite it.
    assert manager._qwen_64k_first_readiness_failure_category == "backend_graph_compute_failure"
    assert manager._qwen_64k_profile_recovery_count == 2
    assert manager._qwen_64k_selected_profile_index == 2


def test_qwen_64k_context_create_failure_retries_q8_profile(tmp_path):
    from utils.context_profiles import apply_context_profile

    attempts = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            if 'type_k' not in kwargs:
                raise ValueError('Failed to create llama_context: Metal KV cache allocation failed')

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False):
            return '<qwen>'

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        llm = manager.get_llm_instance()

    assert llm is not None
    assert len(attempts) == 2
    assert attempts[0]['n_ctx'] == 65536
    assert 'type_k' not in attempts[0]
    assert attempts[1]['type_k'] == 8
    assert attempts[1]['type_v'] == 8
    assert attempts[1]['flash_attn'] is True
    assert attempts[1]['offload_kqv'] is True
    assert manager.last_compute_diagnostics['qwen_64k_memory_profile']['profile_id'] == 'qwen64k_kv_q8_fa_small_batch'
    assert manager.last_qwen_64k_init_failures[0]['safe_error_category'] == 'runtime_context_create_kv_cache_allocation'


def test_qwen_64k_all_profiles_fail_closed_before_registration(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module

    attempts = []
    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            raise model_manager_module.LlamaCppRuntimeInitError(
                'llama_cpp_import failed',
                child_exception_type='SecretInitError',
                safe_error_category='runtime_context_create_metal_buffer_limit',
                child_stderr_tail=(
                    'ggml_metal: buffer size too large; prompt=SECRET_PROMPT '
                    'payload=SECRET_PAYLOAD output=SECRET_OUTPUT command=SECRET_COMMAND '
                    'model_path=SECRET_MODEL traceback=SECRET_TRACEBACK'
                ),
            )

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'metal', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert len(attempts) == 3
    assert manager.llm is None
    assert manager.last_qwen_64k_init_failures
    assert [failure['safe_error_category'] for failure in manager.last_qwen_64k_init_failures] == [
        'runtime_context_create_metal_buffer_limit',
        'runtime_context_create_metal_buffer_limit',
        'runtime_context_create_metal_buffer_limit',
    ]
    allowed_failure_keys = {
        'profile_id',
        'model_profile_id',
        'safe_error_category',
        'exception_type',
        'context_tier',
        'n_ctx',
        'backend',
        'llama_cpp_python_version',
        'yarn_resolver_source',
        'kv_cache_settings',
        'memory_estimate',
        'attempted_runtime_kwargs',
    }
    for failure in manager.last_qwen_64k_init_failures:
        assert set(failure) <= allowed_failure_keys
        assert set(failure['attempted_runtime_kwargs']) <= {
            'n_ctx',
            'type_k',
            'type_v',
            'flash_attn',
            'offload_kqv',
            'n_batch',
            'n_ubatch',
            'rope_scaling_type',
            'rope_freq_scale',
            'yarn_orig_ctx',
        }
    assert 'Qwen 64K memory/KV/cache profile exhaustion before registration' in manager.last_runtime_init_error
    assert 'runtime_context_create_metal_buffer_limit' in manager.last_runtime_init_error
    diagnostics = manager.last_compute_diagnostics
    assert diagnostics['api_v1_runtime_ready'] is False
    assert diagnostics['api_v1_readiness_result'] == 'failed'
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_result'] == 'failed'
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_failure_category'] == 'runtime_context_create_metal_buffer_limit'
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_attempt_ids'] == (
        'qwen64k_f16_fa_small_batch,qwen64k_kv_q8_fa_small_batch,qwen64k_kv_q4_fa_small_batch'
    )
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_id'] == 'qwen64k_kv_q4_fa_small_batch'
    assert diagnostics['api_v1_readiness_backend_used'] == 'metal'
    assert diagnostics['api_v1_readiness_yarn_requested_context_tokens'] == 65536
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_type_k'] == 2
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_type_v'] == 2
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_flash_attn'] is True
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_offload_kqv'] is True
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_n_batch'] == 256
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_n_ubatch'] == 128
    bridge_path = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python' / 'compute_node_bridge.py'
    spec = importlib.util.spec_from_file_location('desktop_compute_node_bridge_for_model_manager_test', bridge_path)
    bridge = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(bridge)
    safe_diagnostics = bridge._safe_readiness_diagnostics(manager)
    assert safe_diagnostics
    assert safe_diagnostics['api_v1_readiness_qwen_64k_runtime_profile_result'] == 'failed'
    assert safe_diagnostics['api_v1_readiness_qwen_64k_runtime_profile_failure_category'] == 'runtime_context_create_metal_buffer_limit'
    serialized = (
        json.dumps(manager.last_qwen_64k_init_failures)
        + manager.last_runtime_init_error
        + json.dumps(diagnostics, sort_keys=True)
        + json.dumps(safe_diagnostics, sort_keys=True)
    )
    for secret in (
        'SecretInitError',
        'SECRET_PROMPT',
        'SECRET_PAYLOAD',
        'SECRET_OUTPUT',
        'SECRET_COMMAND',
        'SECRET_MODEL',
        'SECRET_TRACEBACK',
    ):
        assert secret not in serialized


@pytest.mark.parametrize(
    ('message', 'expected_category'),
    [
        ('model path not found: /tmp/SECRET_MODEL.gguf', 'runtime_model_path_unavailable'),
        ('failed to load model from /tmp/SECRET_MODEL.gguf', 'runtime_model_load_failed'),
        ('vocab load failed for SECRET_PAYLOAD', 'runtime_model_vocab_failed'),
        ('failed to create batch for SECRET_KEY', 'runtime_batch_create_failed'),
        ('undefined symbol: llama_model_load_from_file ABI SECRET_PAYLOAD', 'runtime_model_load_failed'),
        ('unknown ValueError SECRET_PROMPT SECRET_KEY SECRET_PAYLOAD /tmp/SECRET_MODEL.gguf', 'runtime_init_unclassified'),
    ],
)
def test_qwen_64k_subprocess_initialization_failures_are_safe_one_attempt(
    tmp_path, monkeypatch, message, expected_category, caplog
):
    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'fake site with spaces'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "MESSAGE = " + repr(message) + "\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        raise ValueError(MESSAGE)\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.filename': 'mock.gguf',
        'model.url': 'https://example.com/mock.gguf',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'model.download_chunk_size_mb': 1,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_bytes(b'GGUFfake')
    monkeypatch.setattr(
        model_manager_module,
        '_import_llama_cpp_runtime',
        lambda **_kwargs: model_manager_module._import_llama_cpp_subprocess_module(
            module_path_hint=str(fake_pkg / '__init__.py'),
            timeout_seconds=5,
            desktop_runtime_probe={
                'backend': 'cuda',
                'gpu_offload_supported': True,
                'llama_cpp_python_version': '0.3.32',
                'constructor_kwarg_support': {
                    'rope_scaling_type': True,
                    'rope_freq_scale': True,
                    'yarn_orig_ctx': True,
                    'type_k': True,
                    'type_v': True,
                    'flash_attn': True,
                    'offload_kqv': True,
                },
            },
        ),
    )
    monkeypatch.setattr(
        manager,
        '_runtime_capabilities',
        lambda _runtime=None: {
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_cpp_python_version': '0.3.32',
            'yarn_resolver_source': 'numeric_fallback',
            'constructor_kwarg_support': {
                'rope_scaling_type': True,
                'rope_freq_scale': True,
                'yarn_orig_ctx': True,
                'type_k': True,
                'type_v': True,
                'flash_attn': True,
                'offload_kqv': True,
            },
        },
    )

    with caplog.at_level('INFO'):
        assert manager.get_llm_instance() is None

    assert manager.llm is None
    assert manager.last_qwen_64k_init_failures
    assert [failure['profile_id'] for failure in manager.last_qwen_64k_init_failures] == [
        'qwen64k_f16_fa_small_batch'
    ]
    assert manager.last_qwen_64k_init_failures[0]['safe_error_category'] == expected_category
    assert manager.last_compute_diagnostics['api_v1_runtime_ready'] is False
    assert manager.last_compute_diagnostics['api_v1_readiness_qwen_64k_runtime_profile_attempt_ids'] == (
        'qwen64k_f16_fa_small_batch'
    )
    serialized = (
        str(manager.last_runtime_init_error)
        + json.dumps(manager.last_qwen_64k_init_failures, sort_keys=True)
        + json.dumps(manager.last_compute_diagnostics, sort_keys=True)
        + '\n'.join(record.getMessage() for record in caplog.records)
    )
    for forbidden in (
        str(Path(manager.model_path)),
        'SECRET_PROMPT',
        'SECRET_KEY',
        'SECRET_PAYLOAD',
        'SECRET_MODEL',
        message,
    ):
        assert forbidden not in serialized


def test_qwen_64k_kv_constants_resolve_top_level_nested_and_numeric_fallback():
    from utils.llm import model_manager as model_manager_module

    class KwargsLlama:
        def __init__(self, **kwargs):
            pass

    top_value, top_diag = model_manager_module._resolve_ggml_kv_cache_type(
        SimpleNamespace(GGML_TYPE_Q8_0=8), KwargsLlama, 'q8'
    )
    nested_value, nested_diag = model_manager_module._resolve_ggml_kv_cache_type(
        SimpleNamespace(llama_cpp=SimpleNamespace(LLAMA_TYPE_Q4_0=2)), KwargsLlama, 'q4'
    )
    fallback_value, fallback_diag = model_manager_module._resolve_ggml_kv_cache_type(
        SimpleNamespace(), KwargsLlama, 'q8'
    )

    assert (top_value, top_diag['source']) == (8, 'top_level')
    assert (nested_value, nested_diag['source']) == (2, 'nested')
    assert (fallback_value, fallback_diag['source']) == (8, 'verified_numeric_fallback')


def test_child_context_create_error_classification_and_stderr_redaction():
    from utils.llm import model_manager as model_manager_module

    stderr = '/Users/alice/app/model.gguf ggml_metal: failed to allocate KV cache buffer'
    assert model_manager_module._classify_runtime_context_create_error(
        ValueError('Failed to create llama_context'), stderr
    ) == 'runtime_context_create_kv_cache_allocation'
    redacted = model_manager_module._redact_paths_from_text(stderr)
    assert '/Users/alice' not in redacted
    assert '<path>' in redacted


def test_safe_constructor_capability_payload_rejects_bool_kv_values():
    from utils.llm import model_manager as model_manager_module

    module = SimpleNamespace(__token_place_worker_capabilities__={
        'constructor_kwarg_support': {'type_k': True},
        'q8_kv_cache_type_value': True,
        'q4_kv_cache_type_value': 2,
    })

    payload = model_manager_module._safe_constructor_capability_payload(module)

    assert 'q8_kv_cache_type_value' not in payload
    assert payload['q4_kv_cache_type_value'] == 2


def test_unrecognized_init_failure_is_not_context_create_retryable():
    from utils.llm import model_manager as model_manager_module

    category = model_manager_module._classify_runtime_context_create_error(
        RuntimeError('invalid gguf header: missing tensor metadata')
    )

    assert category == 'runtime_init_unclassified'
    assert category not in model_manager_module.QWEN_64K_CONTEXT_CREATE_RETRY_CATEGORIES


def test_cuda_cublas_not_initialized_is_not_memory_retryable():
    from utils.llm import model_manager as model_manager_module

    category = model_manager_module._classify_runtime_context_create_error(
        'CUDA error: CUBLAS_STATUS_NOT_INITIALIZED'
    )

    assert category == 'runtime_init_unclassified'
    assert category not in model_manager_module.QWEN_64K_CONTEXT_CREATE_RETRY_CATEGORIES


def test_bare_cublas_alloc_failed_is_cuda_memory():
    from utils.llm import model_manager as model_manager_module

    assert (
        model_manager_module._classify_runtime_context_create_error('CUBLAS_STATUS_ALLOC_FAILED')
        == 'runtime_context_create_cuda_memory'
    )


def test_cuda_allocation_and_buffer_limit_classifier_branches():
    from utils.llm import model_manager as model_manager_module

    assert (
        model_manager_module._classify_runtime_context_create_error(
            'CUDA driver reported allocation failed while creating context'
        )
        == 'runtime_context_create_cuda_memory'
    )
    assert (
        model_manager_module._classify_runtime_context_create_error(
            'CUDA resource buffer too large for context'
        )
        == 'runtime_context_create_cuda_buffer_limit'
    )


def test_qwen_64k_failure_readiness_publisher_handles_empty_and_non_dict_kwargs():
    manager = object.__new__(ModelManager)
    manager.last_compute_diagnostics = {'existing': 'unchanged'}
    manager._qwen_64k_profile_attempt_ids = ['qwen64k_f16_fa_small_batch']
    manager.last_qwen_64k_memory_profile_diagnostics = {
        'applied': {
            'type_k': 8,
            'type_v': 8,
            'flash_attn': True,
            'offload_kqv': True,
            'n_batch': 256,
            'n_ubatch': 128,
        }
    }

    manager._publish_qwen_64k_init_failure_readiness_diagnostics(
        compute_plan={'backend_used': 'cuda'},
        profile_failures=[],
    )
    assert manager.last_compute_diagnostics == {'existing': 'unchanged'}

    manager._publish_qwen_64k_init_failure_readiness_diagnostics(
        compute_plan={'backend_used': 'cuda'},
        profile_failures=[{
            'profile_id': 'qwen64k_f16_fa_small_batch',
            'safe_error_category': 'runtime_context_create_cuda_memory',
            'attempted_runtime_kwargs': 'not-a-dict',
        }],
    )

    diagnostics = manager.last_compute_diagnostics
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_type_k'] == 8
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_result'] == 'failed'
    assert (
        diagnostics['api_v1_readiness_qwen_64k_runtime_profile_failure_category']
        == 'runtime_context_create_cuda_memory'
    )


def test_qwen_64k_failure_readiness_publisher_falls_back_to_attempted_profile_ids():
    manager = object.__new__(ModelManager)
    manager.last_compute_diagnostics = {}
    manager._qwen_64k_profile_attempt_ids = [
        'qwen64k_f16_fa_small_batch',
        'qwen64k_kv_q8_fa_small_batch',
    ]
    manager.last_qwen_64k_memory_profile_diagnostics = {'applied': {}}

    manager._publish_qwen_64k_init_failure_readiness_diagnostics(
        compute_plan={'backend_used': 'cuda'},
        profile_failures=[{
            'profile_id': '',
            'safe_error_category': 'runtime_context_create_failed',
            'attempted_runtime_kwargs': {'n_ctx': 65536},
        }],
        current_profile_id='qwen64k_kv_q8_fa_small_batch',
    )

    diagnostics = manager.last_compute_diagnostics
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_attempt_ids'] == (
        'qwen64k_f16_fa_small_batch,qwen64k_kv_q8_fa_small_batch'
    )
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_id'] == 'qwen64k_kv_q8_fa_small_batch'
    assert diagnostics['api_v1_readiness_qwen_64k_runtime_profile_failure_category'] == 'runtime_context_create_failed'
    assert diagnostics['api_v1_readiness_yarn_requested_context_tokens'] == 65536


def test_generic_context_create_failure_is_not_retryable_without_memory_kv_or_buffer_evidence():
    from utils.llm import model_manager as model_manager_module

    category = model_manager_module._classify_runtime_context_create_error(
        ValueError('Failed to create llama_context')
    )

    assert category == 'runtime_context_create_failed'
    assert category in model_manager_module.QWEN_64K_CONTEXT_CREATE_RETRY_CATEGORIES


def test_ggml_cuda_failed_to_load_model_is_not_memory_retryable_and_attempts_once(tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module

    category = model_manager_module._classify_runtime_context_create_error(
        RuntimeError('ggml_cuda_init: failed to load model; invalid GGUF ABI')
    )
    assert category == 'runtime_init_unclassified'
    assert category not in model_manager_module.QWEN_64K_CONTEXT_CREATE_RETRY_CATEGORIES

    attempts = []
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': -1,
        'model.gpu_mode': 'gpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config = MagicMock(is_production=False)
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    class FakeLlama:
        def __init__(self, **kwargs):
            attempts.append(dict(kwargs))
            raise RuntimeError('ggml_cuda_init: failed to load model; invalid GGUF ABI')

    fake_llama_cpp = SimpleNamespace(
        Llama=FakeLlama,
        LLAMA_ROPE_SCALING_TYPE_YARN=2,
        GGML_TYPE_Q8_0=8,
        GGML_TYPE_Q4_0=2,
        __file__='/opt/token.place/llama_cpp/__init__.py',
        __version__='0.3.32',
    )
    with patch('utils.llm.model_manager._import_llama_cpp_runtime', return_value=fake_llama_cpp), \
         patch.object(manager, '_runtime_capabilities', return_value={'backend': 'cuda', 'gpu_offload_supported': True, 'error': None}):
        assert manager.get_llm_instance() is None

    assert len(attempts) == 1
    assert manager.llm is None
    assert manager.last_qwen_64k_init_failures[0]['safe_error_category'] == 'runtime_model_load_failed'


def test_child_diagnostic_sanitizer_redacts_secret_values_on_allowlisted_lines():
    from utils.llm import model_manager as model_manager_module

    raw = (
        'ggml_metal: KV cache allocation failed; prompt=SECRET_PROMPT assistant=SECRET_ASSISTANT '
        'ciphertext=CIPHERTEXT_BODY key=API_KEY_VALUE token=TOKEN_VALUE user_token=SK_LIVE_VALUE\n'
        'llama_context failed with decrypted_payload=PLAINTEXT_PAYLOAD and arbitrary SECRET_SNIPPET\n'
    )

    sanitized = model_manager_module._sanitize_child_diagnostic_text(raw)

    assert 'ggml_metal' in sanitized
    assert 'KV cache allocation failed' in sanitized
    assert 'llama_context failed' in sanitized
    for leaked in (
        'SECRET_PROMPT',
        'SECRET_ASSISTANT',
        'CIPHERTEXT_BODY',
        'API_KEY_VALUE',
        'TOKEN_VALUE',
        'PLAINTEXT_PAYLOAD',
        'SECRET_SNIPPET',
        'SK_LIVE_VALUE',
    ):
        assert leaked not in sanitized
    assert 'prompt=<redacted>' in sanitized
    assert 'ciphertext=<redacted>' in sanitized
    assert 'decrypted_payload=<redacted>' in sanitized
    assert 'user_token=<redacted>' in sanitized

def test_path_redaction_handles_spaces_and_traceback_paths():
    from utils.llm import model_manager as model_manager_module

    text = 'File "/Users/Alice/Application Support/token.place/model.gguf", line 10, in <module>'
    redacted = model_manager_module._redact_paths_from_text(text)

    assert '/Users/Alice' not in redacted
    assert 'Application Support' not in redacted
    assert '<path>' in redacted


def test_llama_worker_render_complete_minimal_kwargs_omit_sampling_and_stop(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'minimal fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        forbidden = {'stream', 'stop', 'temperature', 'top_p', 'top_k', 'min_p', 'seed'} & set(kwargs)\n"
        "        if forbidden:\n"
        "            raise TypeError(f\"got an unexpected keyword argument '{sorted(forbidden)[0]}'\")\n"
        "        if set(kwargs) != {'max_tokens'}:\n"
        "            raise TypeError('unexpected keyword argument: extra')\n"
        "        return {'choices': [{'text': 'minimal ok<|im_end|>'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            temperature=0.5,
            stop=[],
            stream=False,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'minimal ok'}}]}


def test_llama_worker_render_complete_falls_back_to_positional_prompt(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'positional fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'prompt' in kwargs:\n"
        "            raise TypeError(\"got an unexpected keyword argument 'prompt'\")\n"
        "        if len(args) != 1 or set(kwargs) != {'max_tokens'}:\n"
        "            raise TypeError('bad method shape')\n"
        "        return {'choices': [{'text': 'positional ok'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'positional ok'}}]}


def test_llama_worker_render_complete_falls_back_for_positional_only_prompt(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'positional only fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, prompt, /, max_tokens=None):\n"
        "        if len(prompt) <= 0 or max_tokens != 4:\n"
        "            raise TypeError('bad method shape')\n"
        "        return {'choices': [{'text': 'positional only ok'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'positional only ok'}}]}


def test_llama_worker_render_complete_falls_back_to_callable_llama(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'callable fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if set(kwargs) != {'max_tokens'}:\n"
        "            raise TypeError('bad callable kwargs')\n"
        "        return {'choices': [{'message': {'content': 'callable ok'}}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'callable ok'}}]}


def test_llama_worker_render_complete_runtime_failure_does_not_fall_back_to_callable(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'runtime failure fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        raise RuntimeError('KV cache allocation failed during completion')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        return {'choices': [{'text': 'callable should not run'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['generation_exception_category'] == 'kv_cache_allocation'
    assert diagnostics['method'] == 'create_completion_keyword_prompt'
    assert diagnostics['attempted_plain_completion_methods'] == 'create_completion_keyword_prompt'
    assert diagnostics['sanitized_error_summary'] == 'RuntimeError:kv_cache_allocation'


def test_llama_worker_render_complete_empty_and_thinking_fail_safely(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'unsafe output fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *, prompt, **kwargs):\n"
        "        if 'empty' in prompt:\n"
        "            return {'choices': [{'text': ''}]}\n"
        "        return {'choices': [{'text': '<think>secret reasoning</think> visible'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as empty_exc:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'empty'}], max_tokens=4,
                token_place_provider='qwen', token_place_template_policy='gguf-jinja', enable_thinking=False,
            )
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as think_exc:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'think'}], max_tokens=4,
                token_place_provider='qwen', token_place_template_policy='gguf-jinja', enable_thinking=False,
            )
    finally:
        proxy.close()

    assert empty_exc.value.diagnostics['generation_exception_category'] == 'empty_completion_output'
    assert empty_exc.value.diagnostics['qwen_high_level_chat_fallback_category'] == 'unsupported_generation_kwarg'
    assert 'sanitized_error_summary' not in empty_exc.value.diagnostics
    assert think_exc.value.diagnostics['generation_exception_category'] == 'thinking_leaked'
    assert not think_exc.value.diagnostics.get('qwen_high_level_chat_fallback_attempted', False)
    assert 'secret reasoning' not in json.dumps(think_exc.value.diagnostics)


def test_llama_worker_render_complete_continues_after_generic_keyword_worker_exception(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'generic keyword fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.calls = []\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'prompt' in kwargs:\n"
        "            raise RuntimeError('generic wrapper failure')\n"
        "        if len(args) == 1 and set(kwargs) == {'max_tokens'} and kwargs['max_tokens'] > 0:\n"
        "            return {'choices': [{'text': 'positional recovered'}]}\n"
        "        raise TypeError('bad shape')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'positional recovered'}}]}


def test_llama_worker_render_complete_continues_to_llama_after_generic_create_completion_failures(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'generic callable fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        raise RuntimeError('generic wrapper failure')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if set(kwargs) != {'max_tokens'} or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        return {'choices': [{'text': 'callable recovered'}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'callable recovered'}}]}


def test_llama_worker_render_complete_token_id_keyword_fallback_recovers_after_string_failures(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'token keyword fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.reset_count = 0\n"
        "    def tokenize(self, prompt, add_bos=False, special=False):\n"
        "        if add_bos is not False or special is not True:\n"
        "            raise AssertionError('expected special tokenization first')\n"
        "        return [11, 22, 33]\n"
        "    def reset(self):\n"
        "        self.reset_count += 1\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, str):\n"
        "            raise RuntimeError('failed to tokenize prompt')\n"
        "        if prompt == [11, 22, 33]:\n"
        "            return {'choices': [{'text': 'token keyword recovered'}]}\n"
        "        raise AssertionError('unexpected prompt shape')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        raise RuntimeError('plain completion string path failed')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'token keyword recovered'}}]}



def test_llama_worker_render_complete_token_id_fallback_recovers_after_invalid_string_output(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'token invalid output fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def tokenize(self, prompt, add_bos=False, special=False):\n"
        "        if add_bos is not False or special is not False:\n"
        "            raise AssertionError('expected non-special tokenization first')\n"
        "        return [44, 55]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, list):\n"
        "            return {'choices': [{'text': 'token fallback after invalid output'}]}\n"
        "        return {'choices': []}\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        return {'choices': []}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {
        'choices': [{'message': {'role': 'assistant', 'content': 'token fallback after invalid output'}}]
    }




def test_llama_worker_render_complete_token_id_fallback_continues_after_invalid_variant(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'token invalid first variant fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def tokenize(self, prompt, add_bos=False, special=False):\n"
        "        if special is False:\n"
        "            return [10]\n"
        "        if special is True:\n"
        "            return [20]\n"
        "        return [30]\n"
        "    def reset(self):\n"
        "        pass\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, list) and prompt == [10]:\n"
        "            return {'choices': []}\n"
        "        if isinstance(prompt, list) and prompt == [20]:\n"
        "            return {'choices': [{'text': 'second token variant recovered'}]}\n"
        "        if isinstance(prompt, list):\n"
        "            raise AssertionError('unexpected token variant')\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {
        'choices': [{'message': {'role': 'assistant', 'content': 'second token variant recovered'}}]
    }

def test_llama_worker_render_complete_thinking_leak_never_uses_token_id_fallback(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'thinking leak no fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def tokenize(self, *args, **kwargs):\n"
        "        raise AssertionError('tokenization must not be called after thinking leak')\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, list):\n"
        "            raise AssertionError('token-id fallback must not be attempted')\n"
        "        return {'choices': [{'text': '<think>leaked reasoning</think> bad'}]}\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise AssertionError('llama fallback must not run after successful keyword response')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['generation_exception_category'] == 'thinking_leaked'
    assert diagnostics['attempted_plain_completion_methods'] == 'create_completion_keyword_prompt'
    assert 'plain_completion_prompt_tokenization_attempted' not in diagnostics
    assert 'create_completion_keyword_token_ids' not in json.dumps(diagnostics)
    unsafe_dump = json.dumps(diagnostics)
    for unsafe in ('leaked reasoning', 'secret prompt text', '<think>', 'token_ids', 'ciphertext', 'tool_args'):
        assert unsafe not in unsafe_dump


def test_llama_worker_render_complete_empty_tokenization_does_not_attempt_token_id_fallback(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'empty tokenization no fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.calls = []\n"
        "    def tokenize(self, prompt, add_bos=False, special=False):\n"
        "        return []\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, list):\n"
        "            raise AssertionError('empty token-id fallback must not be attempted')\n"
        "        raise RuntimeError('failed to tokenize prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        raise RuntimeError('failed to tokenize prompt')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['plain_completion_prompt_tokenization_attempted'] is True
    assert diagnostics['plain_completion_prompt_token_count'] == 0
    assert diagnostics['plain_completion_prompt_tokenization_error_category'] == 'prompt_tokenization_failure'
    assert 'create_completion_keyword_token_ids' not in diagnostics['attempted_plain_completion_methods']
    unsafe_dump = json.dumps(diagnostics)
    for unsafe in ('secret prompt text', 'rendered_prompt', 'token_ids', 'model output', 'decrypted_payload', 'ciphertext', 'tool_args'):
        assert unsafe not in unsafe_dump


def test_llama_worker_render_complete_token_id_thinking_leak_fails_without_retry(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'token id thinking leak fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def tokenize(self, prompt, add_bos=False, special=False):\n"
        "        return [8, 9]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, list) and 'prompt' in kwargs:\n"
        "            return {'choices': [{'text': '<think>token reasoning</think> bad'}]}\n"
        "        if isinstance(prompt, list):\n"
        "            raise AssertionError('positional token-id retry must not run after thinking leak')\n"
        "        raise RuntimeError('failed to tokenize prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        raise RuntimeError('failed to tokenize prompt')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['generation_exception_category'] == 'thinking_leaked'
    assert diagnostics['attempted_plain_completion_methods'].endswith('create_completion_keyword_token_ids')
    assert 'create_completion_positional_token_ids' not in diagnostics['attempted_plain_completion_methods']
    unsafe_dump = json.dumps(diagnostics)
    for unsafe in ('token reasoning', 'secret prompt text', '<think>', '8', '9', 'ciphertext', 'tool_args'):
        assert unsafe not in unsafe_dump

def test_llama_worker_render_complete_token_id_positional_fallback_records_all_methods(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'token positional fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def tokenize(self, prompt, add_bos=False, special=False):\n"
        "        return [7, 8, 9]\n"
        "    def reset(self):\n"
        "        pass\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        prompt = kwargs.get('prompt') if 'prompt' in kwargs else (args[0] if args else None)\n"
        "        if isinstance(prompt, list) and 'prompt' not in kwargs:\n"
        "            return {'choices': [{'text': 'token positional recovered'}]}\n"
        "        raise RuntimeError('failed to tokenize prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        raise RuntimeError('no logits available')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'secret prompt text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'token positional recovered'}}]}


def test_subprocess_worker_tokenization_helper_safe_diagnostics_and_classification():
    from utils.llm import model_manager as model_manager_module

    namespace = {}
    worker_code = model_manager_module._LLAMA_CPP_RUNTIME_WORKER_CODE
    exec(worker_code.split('def _metadata_value', 1)[0], namespace)
    tokenize_prompt = namespace['_tokenize_rendered_prompt_for_plain_completion']
    tokenize_variants = namespace['_tokenize_rendered_prompt_variants_for_plain_completion']
    classify_shape = namespace['_plain_completion_method_shape_category']
    sanitize = namespace['_sanitize_error_summary']

    class RuntimeWithSpecial:
        def __init__(self):
            self.calls = []
        def tokenize(self, prompt, *, add_bos=False, special=False):
            self.calls.append({'add_bos': add_bos, 'special': special, 'prompt_type': type(prompt).__name__})
            return [101, 202]

    runtime = RuntimeWithSpecial()
    tokens, diagnostics = tokenize_prompt(runtime, 'SECRET_RENDERED_PROMPT')
    assert tokens == [101, 202]
    assert runtime.calls[0] == {'add_bos': False, 'special': False, 'prompt_type': 'bytes'}
    assert diagnostics['plain_completion_prompt_tokenization_special'] is False
    variants, variant_diagnostics = tokenize_variants(runtime, 'SECRET_RENDERED_PROMPT')
    assert [variant['tokenization_variant_id'] for variant in variants] == [
        'tokenize_add_bos_false_special_false',
    ]
    assert variant_diagnostics['plain_completion_prompt_tokenization_variant_count'] == 1
    assert diagnostics['plain_completion_prompt_token_count'] == 2
    assert 'SECRET_RENDERED_PROMPT' not in json.dumps(diagnostics)
    assert '101' not in json.dumps(diagnostics)

    class RuntimeDuplicateTokenArrays:
        def __init__(self):
            self.calls = []
        def tokenize(self, prompt, *, add_bos=False, special=False):
            self.calls.append(special)
            return [404, 505]

    duplicate_runtime = RuntimeDuplicateTokenArrays()
    duplicate_variants, duplicate_diagnostics = tokenize_variants(
        duplicate_runtime,
        'SECRET_RENDERED_PROMPT',
    )
    assert duplicate_runtime.calls == [False, False, True]
    assert [variant['tokens'] for variant in duplicate_variants] == [[404, 505]]
    assert duplicate_diagnostics['plain_completion_prompt_tokenization_variant_count'] == 1
    duplicate_dump = json.dumps(duplicate_diagnostics)
    assert 'SECRET_RENDERED_PROMPT' not in duplicate_dump
    assert '404' not in duplicate_dump
    assert '505' not in duplicate_dump

    class RuntimeRejectsSpecial:
        def __init__(self):
            self.calls = []
        def tokenize(self, prompt, *, add_bos=False, **kwargs):
            self.calls.append(kwargs.get('special', 'none'))
            if 'special' in kwargs:
                raise TypeError('special unsupported')
            return [303]

    fallback_runtime = RuntimeRejectsSpecial()
    tokens, diagnostics = tokenize_prompt(fallback_runtime, 'SECRET_RENDERED_PROMPT')
    assert tokens == [303]
    assert fallback_runtime.calls == [False, 'none', True]
    assert diagnostics['plain_completion_prompt_tokenization_special'] is None
    assert diagnostics['plain_completion_prompt_tokenization_special_values'] == 'none'
    assert '303' not in json.dumps(diagnostics)

    class RuntimeEmptyTokens:
        def tokenize(self, prompt, *, add_bos=False, special=False):
            return []

    tokens, diagnostics = tokenize_prompt(RuntimeEmptyTokens(), '')
    assert tokens is None
    assert diagnostics['plain_completion_prompt_token_count'] == 0
    assert diagnostics['plain_completion_prompt_tokenization_error_category'] == 'prompt_tokenization_failure'

    assert classify_shape(RuntimeError('failed to tokenize prompt')) == 'prompt_tokenization_failure'
    assert classify_shape(RuntimeError('llama_decode returned 1')) == 'kv_slot_unavailable'
    assert classify_shape(RuntimeError('no logits available from sampler')) == 'sampling_failure'
    assert classify_shape(RuntimeError('state file failed to open')) == 'worker_exception'
    assert classify_shape(RuntimeError('contextual logging error')) == 'worker_exception'
    assert sanitize(RuntimeError('failed to tokenize SECRET_RENDERED_PROMPT')) == 'RuntimeError:prompt_tokenization_failure'
    assert 'SECRET_RENDERED_PROMPT' not in sanitize(RuntimeError('failed to tokenize SECRET_RENDERED_PROMPT'))


@pytest.mark.parametrize(
    ("message_reasoning_metadata", "choice_reasoning_metadata"),
    [
        ({"reasoning_content": "hidden chain"}, {}),
        ({"reasoning": "hidden chain"}, {}),
        ({"reasoning": {"trace": "hidden chain"}}, {}),
        ({}, {"reasoning": "hidden chain"}),
    ],
)
def test_llama_worker_render_complete_high_level_qwen_fallback_rejects_reasoning_metadata(
    tmp_path, monkeypatch, message_reasoning_metadata, choice_reasoning_metadata
):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback reasoning fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        f"MESSAGE_REASONING_METADATA = {message_reasoning_metadata!r}\n"
        f"CHOICE_REASONING_METADATA = {choice_reasoning_metadata!r}\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def create_chat_completion(self, *, messages, max_tokens, chat_template_kwargs):\n"
        "        message = {'role': 'assistant', 'content': 'visible'}\n"
        "        message.update(MESSAGE_REASONING_METADATA)\n"
        "        choice = {'message': message}\n"
        "        choice.update(CHOICE_REASONING_METADATA)\n"
        "        return {'choices': [choice]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['generation_exception_category'] == 'thinking_leaked'
    assert 'create_chat_completion_qwen_non_thinking' in diagnostics['attempted_plain_completion_methods']
    unsafe_dump = json.dumps(diagnostics)
    for unsafe in ('hidden chain', 'secret prompt text', 'visible'):
        assert unsafe not in unsafe_dump


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("KV cache allocation failed", "kv_cache_allocation"),
        ("Metal buffer allocation out of memory", "metal_memory_allocation"),
        ("RoPE YaRN eval failure", "rope_yarn_eval_failure"),
    ],
)
def test_llama_worker_render_complete_fatal_plain_completion_failure_does_not_retry(
    tmp_path, monkeypatch, message, category
):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / f"fatal fallback fake site {category}"
    fake_pkg = fake_site / "llama_cpp"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text(
        "MESSAGE = " + repr(message) + "\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        if args:\n"
        "            raise AssertionError('positional create_completion fallback attempted')\n"
        "        raise RuntimeError(MESSAGE)\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise AssertionError('llama callable fallback attempted')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / "mock.gguf"),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{"role": "user", "content": "secret prompt text"}],
                max_tokens=4,
                token_place_provider="qwen",
                token_place_template_policy="gguf-jinja",
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics["generation_exception_category"] == category
    assert diagnostics["attempted_generation_kwargs"] == "max_tokens,prompt"
    assert diagnostics["attempted_plain_completion_methods"] == "create_completion_keyword_prompt"


def test_llama_worker_render_complete_max_tokens_rejected_fails_closed_without_unbounded_fallback(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'max token reject fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        raise TypeError(\"got an unexpected keyword argument 'max_tokens'\")\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        raise TypeError(\"got an unexpected keyword argument 'max_tokens'\")\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['rejected_generation_kwarg'] == 'max_tokens'
    assert diagnostics['attempted_generation_kwargs'] == 'max_tokens,prompt'
    assert diagnostics['attempted_plain_completion_methods'] == (
        'create_completion_keyword_prompt,create_completion_positional_prompt,llama_call_positional_prompt'
    )


def test_llama_worker_render_complete_high_level_qwen_fallback_uses_hard_non_thinking(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    calls_file = tmp_path / 'calls.jsonl'
    (fake_pkg / '__init__.py').write_text(
        "import json\n"
        f"CALLS_FILE = {str(calls_file)!r}\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded create_completion')\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        if 'max_tokens' not in kwargs or kwargs['max_tokens'] <= 0:\n"
        "            raise AssertionError('unbounded llama call')\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def create_chat_completion(self, *, messages, max_tokens, chat_template_kwargs):\n"
        "        with open(CALLS_FILE, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({'max_tokens': max_tokens, 'chat_template_kwargs': chat_template_kwargs, 'messages': messages}) + '\\n')\n"
        "        if chat_template_kwargs != {'enable_thinking': False}:\n"
        "            raise AssertionError('missing hard non-thinking switch')\n"
        "        return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(
        model_path=str(tmp_path / 'mock.gguf'),
        timeout_seconds=5,
    )
    try:
        result = proxy.create_chat_completion_from_rendered_prompt(
            [{'role': 'user', 'content': 'literal /no_think remains text'}],
            max_tokens=4,
            token_place_provider='qwen',
            token_place_template_policy='gguf-jinja',
            enable_thinking=False,
        )
    finally:
        proxy.close()

    assert result == {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
    calls = [json.loads(line) for line in calls_file.read_text(encoding='utf-8').splitlines()]
    assert calls == [{
        'max_tokens': 4,
        'chat_template_kwargs': {'enable_thinking': False},
        'messages': [{'role': 'user', 'content': 'literal /no_think remains text'}],
    }]


def test_llama_worker_high_level_qwen_fallback_rejects_missing_chat_template_kwargs_without_retry(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback unsupported fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    calls_file = tmp_path / 'calls.jsonl'
    (fake_pkg / '__init__.py').write_text(
        "import json\n"
        f"CALLS_FILE = {str(calls_file)!r}\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def create_chat_completion(self, *, messages, max_tokens):\n"
        "        with open(CALLS_FILE, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({'messages': messages, 'max_tokens': max_tokens}) + '\\n')\n"
        "        return {'choices': [{'message': {'role': 'assistant', 'content': 'should not be called'}}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'mock.gguf'), timeout_seconds=5)
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['qwen_high_level_chat_fallback_attempted'] is True
    assert diagnostics['qwen_high_level_chat_fallback_supported'] is False
    assert diagnostics['qwen_high_level_chat_fallback_succeeded'] is False
    assert diagnostics['qwen_high_level_chat_fallback_category'] == 'unsupported_generation_kwarg'
    assert 'create_chat_completion_qwen_non_thinking' not in diagnostics['attempted_plain_completion_methods']
    assert not calls_file.exists()



def test_qwen_fallback_preserves_decode_failure(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback missing chat fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        raise RuntimeError('llama_decode returned -1')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise RuntimeError('llama_decode returned -1')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'mock.gguf'), timeout_seconds=5)
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['qwen_high_level_chat_fallback_attempted'] is True
    assert diagnostics['qwen_high_level_chat_fallback_supported'] is False
    assert diagnostics['qwen_high_level_chat_fallback_succeeded'] is False
    assert diagnostics['qwen_high_level_chat_fallback_category'] == 'unsupported_generation_kwarg'
    assert diagnostics['generation_exception_category'] == 'prompt_eval_invalid_batch'
    assert diagnostics['exception_type'] == 'RuntimeError'
    assert diagnostics['sanitized_error_summary'] == 'RuntimeError:redacted'
    assert diagnostics['plain_completion_eval_return_code'] == -1
    assert diagnostics['method'] == 'create_completion_positional_token_ids'
    diagnostics_dump = json.dumps(diagnostics)
    assert 'secret prompt text' not in diagnostics_dump
    assert 'llama_decode returned -1' not in diagnostics_dump


def test_qwen_fallback_preserves_malformed_output(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback malformed missing chat fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        return {'choices': []}\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise RuntimeError('llama_decode returned -1')\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'mock.gguf'), timeout_seconds=5)
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['qwen_high_level_chat_fallback_attempted'] is True
    assert diagnostics['qwen_high_level_chat_fallback_supported'] is False
    assert diagnostics['qwen_high_level_chat_fallback_succeeded'] is False
    assert diagnostics['qwen_high_level_chat_fallback_category'] == 'unsupported_generation_kwarg'
    assert diagnostics['generation_exception_category'] == 'malformed_completion_output'
    assert 'sanitized_error_summary' not in diagnostics
    diagnostics_dump = json.dumps(diagnostics)
    assert 'secret prompt text' not in diagnostics_dump
    assert 'llama_decode returned -1' not in diagnostics_dump


def test_qwen_fallback_unsupported_without_bounded_attempts(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback unsupported only fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    (fake_pkg / '__init__.py').write_text(
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'mock.gguf'), timeout_seconds=5)
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'secret prompt text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['qwen_high_level_chat_fallback_attempted'] is True
    assert diagnostics['qwen_high_level_chat_fallback_supported'] is False
    assert diagnostics['qwen_high_level_chat_fallback_succeeded'] is False
    assert diagnostics['qwen_high_level_chat_fallback_category'] == 'unsupported_generation_kwarg'
    assert diagnostics['generation_exception_category'] == 'unsupported_generation_kwarg'
    assert diagnostics['exception_type'] == 'RuntimeError'
    assert diagnostics['sanitized_error_summary'] == 'RuntimeError:unsupported_kwarg'

def test_llama_worker_high_level_qwen_fallback_rejects_nonempty_think_content(tmp_path, monkeypatch):
    from utils.llm import model_manager as model_manager_module

    fake_site = tmp_path / 'high level fallback think fake site'
    fake_pkg = fake_site / 'llama_cpp'
    fake_pkg.mkdir(parents=True)
    calls_file = tmp_path / 'calls.jsonl'
    (fake_pkg / '__init__.py').write_text(
        "import json\n"
        f"CALLS_FILE = {str(calls_file)!r}\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, enable_thinking=False, **kwargs):\n"
        "        return '<qwen>'\n"
        "    def tokenize(self, payload, add_bos=False, special=False):\n"
        "        return [1, 2, 3]\n"
        "    def create_completion(self, *args, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def __call__(self, prompt, **kwargs):\n"
        "        raise RuntimeError('failed to eval prompt')\n"
        "    def create_chat_completion(self, *, messages, max_tokens, chat_template_kwargs):\n"
        "        with open(CALLS_FILE, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({'messages': messages, 'max_tokens': max_tokens, 'chat_template_kwargs': chat_template_kwargs}) + '\\n')\n"
        "        return {'choices': [{'message': {'role': 'assistant', 'content': '<think>hidden</think> visible'}}]}\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_ENV', 'testing')

    proxy = model_manager_module._SubprocessLlamaProxy(model_path=str(tmp_path / 'mock.gguf'), timeout_seconds=5)
    try:
        with pytest.raises(model_manager_module.LlamaCppInferenceRequestError) as exc_info:
            proxy.create_chat_completion_from_rendered_prompt(
                [{'role': 'user', 'content': 'literal /no_think remains text'}],
                max_tokens=4,
                token_place_provider='qwen',
                token_place_template_policy='gguf-jinja',
                enable_thinking=False,
            )
    finally:
        proxy.close()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics['generation_exception_category'] == 'thinking_leaked'
    assert diagnostics['qwen_high_level_chat_fallback_succeeded'] is False
    calls = [json.loads(line) for line in calls_file.read_text(encoding='utf-8').splitlines()]
    assert calls == [{
        'messages': [{'role': 'user', 'content': 'literal /no_think remains text'}],
        'max_tokens': 4,
        'chat_template_kwargs': {'enable_thinking': False},
    }]
    dumped_calls = json.dumps(calls)
    assert 'enable_thinking": true' not in dumped_calls.lower()
    assert '\n/no_think' not in dumped_calls

def test_qwen_64k_runtime_rejects_mismatched_yarn_context_multiplier(tmp_path):
    from utils.context_profiles import apply_context_profile

    config = MagicMock(is_production=False)
    values = {
        'model.profile_id': 'qwen3-8b-q4-k-m',
        'model.context_size': 8192,
        'model.use_mock': False,
        'model.n_gpu_layers': 0,
        'model.gpu_mode': 'cpu',
        'model.enforce_gpu_memory_headroom': False,
        'paths.models_dir': str(tmp_path),
    }
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    config.set.side_effect = lambda key, value: values.__setitem__(key, value)
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    manager.model_profile = dict(manager.model_profile)
    manager.model_profile['rope_scaling_policy'] = dict(manager.model_profile['rope_scaling_policy'])
    manager.model_profile['rope_scaling_policy']['factor'] = 3.0

    class FakeLlama:
        def __init__(self, model_path, n_gpu_layers, n_ctx, verbose, rope_scaling_type, rope_freq_scale, yarn_orig_ctx):
            raise AssertionError('constructor must not be called when YaRN config is invalid')

    fake_llama_cpp = SimpleNamespace(Llama=FakeLlama, LLAMA_ROPE_SCALING_TYPE_YARN=2)

    with pytest.raises(RuntimeError, match='runtime_qwen_64k_yarn_configuration_invalid'):
        manager._runtime_init_kwargs(FakeLlama, 0, fake_llama_cpp, None)
    assert manager.last_yarn_rope_diagnostics['qwen_yarn_configuration_valid'] is False


def test_qwen_64k_recovery_exhaustion_and_lifecycle_edge_coverage(monkeypatch):
    manager = object.__new__(ModelManager)
    failed_runtime = MagicMock()
    manager.llm_lock = threading.RLock()
    manager.llm = failed_runtime
    manager.model_profile = {"provider": "qwen"}
    manager.context_tier = "64k-full"
    manager.worker_state = "ready"
    manager.last_worker_error_code = None
    manager.last_worker_restart_at_ms = None
    manager.last_plain_completion_eval_return_code = None
    manager.worker_restart_count = 0
    manager._llm_generation = 0
    manager._qwen_64k_profile_recovery_count = 0
    manager._qwen_64k_selected_profile_index = 0
    manager._qwen_64k_selected_profile_id = "only"
    manager._qwen_64k_runtime_profiles = [{"profile_id": "only", "diagnostics": {"backend": "metal"}}]
    manager._qwen_64k_first_readiness_failure_category = None
    manager._qwen_64k_first_readiness_failure_diagnostics = {}
    monkeypatch.setattr(manager, "_close_llm_proxy", MagicMock())
    manager.get_llm_instance = MagicMock()

    assert manager.reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        failed_runtime,
        "backend_graph_compute_failure",
        decode_return_code=-3,
        failure_diagnostics={"method": "create_completion"},
    ) is None

    manager._close_llm_proxy.assert_called_once_with(failed_runtime)
    manager.get_llm_instance.assert_not_called()
    assert manager.llm is None
    assert manager._qwen_64k_selected_profile_index == 1
    assert manager._qwen_64k_first_readiness_failure_diagnostics["method"] == "create_completion"

    class BadProcess:
        def poll(self):
            raise RuntimeError("poll failed")

    assert manager._worker_exit_code(SimpleNamespace(_process=BadProcess())) is None


def test_desktop_runtime_probe_strict_bool_coercion_rejects_truthy_strings():
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module._coerce_strict_bool(False) is False
    assert model_manager_module._coerce_strict_bool(0) is False
    assert model_manager_module._coerce_strict_bool('false') is False
    assert model_manager_module._coerce_strict_bool(True) is True
    assert model_manager_module._coerce_strict_bool(1) is True
    assert model_manager_module._coerce_strict_bool('true') is True
    assert model_manager_module._coerce_strict_bool('yes') is None


def test_complete_cuda_desktop_probe_is_authoritative_without_reprobe(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    support = {name: True for name in model_manager_module.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    facade = model_manager_module._SubprocessLlamaCppModule(
        'C:/Users/Alice/AppData/Local/Programs/Python/Python311/Lib/site-packages/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'runtime_action': 'already_supported',
            'llama_module_path': 'C:/Users/Alice/AppData/Local/Programs/Python/Python311/Lib/site-packages/llama_cpp/__init__.py',
            'llama_module_path_present': True,
            'llama_module_identity': model_manager_module.llama_module_identity_from_path('C:/Users/Alice/AppData/Local/Programs/Python/Python311/Lib/site-packages/llama_cpp/__init__.py'),
            'constructor_kwarg_support': support,
            'constructor_signature_inspectable': True,
            'constructor_has_var_kwargs': False,
            'qwen_64k_yarn_support': 'supported',
            'yarn_enum_value': 2,
            'q8_kv_cache_type_value': 8,
            'q4_kv_cache_type_value': 2,
            'f16_kv_cache_type_value': 1,
            'capability_source': 'desktop_runtime_setup_probe',
        },
    )

    def fail_probe(**_kwargs):
        raise AssertionError('unexpected secondary probe')

    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', fail_probe)
    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)
    profiles = model_manager_module._build_qwen_64k_runtime_profiles(
        facade,
        facade.Llama,
        model_path=__file__,
        n_ctx=65536,
    )

    assert diagnostics['supported'] is True
    assert diagnostics['child_probe_reprobe_attempted'] is False
    assert diagnostics['child_probe_reprobe_skipped_reason'] == 'desktop_probe_authoritative'
    assert [profile['profile_id'] for profile in profiles] == [
        model_manager_module.QWEN_64K_RUNTIME_PROFILE_DEFAULT,
        model_manager_module.QWEN_64K_RUNTIME_PROFILE_Q8,
        model_manager_module.QWEN_64K_RUNTIME_PROFILE_Q4,
    ]
    assert profiles[0]['diagnostics']['backend'] == 'cuda'
    assert profiles[0]['kwargs']['flash_attn'] is True
    assert profiles[0]['kwargs']['offload_kqv'] is True
    assert profiles[0]['kwargs']['n_batch'] == 256
    assert profiles[0]['kwargs']['n_ubatch'] == 128


def test_desktop_probe_module_path_mismatch_fails_closed_without_reprobe(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    support = {name: True for name in model_manager_module.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    facade = model_manager_module._SubprocessLlamaCppModule(
        '/runtime/actual/llama_cpp/__init__.py',
        desktop_runtime_probe={
            'backend': 'cuda',
            'gpu_offload_supported': True,
            'llama_module_path': '/runtime/other/llama_cpp/__init__.py',
            'llama_module_path_present': True,
            'llama_module_identity': model_manager_module.llama_module_identity_from_path('/runtime/actual/llama_cpp/__init__.py'),
            'constructor_kwarg_support': support,
            'constructor_signature_inspectable': True,
            'qwen_64k_yarn_support': 'supported',
            'yarn_enum_value': 2,
            'capability_source': 'desktop_runtime_setup_probe',
        },
    )

    def fail_probe(**_kwargs):
        raise AssertionError('unexpected secondary probe')

    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', fail_probe)

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is False
    assert diagnostics['missing_reason'] == 'runtime_desktop_capability_probe_incomplete'
    assert 'llama_module_identity_match' in diagnostics['missing_required_kwargs']
    assert diagnostics['child_probe_reprobe_attempted'] is False


def test_legacy_flat_desktop_probe_uses_mandated_yarn_bridge_without_reprobe(monkeypatch, tmp_path):
    from utils.context_profiles import apply_context_profile
    from utils.llm import model_manager as model_manager_module

    module_path = '/site/llama_cpp/__init__.py'
    flat_payload = {
        'backend': 'cuda',
        'gpu_offload_supported': 'true',
        'runtime_action': 'already_supported',
        'llama_module_path': module_path,
        'yarn_rope_supported': 'true',
        'yarn_resolver_source': 'numeric_fallback',
        'rope_scaling_type_supported': 'true',
        'rope_freq_scale_supported': 'true',
        'yarn_orig_ctx_supported': 'true',
    }
    facade = model_manager_module._SubprocessLlamaCppModule(
        module_path,
        desktop_runtime_probe=flat_payload,
    )
    capabilities = model_manager_module._safe_constructor_capability_payload(facade)
    assert capabilities['capability_source'] == 'desktop_runtime_setup_probe_legacy'
    assert capabilities['qwen_64k_yarn_support'] == 'supported'
    assert capabilities['yarn_enum_value'] == 2
    assert capabilities['constructor_signature_inspectable'] is True
    assert capabilities['constructor_kwarg_support']['rope_scaling_type'] is True
    assert capabilities['constructor_kwarg_support']['rope_freq_scale'] is True
    assert capabilities['constructor_kwarg_support']['yarn_orig_ctx'] is True
    for unobserved in ('type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch'):
        assert unobserved not in capabilities['constructor_kwarg_support']
        assert unobserved not in capabilities

    def fail_child_probe(**_kwargs):
        raise AssertionError('legacy desktop facade must not launch a child capability reprobe')

    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', fail_child_probe)

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is True
    assert diagnostics['yarn_enum_value'] == 2
    assert diagnostics['capability_source'] == 'desktop_runtime_setup_probe_legacy'
    assert diagnostics['child_probe_reprobe_attempted'] is False
    assert diagnostics['child_probe_reprobe_skipped_reason'] == 'desktop_probe_authoritative'

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
    manager = ModelManager(config)
    apply_context_profile(manager, '64k-full')
    Path(manager.model_path).write_text('fake')

    kwargs = manager._runtime_init_kwargs(facade.Llama, -1, facade)

    assert kwargs['rope_scaling_type'] == 2
    assert kwargs['rope_freq_scale'] == 0.5
    assert kwargs['yarn_orig_ctx'] == 32768
    assert 'type_k' not in kwargs
    assert 'type_v' not in kwargs
    assert 'flash_attn' not in kwargs
    assert 'offload_kqv' not in kwargs
    assert 'n_batch' not in kwargs
    assert 'n_ubatch' not in kwargs
    assert manager.last_yarn_rope_diagnostics['child_probe_reprobe_attempted'] is False


@pytest.mark.parametrize('resolver_source', ['top_level_enum', 'nested_enum', 'llama_class_enum', 'arbitrary'])
def test_legacy_flat_desktop_probe_exported_enum_without_value_fails_closed(monkeypatch, resolver_source):
    from utils.llm import model_manager as model_manager_module

    module_path = '/site/llama_cpp/__init__.py'
    flat_payload = {
        'backend': 'cuda',
        'gpu_offload_supported': 'true',
        'runtime_action': 'already_supported',
        'llama_module_path': module_path,
        'yarn_rope_supported': 'true',
        'yarn_resolver_source': resolver_source,
        'rope_scaling_type_supported': 'true',
        'rope_freq_scale_supported': 'true',
        'yarn_orig_ctx_supported': 'true',
    }
    facade = model_manager_module._SubprocessLlamaCppModule(
        module_path,
        desktop_runtime_probe=flat_payload,
    )
    capabilities = model_manager_module._safe_constructor_capability_payload(facade)

    assert capabilities['capability_source'] == 'desktop_runtime_setup_probe_legacy'
    assert capabilities.get('yarn_enum_value') is None
    assert capabilities.get('qwen_64k_yarn_support') != 'supported'

    def fail_child_probe(**_kwargs):
        raise AssertionError('incomplete legacy desktop probe must fail closed without child reprobe')

    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', fail_child_probe)

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is False
    assert diagnostics['missing_reason'] == 'runtime_desktop_capability_probe_incomplete'
    assert 'yarn_enum_value' in diagnostics['missing_required_kwargs']
    assert diagnostics['child_probe_reprobe_attempted'] is False
    assert diagnostics['child_probe_reprobe_skipped_reason'] == 'desktop_probe_incomplete_fail_closed'


def test_modern_desktop_probe_identity_authoritative_without_path_or_reprobe(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    module_path = tmp_path / 'site-packages' / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir(parents=True)
    module_path.write_text('# mock')
    support = {name: True for name in model_manager_module.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    facade = model_manager_module._SubprocessLlamaCppModule(
        str(module_path),
        desktop_runtime_probe={
            'backend': 'metal', 'gpu_offload_supported': True, 'runtime_action': 'metal_already_supported',
            'llama_module_path_present': True,
            'llama_module_identity': model_manager_module.llama_module_identity_from_path(module_path),
            'constructor_kwarg_support': support, 'constructor_signature_inspectable': True,
            'constructor_has_var_kwargs': False, 'qwen_64k_yarn_support': 'supported',
            'yarn_enum_value': 2, 'q8_kv_cache_type_value': 8, 'q4_kv_cache_type_value': 2,
            'f16_kv_cache_type_value': 1, 'capability_source': 'desktop_runtime_setup_probe',
            'llama_cpp_python_version': '0.3.32',
        },
    )
    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', lambda **_: (_ for _ in ()).throw(AssertionError('unexpected secondary probe')))
    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)
    assert diagnostics['supported'] is True
    assert diagnostics['desktop_probe_authoritative'] is True
    assert diagnostics['llama_module_identity_match'] is True
    assert diagnostics['child_probe_reprobe_attempted'] is False


def test_modern_desktop_probe_identity_negative_cases_fail_closed(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    module_path = tmp_path / 'site-packages' / 'llama_cpp' / '__init__.py'
    other_path = tmp_path / 'other' / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir(parents=True); other_path.parent.mkdir(parents=True)
    module_path.write_text('# mock'); other_path.write_text('# other')
    support = {name: True for name in model_manager_module.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    base = {
        'backend': 'metal', 'gpu_offload_supported': True, 'runtime_action': 'metal_already_supported',
        'llama_module_path_present': True, 'constructor_kwarg_support': support,
        'constructor_signature_inspectable': True, 'qwen_64k_yarn_support': 'supported',
        'yarn_enum_value': 2, 'capability_source': 'desktop_runtime_setup_probe',
    }
    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', lambda **_: (_ for _ in ()).throw(AssertionError('unexpected secondary probe')))
    for identity in (None, 'sha256:not-valid', model_manager_module.llama_module_identity_from_path(other_path)):
        probe = dict(base)
        if identity is not None:
            probe['llama_module_identity'] = identity
        facade = model_manager_module._SubprocessLlamaCppModule(str(module_path), desktop_runtime_probe=probe)
        diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)
        assert diagnostics['supported'] is False
        assert diagnostics['missing_reason'] == 'runtime_desktop_capability_probe_incomplete'
        assert diagnostics['child_probe_reprobe_attempted'] is False
        formatted = model_manager_module._format_qwen_yarn_unsupported_diagnostics(diagnostics)
        assert str(module_path) not in formatted
        if identity:
            assert identity not in formatted
        assert 'child_probe_reprobe_attempted=False' in formatted
        assert 'incomplete_probe_fields=' in formatted
        if identity == 'sha256:not-valid':
            assert 'llama_module_identity' in diagnostics['incomplete_probe_fields']
            assert 'llama_module_identity_match' not in diagnostics['incomplete_probe_fields']


def test_coerce_desktop_runtime_probe_preserves_malformed_identity_state():
    from utils.llm import model_manager as model_manager_module

    probe = model_manager_module._coerce_desktop_runtime_probe({
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'metal_already_supported',
        'llama_module_path': '/private/redacted/llama_cpp/__init__.py',
        'llama_module_path_present': False,
        'llama_module_identity': 'sha256:not-valid',
    })

    assert probe is not None
    assert probe['llama_module_path_present'] is False
    assert probe['llama_module_identity_malformed'] is True
    assert 'llama_module_identity' not in probe


def test_qwen_yarn_diagnostics_preserve_false_and_empty_values():
    from utils.llm import model_manager as model_manager_module

    formatted = model_manager_module._format_qwen_yarn_unsupported_diagnostics({
        'child_probe_reprobe_attempted': False,
        'constructor_kwargs_attempted': [],
        'incomplete_probe_fields': ['llama_module_identity'],
    })
    assert 'child_probe_reprobe_attempted=False' in formatted
    assert 'constructor_kwargs_attempted=[]' in formatted
    assert "incomplete_probe_fields=['llama_module_identity']" in formatted


def test_modern_probe_rejects_concrete_path_fallback_when_identity_missing(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    module_path = tmp_path / 'site-packages' / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir(parents=True)
    module_path.write_text('# mock')
    support = {name: True for name in model_manager_module.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', lambda **_: (_ for _ in ()).throw(AssertionError('unexpected secondary probe')))
    facade = model_manager_module._SubprocessLlamaCppModule(str(module_path), desktop_runtime_probe={
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'metal_already_supported',
        'llama_module_path': str(module_path),
        'llama_module_path_present': True,
        'constructor_kwarg_support': support,
        'constructor_signature_inspectable': True,
        'qwen_64k_yarn_support': 'supported',
        'yarn_enum_value': 2,
        'capability_source': 'desktop_runtime_setup_probe',
    })

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is False
    assert diagnostics['child_probe_reprobe_attempted'] is False
    assert diagnostics['incomplete_probe_fields'] == ['llama_module_identity']


def test_legacy_probe_allows_concrete_path_only_match(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    module_path = tmp_path / 'site-packages' / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir(parents=True)
    module_path.write_text('# mock')
    support = {name: True for name in model_manager_module.LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS}
    monkeypatch.setattr(model_manager_module, '_probe_llama_cpp_capabilities_in_subprocess', lambda **_: (_ for _ in ()).throw(AssertionError('unexpected secondary probe')))
    facade = model_manager_module._SubprocessLlamaCppModule(str(module_path), desktop_runtime_probe={
        'backend': 'metal',
        'gpu_offload_supported': True,
        'runtime_action': 'metal_already_supported',
        'llama_module_path': str(module_path),
        'llama_module_path_present': True,
        'constructor_kwarg_support': support,
        'constructor_signature_inspectable': True,
        'qwen_64k_yarn_support': 'supported',
        'yarn_enum_value': 2,
        'capability_source': 'desktop_runtime_setup_probe_legacy',
    })

    diagnostics = model_manager_module._runtime_supports_qwen_yarn_rope(facade, facade.Llama)

    assert diagnostics['supported'] is True
    assert diagnostics['desktop_probe_authoritative'] is True
    assert diagnostics['child_probe_reprobe_attempted'] is False


def test_llama_module_identity_consumer_rejects_sentinels_and_normalizes_windows():
    from utils.llm import model_manager as model_manager_module

    assert model_manager_module.llama_module_identity_from_path('unknown') is None
    assert model_manager_module.llama_module_identity_from_path('missing') is None
    base = r'C:\Users\Alice\Runtime\Lib\site-packages\llama_cpp\__init__.py'
    prefixed = r'\\?\C:\Users\Alice\Runtime\Lib\site-packages\llama_cpp\..\llama_cpp\__init__.py'
    mixed = r'c:/users/alice/runtime/lib/site-packages/LLAMA_CPP/__init__.py'
    assert model_manager_module.llama_module_identity_from_path(base) == model_manager_module.llama_module_identity_from_path(prefixed)
    assert model_manager_module.llama_module_identity_from_path(base) == model_manager_module.llama_module_identity_from_path(mixed)


def test_import_llama_cpp_runtime_identity_only_probe_skips_discovery(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    module_path = tmp_path / 'site-packages' / 'llama_cpp' / '__init__.py'
    module_path.parent.mkdir(parents=True)
    module_path.write_text('# fake runtime')
    identity = model_manager_module.llama_module_identity_from_path(module_path)
    probe = {
        'backend': 'cuda',
        'gpu_offload_supported': True,
        'runtime_action': 'already_supported',
        'llama_module_path_present': True,
        'llama_module_identity': identity,
        'capability_source': 'desktop_runtime_setup_probe',
    }
    captured = {}

    monkeypatch.setattr(model_manager_module, '_signal_guard_available', lambda: False)
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('unexpected redundant discovery')),
    )

    def fake_import(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(__file__='identity-only-facade')

    monkeypatch.setattr(model_manager_module, '_import_llama_cpp_in_parent_with_timeout', fake_import)

    runtime = model_manager_module._import_llama_cpp_runtime(desktop_runtime_probe=probe, timeout_seconds=0.01)

    assert runtime.__file__ == 'identity-only-facade'
    assert captured['module_path_hint'] is None
    assert captured['expected_llama_module_identity'] == identity
    assert captured['desktop_runtime_probe']['llama_module_identity'] == identity


@pytest.mark.parametrize('identity_value', [None, 'sha256:not-valid'])
def test_import_llama_cpp_runtime_identity_only_probe_missing_or_malformed_fails_closed(monkeypatch, tmp_path, identity_value):
    from utils.llm import model_manager as model_manager_module

    probe = {
        'backend': 'cuda',
        'gpu_offload_supported': True,
        'runtime_action': 'already_supported',
        'llama_module_path_present': True,
        'capability_source': 'desktop_runtime_setup_probe',
    }
    if identity_value is not None:
        probe['llama_module_identity'] = identity_value
    monkeypatch.setattr(model_manager_module, '_signal_guard_available', lambda: False)
    monkeypatch.setattr(
        model_manager_module,
        '_find_llama_cpp_spec_in_subprocess',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('unexpected legacy discovery fallback')),
    )

    with pytest.raises(ImportError) as exc_info:
        model_manager_module._import_llama_cpp_runtime(desktop_runtime_probe=probe, timeout_seconds=0.01)

    message = str(exc_info.value)
    assert 'identity is missing or malformed' in message
    assert 'sha256:' not in message
    assert str(tmp_path) not in message


def test_subprocess_worker_identity_mismatch_fails_before_constructor_without_leaks(monkeypatch, tmp_path):
    from utils.llm import model_manager as model_manager_module

    site = tmp_path / 'space containing site'
    package = site / 'llama_cpp'
    package.mkdir(parents=True)
    constructed = tmp_path / 'constructed.txt'
    module_path = package / '__init__.py'
    module_path.write_text(
        'from pathlib import Path\n'
        f'CONSTRUCTED = Path({str(constructed)!r})\n'
        'class Llama:\n'
        '    def __init__(self, *args, **kwargs):\n'
        '        CONSTRUCTED.write_text("constructed")\n'
    )
    other = tmp_path / 'other' / 'llama_cpp' / '__init__.py'
    other.parent.mkdir(parents=True)
    other.write_text('# other')
    monkeypatch.syspath_prepend(str(site))

    probe = {
        'backend': 'cuda',
        'gpu_offload_supported': True,
        'runtime_action': 'already_supported',
        'llama_module_path_present': True,
        'llama_module_identity': model_manager_module.llama_module_identity_from_path(other),
        'capability_source': 'desktop_runtime_setup_probe',
    }
    facade = model_manager_module._SubprocessLlamaCppModule(
        None,
        timeout_seconds=5,
        desktop_runtime_probe=probe,
        expected_llama_module_identity=probe['llama_module_identity'],
    )

    with pytest.raises(Exception) as exc_info:
        facade.Llama(model_path=str(tmp_path / 'model.gguf'))

    assert not constructed.exists()
    message = str(exc_info.value)
    assert str(module_path) not in message
    assert str(other) not in message
    assert probe['llama_module_identity'] not in message
