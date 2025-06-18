import pytest
# import requests  # No longer needed
import json
import base64
import time
from encrypt import encrypt, decrypt, generate_keys
import sys
import os

# Add project root to the Python path to import relay
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from relay import app

# API base URL for testing - No longer needed with test client
# API_BASE_URL = "http://localhost:5000/api/v1"

# Set app to testing mode
app.config['TESTING'] = True

# --- BEGIN NEW MOCK SETUP ---
# Mock the Llama class before it's used by api.v1.models
@pytest.fixture(autouse=True)
def mock_llama(mocker):
    """Mocks the llama_cpp.Llama class for all tests."""
    mock_response = {
        'choices': [{
            'message': {'role': 'assistant', 'content': 'Mock response: The capital of France is Paris.'}
        }]
    }
    mock_instance = mocker.Mock()
    mock_instance.create_chat_completion.return_value = mock_response
    mocker.patch('api.v1.models.Llama', return_value=mock_instance, autospec=True)
    return mock_instance
# --- END NEW MOCK SETUP ---

@pytest.fixture
def client():
    """Create a Flask test client fixture"""
    with app.test_client() as client:
        yield client

@pytest.fixture
def client_keys():
    """Generate client keys for testing encrypted API calls"""
    private_key, public_key = generate_keys()
    public_key_b64 = base64.b64encode(public_key).decode('utf-8')
    return {
        'private_key': private_key,
        'public_key': public_key,
        'public_key_b64': public_key_b64
    }

def test_api_health(client):
    """Test the API health endpoint"""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert data['version'] == 'v1'
    assert 'timestamp' in data

def test_list_models(client):
    """Test the models list endpoint"""
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    data = response.get_json()
    assert data['object'] == 'list'
    assert isinstance(data['data'], list)
    assert len(data['data']) > 0
    
    # Verify model format
    model = data['data'][0]
    assert 'id' in model
    assert 'object' in model
    assert model['object'] == 'model'
    assert 'owned_by' in model
    assert 'permission' in model

def test_get_model(client, mock_llama):
    """Test retrieving a specific model"""
    # First get the list of models
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    models = response.get_json()['data']
    
    # Get the first model's ID
    model_id = models[0]['id']
    
    # Retrieve the specific model
    response = client.get(f"/api/v1/models/{model_id}")
    assert response.status_code == 200
    model = response.get_json()
    
    # Verify it's the same model
    assert model['id'] == model_id
    assert model['object'] == 'model'

def test_get_public_key(client):
    """Test retrieving the server's public key"""
    response = client.get("/api/v1/public-key")
    assert response.status_code == 200
    data = response.get_json()
    assert 'public_key' in data
    assert len(data['public_key']) > 0

