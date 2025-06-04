import pytest
from server.server_app import ServerApp
import json
from encrypt import generate_keys
from unittest.mock import MagicMock, patch
from config import get_config, Config

# Get configuration
config = get_config()

@pytest.fixture
def server_app():
    """Create a ServerApp instance for testing."""
    with patch('server.server_app.model_manager') as mock_model_manager, \
         patch('server.server_app.crypto_manager') as mock_crypto_manager, \
         patch('server.server_app.RelayClient') as mock_relay_client_class:
        
        # Set up return values for mocks
        mock_model_manager.use_mock_llm = True
        
        # Create a test relay client
        mock_relay_client = MagicMock()
        mock_relay_client_class.return_value = mock_relay_client
        
        # Create the server app
        server = ServerApp(
            server_port=9000,  # Use a different port for testing
            relay_port=9001,
            relay_url="http://localhost"
        )
        
        # Patch the start_relay_polling method to avoid starting threads
        server.start_relay_polling = MagicMock()
        
        yield server

@pytest.fixture
def client(server_app):
    """Create a test client for the Flask app."""
    server_app.app.config['TESTING'] = True
    with server_app.app.test_client() as client:
        yield client

@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Ensure we're using testing configuration."""
    # Force testing environment for all server tests
    monkeypatch.setattr('config.config.env', 'testing')
    # Create a new test Config instance with testing env
    test_config = Config(env="testing")
    # Replace the global config with our test config
    monkeypatch.setattr('config.config', test_config)
    monkeypatch.setattr('server.server_app.config', test_config)
    
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
    """Test that the home endpoint returns a valid response."""
    response = client.get('/')
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert 'message' in data

def test_health_endpoint(client):
    """Test the health endpoint."""
    response = client.get('/health')
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert 'version' in data
    assert 'mock_mode' in data

def test_server_initialization(server_app):
    """Test that the server initializes correctly."""
    # Check that the server object has the expected attributes
    assert server_app.server_port == 9000
    assert server_app.relay_port == 9001
    assert server_app.relay_url == "http://localhost"
    assert server_app.app is not None

def test_setup_routes(server_app):
    """Test that the routes are set up correctly."""
    # Check that the expected routes are registered
    routes = [rule.rule for rule in server_app.app.url_map.iter_rules()]
    assert '/' in routes
    assert '/health' in routes

def test_initialize_llm_mock_mode(server_app):
    """Test the initialize_llm method in mock mode."""
    with patch('server.server_app.model_manager') as mock_model_manager:
        mock_model_manager.use_mock_llm = True
        server_app.initialize_llm()
        # Verify that download_model_if_needed was not called
        mock_model_manager.download_model_if_needed.assert_not_called()

def test_initialize_llm_real_mode(server_app):
    """Test the initialize_llm method in real mode."""
    with patch('server.server_app.model_manager') as mock_model_manager:
        mock_model_manager.use_mock_llm = False
        mock_model_manager.download_model_if_needed.return_value = True
        server_app.initialize_llm()
        # Verify that download_model_if_needed was called
        mock_model_manager.download_model_if_needed.assert_called_once()
