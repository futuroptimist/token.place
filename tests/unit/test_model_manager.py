"""
Unit tests for the model manager module.
"""
import logging
import os
import queue
import threading
import pytest
import shutil
import time
from unittest.mock import MagicMock, patch
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
        tokens = llm.tokenize(rendered.encode('utf-8'), add_bos=False)

        assert isinstance(rendered, str)
        assert '<|user|>' in rendered
        assert '<|assistant|>' in rendered
        assert isinstance(tokens, list)
        assert len(tokens) > 0

    def test_get_model_artifact_metadata(self, model_manager):
        """Test runtime model metadata includes expected keys and file state."""
        metadata = model_manager.get_model_artifact_metadata()

        assert metadata['canonical_family_url'] == 'https://huggingface.co/meta-llama/Meta-Llama-3-8B'
        assert metadata['filename'] == 'test_model.gguf'
        assert metadata['url'] == 'https://example.com/model.gguf'
        assert metadata['models_dir'] == self._temp_dir
        assert metadata['resolved_model_path'] == os.path.join(self._temp_dir, 'test_model.gguf')
        assert metadata['exists'] is True
        assert metadata['size_bytes'] == len(b'fake model data')

        os.remove(metadata['resolved_model_path'])
        missing_metadata = model_manager.get_model_artifact_metadata()
        assert missing_metadata['exists'] is False
        assert missing_metadata['size_bytes'] is None

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
        """Test download_model_if_needed when model already exists."""
        # Setup mocks
        mock_exists.return_value = True  # Model already exists

        # Call the method
        result = model_manager.download_model_if_needed()

        # Check the result
        assert result is True

        # Verify mock calls
        mock_download.assert_not_called()

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
        assert isinstance(llm, MagicMock)

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
        assert any(message.startswith('llama_cpp runtime located module_path=') for message in messages)
        assert 'Selecting compute plan for model initialization...' in messages
        assert any(
            message.startswith('Selected compute plan for model initialization ')
            for message in messages
        )
        assert any(message.startswith('About to instantiate Llama model from ') for message in messages)
        assert any(message.startswith('Llama init started for ') for message in messages)
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
        assert os.path.exists(file_path)
        assert os.path.getsize(file_path) != 1048576

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

        assert isinstance(llm, MagicMock)
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
        assert 'llama_module_path=' in summary
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
        "print('stdout clue before exit')\n"
        "print('stderr clue before exit', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(fake_site))
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '\\\\?\\C:\\Users\\danie\\AppData\\Local\\token.place desktop\\_up_\\_up_')

    with pytest.raises(RuntimeError) as exc_info:
        model_manager_module._SubprocessLlamaProxy(model_path='model.gguf', timeout_seconds=5)

    message = str(exc_info.value)
    assert 'llama_cpp_import subprocess exited before JSON handshake' in message
    assert 'llama_cpp_import subprocess ended' not in message
    assert 'exit_code=7' in message
    assert 'stdout clue before exit' in message
    assert 'stderr clue before exit' in message
    assert 'import_root=C:' in message
    assert 'token.place desktop' in message


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
            self._token_place_stderr_tail = ['stderr clue\n']

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
    assert 'llama_cpp_import subprocess ended' not in message
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
    assert created[0].stdin.writes[1]['kwargs']['stream'] is True


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
    assert 'init failed clearly' in message
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

    assert str(exc_info.value) == 'init failed'
    assert not isinstance(exc_info.value, model_manager_module.LlamaCppInferenceRequestError)


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
