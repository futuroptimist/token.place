"""
Unit tests for the model manager module.
"""
import os
import pytest
import shutil
from unittest.mock import MagicMock, patch
import json
import sys
import tempfile
from pathlib import Path

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the module to test
from utils.llm.model_manager import ModelManager

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
    @patch('llama_cpp.Llama', create=True)
    def test_get_llm_instance_falls_back_to_cpu_on_low_gpu_memory(
        self,
        mock_llama,
        mock_can_allocate,
        mock_exists,
        _mock_getsize,
        model_manager,
    ):
        """When GPU headroom is insufficient the model should load on CPU."""

        instance = MagicMock()
        mock_llama.return_value = instance

        result = model_manager.get_llm_instance()

        assert result is instance
        mock_llama.assert_called_once()
        kwargs = mock_llama.call_args.kwargs
        assert kwargs['n_gpu_layers'] == 0
        mock_can_allocate.assert_called_once()

    @patch('utils.llm.model_manager.os.path.getsize', return_value=4_000_000_000)
    @patch('utils.llm.model_manager.os.path.exists', return_value=True)
    @patch('utils.llm.model_manager.resource_monitor.can_allocate_gpu_memory', return_value=True)
    @patch('llama_cpp.Llama', create=True)
    def test_get_llm_instance_uses_gpu_when_headroom_available(
        self,
        mock_llama,
        mock_can_allocate,
        mock_exists,
        _mock_getsize,
        model_manager,
    ):
        """When memory is available the model continues to use the GPU."""

        instance = MagicMock()
        mock_llama.return_value = instance

        result = model_manager.get_llm_instance()

        assert result is instance
        kwargs = mock_llama.call_args.kwargs
        assert kwargs['n_gpu_layers'] == -1
        mock_can_allocate.assert_called_once_with(4_000_000_000, headroom_percent=0.1)

    def test_get_llm_instance_skips_headroom_when_size_unknown(self, model_manager):
        """If the model size cannot be determined we skip the headroom check."""

        instance = MagicMock()

        with patch('llama_cpp.Llama', return_value=instance, create=True) as mock_llama, \
             patch('utils.llm.model_manager.resource_monitor.can_allocate_gpu_memory') as mock_can_allocate, \
             patch('utils.llm.model_manager.os.path.exists', return_value=True), \
             patch('utils.llm.model_manager.os.path.getsize', side_effect=OSError('stat failed')):

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

    def test_get_llm_instance_real_mode(self, model_manager):
        """Test get_llm_instance in real mode when the model file exists."""
        # Create a mock for Llama
        mock_llama = MagicMock()

        # Patch everything needed
        with patch('os.path.exists', return_value=True), \
             patch('llama_cpp.Llama', return_value=mock_llama, create=True):

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

    @patch('llama_cpp.Llama', side_effect=Exception('fail'), create=True)
    @patch('os.path.exists', return_value=True)
    def test_get_llm_instance_init_failure(self, mock_exists, mock_llama, model_manager):
        """Return None if Llama initialization raises an exception."""
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
