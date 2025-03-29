import pytest
from server import app
import json
from encrypt import generate_keys
from unittest.mock import MagicMock, patch
from config import get_config, Config

# Get configuration
config = get_config()

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

@pytest.fixture(autouse=True)
def mock_keys(monkeypatch):
    def mock_generate_keys():
        return b'mock_private_key', b'mock_public_key'
    monkeypatch.setattr('server.generate_keys', mock_generate_keys)

@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Mocks the get_llm_instance function to return a mock Llama object."""
    # Create a mock Llama instance
    mock_llama_instance = MagicMock()
    
    # Configure the mock create_chat_completion to return a sample response
    mock_response = {
        'choices': [
            {
                'message': {
                    'role': 'assistant',
                    'content': 'This is a mock response from the Llama model.'
                }
            }
        ]
        # Add other fields if your code uses them
    }
    mock_llama_instance.create_chat_completion.return_value = mock_response

    # Mock the get_llm_instance function in the server module
    monkeypatch.setattr('server.get_llm_instance', lambda: mock_llama_instance)

# Mock config for testing
@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Ensure we're using testing configuration."""
    # Force testing environment for all server tests
    # Instead of setting is_testing directly, set the env property
    monkeypatch.setattr('config.config.env', 'testing')
    # Create a new test Config instance with testing env
    test_config = Config(env="testing")
    # Replace the global config with our test config
    monkeypatch.setattr('config.config', test_config)
    monkeypatch.setattr('server.config', test_config)
    monkeypatch.setattr('server.USE_MOCK_LLM', True)
    
    # Ensure the models directory exists
    import os
    from utils.path_handling import ensure_dir_exists
    models_dir = config.get('paths.models_dir')
    ensure_dir_exists(models_dir)
    
    # Create a dummy model file if needed for tests
    dummy_model_path = os.path.join(models_dir, config.get('model.filename'))
    if not os.path.exists(dummy_model_path):
        with open(dummy_model_path, 'wb') as f:
            f.write(b'dummy model data for testing')

def test_home_endpoint(client):
    """Test that the home endpoint accepts POST requests and returns a valid response."""
    response = client.post('/', json={'chat_history': []})
    assert response.status_code == 200
    assert isinstance(response.get_json(), list)

def test_model_integration(client):
    """Test the model integration by sending a simulated chat history."""
    chat_history = [{"role": "user", "content": "Hello, how are you?"}]
    response = client.post('/', json={'chat_history': chat_history})
    assert response.status_code == 200
    assert isinstance(response.get_json(), list)
    response_data = response.get_json()
    assert any(item.get('role') == 'assistant' for item in response_data)

def test_invalid_request(client):
    """Test sending an invalid request to the home endpoint."""
    response = client.post('/', data=json.dumps({'invalid_key': 'invalid_value'}), content_type='application/json')
    assert response.status_code == 400
    assert response.get_json() == {'error': 'Invalid request format'}

def test_invalid_method(client):
    """Test sending a GET request to the home endpoint."""
    response = client.get('/')
    assert response.status_code == 405
    assert response.get_json() == {'error': 'Method not allowed'}