def test_unencrypted_chat_completion(client, client_keys, mock_llama):
    """Test the chat completion API without encryption"""
    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"}
        ]
    }
    
    response = client.post("/api/v1/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    
    # Verify response format
    assert 'id' in data
    assert data['object'] == 'chat.completion'
    assert 'choices' in data
    assert len(data['choices']) > 0
    assert 'message' in data['choices'][0]
    assert data['choices'][0]['message']['role'] == 'assistant'
    assert len(data['choices'][0]['message']['content']) > 0
    assert 'Mock response' in data['choices'][0]['message']['content']
    print(f"Unencrypted Response: {data['choices'][0]['message']['content']}")

def test_encrypted_chat_completion(client, client_keys, mock_llama):
    """Test the chat completion API with encryption"""
    # Get the server's public key
    response = client.get("/api/v1/public-key")
    assert response.status_code == 200
    server_public_key = response.get_json()['public_key']
    
    # Prepare the message data
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
    
    # Encrypt the messages
    server_public_key_bytes = base64.b64decode(server_public_key)
    ciphertext_dict, cipherkey, iv = encrypt(json.dumps(messages).encode('utf-8'), server_public_key_bytes)
    
    # Create the encrypted request payload
    payload = {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "client_public_key": client_keys['public_key_b64'],
        "messages": {
            "ciphertext": base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
            "cipherkey": base64.b64encode(cipherkey).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8')
        }
    }
    
    # Send the request
    response = client.post("/api/v1/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    
    # Verify response format for encrypted response
    assert 'encrypted' in data
    assert data['encrypted'] == True
    assert 'data' in data
    assert 'ciphertext' in data['data']
    assert 'cipherkey' in data['data']
    assert 'iv' in data['data']
    
    # Decrypt the response
    ciphertext = base64.b64decode(data['data']['ciphertext'])
    cipherkey = base64.b64decode(data['data']['cipherkey'])
    iv = base64.b64decode(data['data']['iv'])
    
    decrypted_bytes = decrypt({'ciphertext': ciphertext, 'iv': iv}, cipherkey, client_keys['private_key'])
    assert decrypted_bytes is not None
    
    # Parse the decrypted data
    decrypted_data = json.loads(decrypted_bytes.decode('utf-8'))
    
    # Verify the decrypted response format
    assert 'id' in decrypted_data
    assert decrypted_data['object'] == 'chat.completion'
    assert 'choices' in decrypted_data
    assert len(decrypted_data['choices']) > 0
    assert 'message' in decrypted_data['choices'][0]
    assert decrypted_data['choices'][0]['message']['role'] == 'assistant'
    assert len(decrypted_data['choices'][0]['message']['content']) > 0
    assert 'Mock response' in decrypted_data['choices'][0]['message']['content']
    print(f"Decrypted Response: {decrypted_data['choices'][0]['message']['content']}")

def test_completions_endpoint(client, mock_llama):
    """Test the regular completions endpoint (redirects to chat)"""
    payload = {
        "model": "llama-3-8b-instruct",
        "prompt": "What is the capital of France?",
        "max_tokens": 100
    }
    
    response = client.post("/api/v1/completions", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    
    # Verify the response is in text completion format
    assert 'id' in data
    assert data['object'] == 'text_completion'
    assert 'choices' in data
    assert len(data['choices']) > 0
    assert 'text' in data['choices'][0]
    assert 'Mock response' in data['choices'][0]['text']
    print(f"Completions Endpoint Response: {data['choices'][0]['text']}")

def test_error_handling(client, mock_llama):
    """Test API error handling"""
    # Test missing model parameter
    payload = {
        "messages": [
            {"role": "user", "content": "What is the capital of France?"}
        ]
    }
    
    response = client.post("/api/v1/chat/completions", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert 'message' in data['error']

    # Test invalid model ID
    model_id = "non-existent-model"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "user", "content": "What is the capital of France?"}
        ]
    }
    
    response = client.post("/api/v1/chat/completions", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert 'error' in data
    assert model_id in data['error']['message']

# Add more tests as needed
# Consider adding tests for different error conditions, edge cases, etc.
# Example: test invalid encryption data, test max_tokens, etc. 


def test_get_model_not_found(client):
    resp = client.get('/api/v1/models/does-not-exist')
    assert resp.status_code == 404
    data = resp.get_json()
    assert 'error' in data
    assert data['error']['type'] == 'invalid_request_error'


def test_completions_encryption_error(client, monkeypatch, mock_llama):
    payload = {
        'model': 'llama-3-8b-instruct',
        'prompt': 'hi',
        'encrypted': True,
        'client_public_key': 'bogus'
    }
    monkeypatch.setattr('api.v1.routes.encryption_manager.encrypt_message', lambda *a, **k: None)
    response = client.post('/api/v1/completions', json=payload)
    assert response.status_code == 500
    data = response.get_json()
    assert 'error' in data
    assert 'Failed to encrypt response' in data['error']['message']


def test_create_completion_encrypted_success(client, monkeypatch, mock_llama):
    monkeypatch.setattr('api.v1.routes.get_model_instance', lambda m: object())
    monkeypatch.setattr('api.v1.routes.generate_response', lambda m, msgs: msgs + [{'role':'assistant','content':'ok'}])
    monkeypatch.setattr('api.v1.routes.encryption_manager.encrypt_message', lambda data, key: {'ciphertext':'a','cipherkey':'b','iv':'c'})
    payload = {'model':'llama-3-8b-instruct','prompt':'hi','encrypted':True,'client_public_key':'x'}
    res = client.post('/api/v1/completions', json=payload)
    assert res.status_code == 200
    d = res.get_json()
    assert d['encrypted'] is True


def test_create_chat_completion_model_error(client, monkeypatch, mock_llama):
    class DummyErr(Exception):
        pass
    from api.v1.models import ModelError
    monkeypatch.setattr('api.v1.routes.get_model_instance', lambda m: object())
    monkeypatch.setattr('api.v1.routes.generate_response', lambda m, msgs: (_ for _ in ()).throw(ModelError('boom')))
    payload = {'model':'llama-3-8b-instruct','messages':[{'role':'user','content':'hi'}]}
    res = client.post('/api/v1/chat/completions', json=payload)
    assert res.status_code == 400
    assert 'error' in res.get_json()


def test_create_completion_exception(client, monkeypatch, mock_llama):
    monkeypatch.setattr('api.v1.routes.get_model_instance', lambda m: (_ for _ in ()).throw(RuntimeError('oops')))
    payload = {'model':'llama-3-8b-instruct','prompt':'hi'}
    res = client.post('/api/v1/completions', json=payload)
    assert res.status_code == 400
    assert 'error' in res.get_json()
