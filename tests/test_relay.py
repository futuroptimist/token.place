import pytest
import time
import base64
from flask import Flask
import sys
import os
from datetime import datetime, timedelta

# Add project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from relay import app

# Import the global dictionaries from relay to inspect/manipulate state if needed
# Be cautious with direct manipulation in tests, prefer using API endpoints
from relay import known_servers, client_inference_requests, client_responses

# Generate dummy keys for testing
# (You might want to use the generate_keys function from encrypt.py if needed)
DUMMY_SERVER_PUB_KEY = base64.b64encode(b"server_public_key_123").decode('utf-8')
DUMMY_CLIENT_PUB_KEY = base64.b64encode(b"client_public_key_456").decode('utf-8')


@pytest.fixture
def client():
    """Create a Flask test client fixture"""
    app.config['TESTING'] = True
    # Reset state before each test
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()

    with app.test_client() as client:
        yield client

    # Clean up state after test (optional, as fixture resets before)
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()

# --- Test /next_server ---

def test_next_server_no_servers(client):
    """Test /next_server when no servers are registered."""
    response = client.get("/next_server")
    assert response.status_code == 200 # Endpoint itself works
    data = response.get_json()
    assert 'error' in data
    assert data['error']['message'] == 'No servers available'
    assert data['error']['code'] == 503

def test_next_server_one_server(client):
    """Test /next_server when one server is registered."""
    # Simulate server registration (directly modifying state for setup)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': time.time(),
        'last_ping_duration': 10
    }

    response = client.get("/next_server")
    assert response.status_code == 200
    data = response.get_json()
    assert 'error' not in data
    assert 'server_public_key' in data
    assert data['server_public_key'] == DUMMY_SERVER_PUB_KEY

# --- Test /sink ---

def test_sink_register_new_server(client):
    """Test server registration via /sink."""
    payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert 'next_ping_in_x_seconds' in data
    assert DUMMY_SERVER_PUB_KEY in known_servers
    assert known_servers[DUMMY_SERVER_PUB_KEY]['public_key'] == DUMMY_SERVER_PUB_KEY

def test_sink_update_existing_server(client):
    """Test server ping update via /sink."""
    # Initial registration using datetime
    initial_ping_time = datetime.now() - timedelta(seconds=20)
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': initial_ping_time,
        'last_ping_duration': 10
    }

    time.sleep(0.1) # Ensure time progresses slightly

    # Send update ping
    payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=payload)
    assert response.status_code == 200

    assert DUMMY_SERVER_PUB_KEY in known_servers
    # Compare datetime objects
    assert known_servers[DUMMY_SERVER_PUB_KEY]['last_ping'] > initial_ping_time

