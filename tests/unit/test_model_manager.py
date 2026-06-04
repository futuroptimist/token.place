"""
Unit tests for the model manager module.
"""
import os
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
    assert sanitized_path[0] == str(site_packages)


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
    assert calls == ['watchdog', 'llama_cpp']


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
    monkeypatch.setattr(model_manager_module.importlib, 'import_module', lambda _name: fake_runtime)
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
    monkeypatch.setattr(
        model_manager_module,
        '_run_llama_cpp_import_watchdog',
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


def test_import_llama_cpp_runtime_no_signal_fails_closed_after_child_watchdog(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    sys.modules.pop('llama_cpp', None)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_ALLOW_UNBOUNDED_LLAMA_CPP_PARENT_IMPORT', raising=False)
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
        model_manager_module,
        '_run_llama_cpp_import_watchdog',
        lambda **_kwargs: {'module_path': '/site-packages/llama_cpp/__init__.py'},
    )
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('would hang if parent import started')),
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
        model_manager_module._import_llama_cpp_runtime(timeout_seconds=0.01)

    assert exc_info.value.stage == 'llama_cpp_import'
    assert model_manager_module._format_runtime_stage_timeout(exc_info.value) == (
        'llama_cpp_import_timeout after 0.01s'
    )
    assert 'llama_cpp' not in sys.modules


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


def test_parent_import_guard_no_signal_fails_closed_by_default(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    sys.modules.pop('llama_cpp', None)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_ALLOW_UNBOUNDED_LLAMA_CPP_PARENT_IMPORT', raising=False)
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(AssertionError('must not import unbounded')),
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
        model_manager_module._import_llama_cpp_in_parent_with_timeout(timeout_seconds=0.01)

    assert exc_info.value.stage == 'llama_cpp_import'
    assert exc_info.value.timeout_seconds == 0.01


def test_parent_import_guard_no_signal_imports_with_explicit_unbounded_opt_in(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    sys.modules.pop('llama_cpp', None)
    fake_runtime = SimpleNamespace(__file__='/site-packages/llama_cpp/__init__.py')
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.setenv('TOKEN_PLACE_ALLOW_UNBOUNDED_LLAMA_CPP_PARENT_IMPORT', '1')
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda name: fake_runtime if name == 'llama_cpp' else None,
    )

    assert model_manager_module._import_llama_cpp_in_parent_with_timeout(timeout_seconds=0.01) is fake_runtime


def test_parent_import_guard_no_signal_wraps_timeout_error_with_unbounded_opt_in(monkeypatch):
    from utils.llm import model_manager as model_manager_module

    sys.modules.pop('llama_cpp', None)
    monkeypatch.delattr(model_manager_module.signal, 'SIGALRM', raising=False)
    monkeypatch.setenv('TOKEN_PLACE_ALLOW_UNBOUNDED_LLAMA_CPP_PARENT_IMPORT', '1')
    monkeypatch.setattr(
        model_manager_module.importlib,
        'import_module',
        lambda _name: (_ for _ in ()).throw(TimeoutError('native import timeout')),
    )

    with pytest.raises(model_manager_module.LlamaCppRuntimeStageTimeout) as exc_info:
        model_manager_module._import_llama_cpp_in_parent_with_timeout(timeout_seconds=0.01)

    assert exc_info.value.stage == 'llama_cpp_import'
    assert exc_info.value.timeout_seconds == 0.01


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
