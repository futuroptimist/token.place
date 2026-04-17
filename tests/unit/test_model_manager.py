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

        with patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=True):
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

        with patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=True):
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
             patch('utils.llm.model_manager.os.path.getsize', side_effect=OSError('stat failed')), \
             patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=True):

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

        with patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=False):
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

        with patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=True):
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
             patch.dict(sys.modules, {'llama_cpp': fake_llama}):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'cuda'

    def test_platform_gpu_backend_detects_nested_cuda_marker_variants(self):
        """Backend detection should accept CUDA markers on nested llama_cpp module."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=False,
            llama_cpp=SimpleNamespace(
                GGML_CUDA=True,
                GGML_USE_CUBLAS=False,
            ),
            llama_supports_gpu_offload=lambda: False,
        )

        with patch('utils.llm.model_manager.sys.platform', 'win32'), \
             patch.dict(sys.modules, {'llama_cpp': fake_llama}):
            backend = ModelManager._platform_gpu_backend()

        assert backend == 'cuda'

    def test_llama_gpu_offload_available_returns_false_on_runtime_error(self):
        """GPU support probe should fail closed if llama_cpp probe raises."""

        def _raise_runtime_error():
            raise RuntimeError("probe failed")

        fake_llama = SimpleNamespace(llama_supports_gpu_offload=_raise_runtime_error)
        with patch.dict(sys.modules, {'llama_cpp': fake_llama}):
            assert ModelManager._llama_gpu_offload_available() is False

    def test_get_llm_instance_mock_mode_refreshes_compute_diagnostics(self, model_manager):
        """Mock mode should persist diagnostics from resolved compute plan."""
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

        with patch.object(model_manager, '_resolve_compute_plan', return_value=expected_plan):
            llm = model_manager.get_llm_instance()

        assert isinstance(llm, MagicMock)
        assert model_manager.last_compute_diagnostics == expected_plan

    def test_platform_gpu_backend_linux_detects_metal_marker(self):
        """Linux backend detection should return Metal when CUDA is unavailable."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=True,
            llama_supports_gpu_offload=lambda: False,
        )

        with patch('utils.llm.model_manager.sys.platform', 'linux'), \
             patch.dict(sys.modules, {'llama_cpp': fake_llama}):
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
             patch.dict(sys.modules, {'llama_cpp': fake_llama}):
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
             patch.dict(sys.modules, {'llama_cpp': fake_llama}):
            backend = ModelManager._platform_gpu_backend()

        assert backend is None

    def test_llama_gpu_offload_available_uses_marker_fallback(self):
        """GPU support probe should fall back to GGML capability markers."""
        fake_llama = SimpleNamespace(
            GGML_USE_CUDA=False,
            GGML_USE_METAL=True,
            llama_supports_gpu_offload=None,
        )

        with patch.dict(sys.modules, {'llama_cpp': fake_llama}):
            assert ModelManager._llama_gpu_offload_available() is True

    def test_detect_runtime_capabilities_accepts_top_level_ggml_cuda_marker(self):
        from utils.llm import model_manager as mm

        fake_llama = SimpleNamespace(
            GGML_CUDA=True,
            GGML_USE_CUDA=False,
            GGML_USE_METAL=False,
            llama_supports_gpu_offload=None,
            __file__='C:/Python/site-packages/llama_cpp/__init__.py',
        )

        with patch.dict(sys.modules, {'llama_cpp': fake_llama}):
            payload = mm.detect_llama_runtime_capabilities()

        assert payload['backend'] == 'cuda'
        assert payload['gpu_offload_supported'] is True
        assert payload['detected_device'] == 'cuda'

    def test_resolve_compute_plan_gpu_and_hybrid_success_paths(self, model_manager):
        """Explicit gpu/hybrid requests should emit expected diagnostics."""
        model_manager.requested_compute_mode = 'gpu'
        with patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=True):
            gpu_plan = model_manager._resolve_compute_plan()

        assert gpu_plan['effective_mode'] == 'cuda'
        assert gpu_plan['backend_used'] == 'cuda'
        assert gpu_plan['n_gpu_layers'] == -1
        assert gpu_plan['fallback_reason'] is None

        model_manager.requested_compute_mode = 'hybrid'
        model_manager.hybrid_n_gpu_layers = -5
        with patch.object(model_manager, '_platform_gpu_backend', return_value='cuda'), \
             patch.object(model_manager, '_llama_gpu_offload_available', return_value=True):
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
        import builtins
        from utils.llm import model_manager as mm

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'llama_cpp':
                raise ModuleNotFoundError("No module named 'llama_cpp'")
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=fake_import):
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

        with patch('llama_cpp.Llama', FakeLlama), \
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
