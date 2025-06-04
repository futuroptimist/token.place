import importlib
from unittest.mock import MagicMock, patch
import os

@patch.dict(os.environ, {"USE_MOCK_LLM": "0"})
def test_generate_response_uses_real_model():
    # Provide a fake llama_cpp module so api.v1.models can import it
    fake_llama_module = MagicMock()
    with patch.dict('sys.modules', {'llama_cpp': fake_llama_module}):
        import api.v1.models as models
        importlib.reload(models)

        with patch.object(models, 'Llama') as mock_llama:
            mock_instance = MagicMock()
            mock_instance.create_chat_completion.return_value = {
                'choices': [{'message': {'role': 'assistant', 'content': 'real resp'}}]
            }
            mock_llama.return_value = mock_instance

            messages = [{'role': 'user', 'content': 'hi'}]
            result = models.generate_response('llama-3-8b-instruct', messages)

            mock_instance.create_chat_completion.assert_called_once_with(messages=messages)
            assert result[-1]['role'] == 'assistant'
            assert 'real resp' in result[-1]['content']

