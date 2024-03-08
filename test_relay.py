import pytest
from relay import app
import json
from datetime import datetime

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_index(client):
    """Test the index route."""
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.content_type

def test_next_server_no_servers(client):
    """Test the /next_server endpoint when no servers are known. This should return a 200 status code with an error message in JSON format, as it's part of the application's happy path."""
    response = client.get('/next_server')
    assert response.status_code == 200  # Expecting 200 OK because the request was successfully processed
    # Also check that the JSON response contains the expected error structure and message
    assert response.json == {
        'error': {
            'message': 'No servers available',
            'code': 503  # The code within the JSON can still indicate the specific nature of the error
        }
    }

def test_sink_new_server(client):
    """Test the /sink endpoint with a new server announcement."""
    public_key = "test_public_key"
    response = client.post('/sink', json={'server_public_key': public_key})
    assert response.status_code == 200
    data = response.json
    assert 'next_ping_in_x_seconds' in data

def test_faucet_request_without_known_server(client):
    """Test the /faucet endpoint with an unknown server."""
    response = client.post('/faucet', json={
        'client_public_key': 'client_key',
        'server_public_key': 'unknown_server_key',
        'chat_history': 'encrypted_chat_history',
        'cipherkey': 'mock_cipherkey',  # Mock cipherkey for the test.
        'iv': 'mock_iv'  # Mock IV for the test.
    })
    assert response.status_code == 404
    assert response.json == {'error': 'Server with the specified public key not found'}


def test_retrieve_no_response(client):
    """Test the /retrieve endpoint when there is no response available."""
    response = client.post('/retrieve', json={'client_public_key': 'client_key'})
    assert response.status_code == 200
    assert response.json == {'error': 'No response available for the given public key'}

# Additional tests can be added to cover other endpoints and scenarios, such as:
# - Testing /sink with an existing server (updating last_ping)
# - Testing /faucet with a known server
# - Testing /source and /retrieve with valid data
# - Testing edge cases and error handling

if __name__ == '__main__':
    pytest.main()