def test_sink_invalid_payload(client):
    """Test /sink with missing public key."""
    response = client.post("/sink", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid public key'

# --- Test /faucet ---

def test_faucet_submit_request(client):
    """Test submitting a valid inference request via /faucet."""
    # Register server first
    known_servers[DUMMY_SERVER_PUB_KEY] = {
        'public_key': DUMMY_SERVER_PUB_KEY,
        'last_ping': time.time(),
        'last_ping_duration': 10
    }

    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector"
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['message'] == 'Request received'

    # Check internal state
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1
    queued_req = client_inference_requests[DUMMY_SERVER_PUB_KEY][0]
    assert queued_req['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert queued_req['chat_history'] == "encrypted_chat_history_data"

def test_faucet_invalid_payload(client):
    """Test /faucet with missing fields."""
    # Register server
    known_servers[DUMMY_SERVER_PUB_KEY] = {'public_key': DUMMY_SERVER_PUB_KEY, 'last_ping': time.time(), 'last_ping_duration': 10}

    payload = { "server_public_key": DUMMY_SERVER_PUB_KEY } # Missing other fields
    response = client.post("/faucet", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error']['message'] == 'Invalid request data'

def test_faucet_unknown_server(client):
    """Test /faucet request to an unknown server."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": "unknown_server_key", # This server is not registered
        "chat_history": "encrypted_chat_history_data",
        "cipherkey": "encrypted_aes_key",
        "iv": "initialization_vector"
    }
    response = client.post("/faucet", json=payload)
    assert response.status_code == 404
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Server with the specified public key not found'

# --- Test /source ---

def test_source_submit_response(client):
    """Test server submitting a response via /source."""
    payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_encrypted_response_history",
        "cipherkey": "server_encrypted_aes_key",
        "iv": "server_iv"
    }
    response = client.post("/source", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['message'] == 'Response received and queued for client'

    # Check internal state
    assert DUMMY_CLIENT_PUB_KEY in client_responses
    queued_resp = client_responses[DUMMY_CLIENT_PUB_KEY]
    assert queued_resp['chat_history'] == "server_encrypted_response_history"

def test_source_invalid_payload(client):
    """Test /source with missing fields."""
    payload = { "client_public_key": DUMMY_CLIENT_PUB_KEY } # Missing other fields
    response = client.post("/source", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid request data'

# --- Test /retrieve ---

def test_retrieve_get_response(client):
    """Test client retrieving a queued response via /retrieve."""
    # Queue a response first (directly modify state for setup)
    client_responses[DUMMY_CLIENT_PUB_KEY] = {
        'chat_history': "server_encrypted_response_history",
        'cipherkey': "server_encrypted_aes_key",
        'iv': "server_iv"
    }

    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200
    data = response.get_json()

    assert data['chat_history'] == "server_encrypted_response_history"
    assert data['cipherkey'] == "server_encrypted_aes_key"
    assert data['iv'] == "server_iv"

    # Check state - response should be removed after retrieval
    assert DUMMY_CLIENT_PUB_KEY not in client_responses

def test_retrieve_no_response_available(client):
    """Test /retrieve when no response is queued for the client."""
    payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=payload)
    assert response.status_code == 200 # Endpoint works, just no data
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'No response available for the given public key'

def test_retrieve_invalid_payload(client):
    """Test /retrieve with missing client public key."""
    response = client.post("/retrieve", json={})
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert data['error'] == 'Invalid request data'

# --- Integration Test ---

def test_full_relay_flow(client):
    """Test the full flow: register, faucet, sink poll, source, retrieve."""
    # 1. Server registers via /sink
    sink_payload = {'server_public_key': DUMMY_SERVER_PUB_KEY}
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in known_servers

    # 2. Client requests inference via /faucet
    faucet_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "server_public_key": DUMMY_SERVER_PUB_KEY,
        "chat_history": "client_request_data",
        "cipherkey": "client_key_data",
        "iv": "client_iv_data"
    }
    response = client.post("/faucet", json=faucet_payload)
    assert response.status_code == 200
    assert DUMMY_SERVER_PUB_KEY in client_inference_requests
    assert len(client_inference_requests[DUMMY_SERVER_PUB_KEY]) == 1

    # 3. Server polls /sink and gets the request
    response = client.post("/sink", json=sink_payload)
    assert response.status_code == 200
    sink_data = response.get_json()
    assert sink_data['client_public_key'] == DUMMY_CLIENT_PUB_KEY
    assert sink_data['chat_history'] == "client_request_data"
    assert sink_data['cipherkey'] == "client_key_data"
    assert sink_data['iv'] == "client_iv_data"
    # Request should be removed from queue
    assert not client_inference_requests.get(DUMMY_SERVER_PUB_KEY, [])

    # 4. Server processes and submits response via /source
    source_payload = {
        "client_public_key": DUMMY_CLIENT_PUB_KEY,
        "chat_history": "server_response_data",
        "cipherkey": "server_key_data",
        "iv": "server_iv_data"
    }
    response = client.post("/source", json=source_payload)
    assert response.status_code == 200
    assert DUMMY_CLIENT_PUB_KEY in client_responses

    # 5. Client retrieves response via /retrieve
    retrieve_payload = {"client_public_key": DUMMY_CLIENT_PUB_KEY}
    response = client.post("/retrieve", json=retrieve_payload)
    assert response.status_code == 200
    retrieve_data = response.get_json()
    assert retrieve_data['chat_history'] == "server_response_data"
    assert retrieve_data['cipherkey'] == "server_key_data"
    assert retrieve_data['iv'] == "server_iv_data"
    # Response should be removed from queue
    assert DUMMY_CLIENT_PUB_KEY not in client_responses
