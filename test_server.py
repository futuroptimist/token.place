import pytest
from server import app
import json

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

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