import pytest
# import requests  # No longer needed
import json
import base64
import time
import logging
import importlib
import inspect
from unittest.mock import MagicMock, patch
from encrypt import encrypt, decrypt, generate_keys, decrypt_stream_chunk
import sys
import os

# Add project root to the Python path to import relay
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from relay import app
from api.v1.routes import format_error_response

OPERATOR_TOKEN_ENV_VARS = (
    "TOKEN_PLACE_OPERATOR_TOKEN",
    "TOKEN_PLACE_KEY_ROTATION_TOKEN",
    "PUBLIC_KEY_ROTATION_TOKEN",
)


def clear_operator_token_env(monkeypatch):
    """Remove any configured operator tokens for the duration of a test."""

    for env_var in OPERATOR_TOKEN_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)

# API base URL for testing - No longer needed with test client
# API_BASE_URL = "http://localhost:5000/api/v1"

# Set app to testing mode
app.config['TESTING'] = True

# --- BEGIN NEW MOCK SETUP ---
# Mock the Llama class before it's used by api.v1.models
@pytest.fixture(autouse=True)
def sync_api_module_references():
    """Point ``sys.modules`` at the same API module objects bound to ``app`` views."""

    routes_module = None
    for view_fn in app.view_functions.values():
        if getattr(view_fn, "__module__", "") == "api.v1.routes":
            routes_module = inspect.getmodule(view_fn)
            break

    if routes_module is None:
        yield
        return

    models_module = inspect.getmodule(routes_module.get_model_instance)

    import api.v1 as api_v1_pkg

    sys.modules["api.v1.routes"] = routes_module
    setattr(api_v1_pkg, "routes", routes_module)

    if models_module is not None:
        sys.modules["api.v1.models"] = models_module
        setattr(api_v1_pkg, "models", models_module)

    yield


@pytest.fixture(autouse=True)
def mock_llama():
    """Mock the ``api.v1.models.Llama`` class for all tests."""
    mock_response = {
        'choices': [{
            'message': {'role': 'assistant', 'content': 'Mock response: The capital of France is Paris.'}
        }]
    }
    mock_instance = MagicMock()
    mock_instance.create_chat_completion.return_value = mock_response
    models_module = importlib.import_module("api.v1.models")
    with patch.object(models_module, "Llama", MagicMock(return_value=mock_instance)):
        yield mock_instance
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


def _encrypt_messages_for_server(messages, server_public_key):
    """Encrypt chat messages using the server public key for request payloads."""

    server_public_key_bytes = base64.b64decode(server_public_key)
    ciphertext_dict, cipherkey, iv = encrypt(
        json.dumps(messages).encode('utf-8'),
        server_public_key_bytes,
    )

    return {
        'ciphertext': base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
        'cipherkey': base64.b64encode(cipherkey).decode('utf-8'),
        'iv': base64.b64encode(iv).decode('utf-8'),
    }

def test_api_health(client):
    """Test the API health endpoint"""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert data['version'] == 'v1'
    assert data['service'] == 'token.place'
    assert 'timestamp' in data


def test_api_health_reflects_service_env(monkeypatch, client):
    """Health endpoint should include the configured service identifier."""

    monkeypatch.setenv("SERVICE_NAME", "token.place-ci")

    response = client.get("/api/v1/health")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["service"] == "token.place-ci"

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


def test_server_provider_directory(client):
    """The server provider registry should list known compute providers."""
    response = client.get("/api/v1/server-providers")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["object"] == "list"
    providers = payload["data"]
    assert isinstance(providers, list)
    assert providers, "expected at least one provider"

    sample = providers[0]
    required_fields = {"id", "name", "region", "status", "endpoints"}
    assert required_fields.issubset(sample)
    assert isinstance(sample["endpoints"], list)
    assert sample["endpoints"], "providers should surface at least one endpoint"

    for endpoint in sample["endpoints"]:
        assert "type" in endpoint
        assert "url" in endpoint

def test_unencrypted_chat_completion(client, client_keys, mock_llama):
    """Test the chat completion API without encryption"""
    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"}
        ]
    }

    response = client.post("/api/v2/chat/completions", json=payload)
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


def _clear_distributed_target_env(monkeypatch):
    for env_name in (
        "TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL",
        "TOKENPLACE_DISTRIBUTED_RELAY_URL",
        "TOKENPLACE_DISTRIBUTED_COMPUTE_URL",
        "TOKENPLACE_RELAY_INTERNAL_URL",
        "TOKEN_PLACE_RELAY_INTERNAL_URL",
        "RELAY_INTERNAL_URL",
        "TOKENPLACE_RELAY_PUBLIC_URL",
        "TOKEN_PLACE_RELAY_PUBLIC_URL",
        "RELAY_PUBLIC_URL",
    ):
        monkeypatch.delenv(env_name, raising=False)


def test_api_v1_distributed_provider_uses_staging_relay_public_url(monkeypatch, caplog):
    import api.v1.compute_provider as compute_provider

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://staging.token.place")
    compute_provider._build_api_v1_compute_provider.cache_clear()

    with caplog.at_level(logging.INFO, logger="api.v1.compute_provider"):
        provider = compute_provider.get_api_v1_compute_provider()

    assert isinstance(provider, compute_provider.DistributedApiV1ComputeProvider)
    assert provider.base_url == "https://staging.token.place"
    assert provider.base_url != "https://token.place"
    assert "target=https://staging.token.place" in caplog.text
    assert "target_source=relay_public_env:TOKENPLACE_RELAY_PUBLIC_URL" in caplog.text
    assert "relay_only=True" in caplog.text


def test_api_v1_distributed_provider_production_uses_default_target(monkeypatch, caplog):
    import api.v1.compute_provider as compute_provider

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    compute_provider._build_api_v1_compute_provider.cache_clear()

    with caplog.at_level(logging.INFO, logger="api.v1.compute_provider"):
        provider = compute_provider.get_api_v1_compute_provider()

    assert isinstance(provider, compute_provider.DistributedApiV1ComputeProvider)
    assert provider.base_url == "https://token.place"
    assert "target=https://token.place" in caplog.text
    assert "target_source=production_default" in caplog.text
    assert "relay_only=False" in caplog.text


def test_api_v1_distributed_provider_production_prefers_non_default_relay_config(
    monkeypatch, caplog
):
    import api.v1.compute_provider as compute_provider

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")

    config_values = {
        "api.relay_url": "https://token.place",
        "relay.server_url": "https://prod-relay.example",
    }
    monkeypatch.setattr(
        compute_provider,
        "_config_value",
        lambda key: config_values.get(key, ""),
    )
    compute_provider._build_api_v1_compute_provider.cache_clear()

    with caplog.at_level(logging.INFO, logger="api.v1.compute_provider"):
        provider = compute_provider.get_api_v1_compute_provider()

    assert isinstance(provider, compute_provider.DistributedApiV1ComputeProvider)
    assert provider.base_url == "https://prod-relay.example"
    assert "target=https://prod-relay.example" in caplog.text
    assert "target_source=config:relay.server_url" in caplog.text
    assert "target_source=production_default" not in caplog.text


def test_api_v1_distributed_provider_staging_fails_without_target(monkeypatch):
    import api.v1.compute_provider as compute_provider

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    compute_provider._build_api_v1_compute_provider.cache_clear()

    with pytest.raises(compute_provider.ComputeProviderError) as exc_info:
        compute_provider.get_api_v1_compute_provider()

    assert "outside production" in str(exc_info.value)


def test_api_v1_distributed_provider_staging_rejects_malformed_relay_public_url(
    monkeypatch,
):
    import api.v1.compute_provider as compute_provider

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "staging.token.place")

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("Distributed provider should not be constructed")

    monkeypatch.setattr(
        compute_provider,
        "DistributedApiV1ComputeProvider",
        fail_if_constructed,
    )
    compute_provider._build_api_v1_compute_provider.cache_clear()

    with pytest.raises(compute_provider.ComputeProviderError) as exc_info:
        compute_provider.get_api_v1_compute_provider()

    message = str(exc_info.value)
    assert "env:TOKENPLACE_RELAY_PUBLIC_URL" in message
    assert "absolute HTTP(S) URL" in message


def test_api_v1_chat_completion_staging_no_nodes_returns_clear_503(client, monkeypatch):
    import api.v1.compute_provider as compute_provider

    class NoNodesResponse:
        status_code = 503

        def json(self):
            return {
                "error": {
                    "message": "No registered compute nodes are available on this relay.",
                    "code": "no_registered_compute_nodes",
                }
            }

    def fake_get(url, timeout):
        assert url == "https://staging.token.place/api/v1/relay/servers/next"
        return NoNodesResponse()

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://staging.token.place")
    monkeypatch.setattr(compute_provider.requests, "get", fake_get)
    compute_provider._build_api_v1_compute_provider.cache_clear()

    response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "hello staging"}],
        },
    )

    assert response.status_code == 503
    data = response.get_json()
    assert data["error"]["code"] == "no_registered_compute_nodes"
    assert (
        data["error"]["message"]
        == "No registered compute nodes are available on this relay."
    )


def test_api_v1_chat_completion_returns_503_when_distributed_has_no_registered_nodes(client, monkeypatch):
    monkeypatch.setenv('TOKENPLACE_API_V1_COMPUTE_PROVIDER', 'distributed')
    monkeypatch.setenv('TOKENPLACE_DISTRIBUTED_COMPUTE_URL', 'https://compute.example')
    monkeypatch.setenv('TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK', '0')

    payload = {
        'model': 'llama-3-8b-instruct',
        'messages': [{'role': 'user', 'content': 'Ping distributed runtime'}],
        'temperature': 0.2,
        'stop': ['END'],
    }
    response = client.post('/api/v1/chat/completions', json=payload)
    assert response.status_code == 503
    data = response.get_json()
    assert data['error']['type'] == 'service_unavailable_error'
    assert data['error']['code'] in {'no_registered_compute_nodes', 'compute_node_unreachable'}


def test_api_v1_chat_completion_distributed_provider_falls_back_to_local(client, monkeypatch):
    fallback_message = {
        'role': 'assistant',
        'content': 'local fallback response',
    }

    monkeypatch.setattr(
        'api.v1.compute_provider.generate_response',
        lambda _model, messages, **_options: messages + [fallback_message],
    )

    monkeypatch.setenv('TOKENPLACE_API_V1_COMPUTE_PROVIDER', 'distributed')
    monkeypatch.setenv('TOKENPLACE_DISTRIBUTED_COMPUTE_URL', 'https://compute.example')

    response = client.post(
        '/api/v1/chat/completions',
        json={
            'model': 'llama-3-8b-instruct',
            'messages': [{'role': 'user', 'content': 'fallback please'}],
        },
    )

    assert response.status_code == 200
    assert response.get_json()['choices'][0]['message']['content'] == 'local fallback response'


def test_api_v1_chat_completion_distributed_no_fallback_returns_503(client, monkeypatch):
    monkeypatch.setenv('TOKENPLACE_API_V1_COMPUTE_PROVIDER', 'distributed')
    monkeypatch.setenv('TOKENPLACE_DISTRIBUTED_COMPUTE_URL', 'https://compute.example')
    monkeypatch.setenv('TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK', '0')

    monkeypatch.setattr(
        'api.v1.routes.get_api_v1_compute_provider',
        lambda: importlib.import_module('api.v1.compute_provider').DistributedApiV1ComputeProvider(
            base_url='https://compute.example'
        ),
    )

    local_generate = MagicMock(side_effect=AssertionError('local generation should not run'))
    monkeypatch.setattr('api.v1.compute_provider.generate_response', local_generate)

    response = client.post(
        '/api/v1/chat/completions',
        json={
            'model': 'llama-3-8b-instruct',
            'messages': [{'role': 'user', 'content': 'no fallback please'}],
        },
    )

    assert response.status_code == 503
    assert response.get_json()['error']['code'] in {
        'no_registered_compute_nodes',
        'compute_node_unreachable',
    }
    local_generate.assert_not_called()




def test_api_v1_chat_completion_local_provider_rejects_unsupported_model(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'llama-3-8b-instruct'}])

    class ProviderShouldNotRun:
        def complete_chat(self, **kwargs):
            raise AssertionError('provider should not be called for unsupported local models')

    monkeypatch.setattr('api.v1.routes.get_api_v1_compute_provider', lambda: ProviderShouldNotRun())
    monkeypatch.setattr('api.v1.routes.get_api_v1_resolved_provider_path', lambda _provider: 'local')

    response = client.post('/api/v1/chat/completions', json={
        'model': 'remote-only-model',
        'messages': [{'role': 'user', 'content': 'hello'}],
    })

    assert response.status_code == 400
    body = response.get_json()
    assert body['error']['param'] == 'model'
    assert body['error']['code'] == 'model_not_supported'


def test_api_v1_chat_completion_distributed_with_fallback_allows_model_absent_from_local_catalogue(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'llama-3-8b-instruct'}])
    monkeypatch.setattr('api.v1.routes.validate_chat_messages', lambda msgs: None)

    captured = {}

    class FakeDistributedProvider:
        def complete_chat(self, *, model_id, messages, options=None):
            captured['model_id'] = model_id
            captured['messages'] = messages
            return {'role': 'assistant', 'content': 'distributed ok'}

    monkeypatch.setenv('TOKENPLACE_API_V1_COMPUTE_PROVIDER', 'distributed')
    monkeypatch.setattr('api.v1.routes.get_api_v1_compute_provider', lambda: FakeDistributedProvider())
    monkeypatch.setattr('api.v1.routes.get_api_v1_resolved_provider_path', lambda _provider: 'distributed_with_local_fallback')
    monkeypatch.setattr('api.v1.routes.get_api_v1_last_backend_path', lambda: 'distributed_relay_e2ee')

    response = client.post('/api/v1/chat/completions', json={
        'model': 'remote-only-model',
        'messages': [{'role': 'user', 'content': 'route remotely'}],
    })

    assert response.status_code == 200
    assert captured['model_id'] == 'remote-only-model'
    assert response.get_json()['choices'][0]['message']['content'] == 'distributed ok'




def test_api_v1_completions_distributed_with_fallback_allows_model_absent_from_local_catalogue(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'llama-3-8b-instruct'}])

    captured = {}

    class FakeDistributedProvider:
        def complete_chat(self, *, model_id, messages, options=None):
            captured['model_id'] = model_id
            captured['messages'] = messages
            return {'role': 'assistant', 'content': 'distributed completion ok'}

    monkeypatch.setenv('TOKENPLACE_API_V1_COMPUTE_PROVIDER', 'distributed')
    monkeypatch.setattr('api.v1.routes.get_api_v1_compute_provider', lambda: FakeDistributedProvider())
    monkeypatch.setattr('api.v1.routes.get_api_v1_resolved_provider_path', lambda _provider: 'distributed_with_local_fallback')
    monkeypatch.setattr('api.v1.routes.get_api_v1_last_backend_path', lambda: 'distributed_relay_e2ee')

    response = client.post('/api/v1/completions', json={
        'model': 'remote-only-model',
        'prompt': 'route remotely',
    })

    assert response.status_code == 200
    assert captured['model_id'] == 'remote-only-model'
    assert response.get_json()['choices'][0]['text'] == 'distributed completion ok'


def test_api_v1_completions_local_provider_rejects_unsupported_model(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'llama-3-8b-instruct'}])

    class ProviderShouldNotRun:
        def complete_chat(self, **kwargs):
            raise AssertionError('provider should not be called for unsupported local completion models')

    monkeypatch.setattr('api.v1.routes.get_api_v1_compute_provider', lambda: ProviderShouldNotRun())
    monkeypatch.setattr('api.v1.routes.get_api_v1_resolved_provider_path', lambda _provider: 'local')

    response = client.post('/api/v1/completions', json={
        'model': 'unsupported-model',
        'prompt': 'hello',
    })

    assert response.status_code == 400
    body = response.get_json()
    assert body['error']['param'] == 'model'
    assert body['error']['code'] == 'model_not_supported'


def test_chat_completion_rejects_empty_messages(client):
    """Empty chat message arrays should be rejected as invalid input."""

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [],
    }

    response = client.post("/api/v1/chat/completions", json=payload)
    assert response.status_code == 400

    error = response.get_json()
    assert error["error"]["type"] == "invalid_request_error"
    assert error["error"].get("param") == "messages"
    assert error["error"]["message"] == "messages must contain at least one item"


def test_encrypted_chat_completion(client, client_keys, mock_llama):
    """Test the chat completion API with encryption"""
    # Get the server's public key
    response = client.get("/api/v2/public-key")
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
    response = client.post("/api/v2/chat/completions", json=payload)
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


def test_encrypted_streaming_requires_client_key(client, mock_llama):
    """Encrypted streaming must provide a client public key for SSE envelopes."""

    public_key_resp = client.get("/api/v2/public-key")
    assert public_key_resp.status_code == 200
    server_public_key = public_key_resp.get_json()['public_key']

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Stream an encrypted response."},
    ]

    encrypted_messages = _encrypt_messages_for_server(messages, server_public_key)

    payload = {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "stream": True,
        "client_public_key": "",
        "messages": encrypted_messages,
    }

    response = client.post("/api/v2/chat/completions", json=payload)
    assert response.status_code == 400

    error = response.get_json()
    assert error["error"]["type"] == "encryption_error"
    assert error["error"]["message"] == "Client public key required for encrypted streaming"


def test_encrypted_streaming_chat_completion(client, client_keys, mock_llama):
    """Encrypted streaming responses should emit encrypted SSE chunks."""

    public_key_resp = client.get("/api/v2/public-key")
    assert public_key_resp.status_code == 200
    server_public_key = public_key_resp.get_json()['public_key']

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Stream an encrypted response."},
    ]

    encrypted_messages = _encrypt_messages_for_server(messages, server_public_key)

    payload = {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "stream": True,
        "client_public_key": client_keys['public_key_b64'],
        "messages": encrypted_messages,
    }

    response = client.post(
        "/api/v2/chat/completions",
        json=payload,
        headers={"Accept": "text/event-stream"},
    )

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers['Cache-Control'] == 'no-cache'

    body = b"".join(response.response).decode("utf-8")
    events = [chunk for chunk in body.split("\n\n") if chunk.strip()]

    assert events, "expected streaming events to be returned"
    assert events[-1].strip() == "data: [DONE]"

    encrypted_chunks = events[:-1]
    assert encrypted_chunks, "expected at least one encrypted data chunk"

    stream_session_id = None
    decrypt_session = None
    decrypted_events = []
    for index, chunk in enumerate(encrypted_chunks):
        assert chunk.startswith("data: ")
        payload_json = chunk[len("data: ") :]
        envelope = json.loads(payload_json)

        assert envelope["event"] == "delta"
        assert envelope["encrypted"] is True

        if stream_session_id is None:
            stream_session_id = envelope.get("stream_session_id")
            assert isinstance(stream_session_id, str)
        else:
            assert envelope.get("stream_session_id") == stream_session_id

        encrypted_payload = envelope["data"]
        assert encrypted_payload["encrypted"] is True
        assert encrypted_payload.get("stream_session_id") == stream_session_id

        ciphertext_dict = {
            "ciphertext": base64.b64decode(encrypted_payload["ciphertext"]),
            "iv": base64.b64decode(encrypted_payload["iv"]),
        }

        if "tag" in encrypted_payload:
            ciphertext_dict["tag"] = base64.b64decode(encrypted_payload["tag"])

        mode = encrypted_payload.get("mode")
        associated_data_value = encrypted_payload.get("associated_data")
        associated_data = (
            base64.b64decode(associated_data_value)
            if isinstance(associated_data_value, str)
            else None
        )

        if index == 0:
            assert "cipherkey" in encrypted_payload
            encrypted_key_bytes = base64.b64decode(encrypted_payload["cipherkey"])
        else:
            assert "cipherkey" not in encrypted_payload
            encrypted_key_bytes = None

        decrypted_bytes, decrypt_session = decrypt_stream_chunk(
            ciphertext_dict,
            client_keys['private_key'],
            session=decrypt_session,
            encrypted_key=encrypted_key_bytes,
            cipher_mode=mode,
            associated_data=associated_data,
        )

        decrypted_events.append(json.loads(decrypted_bytes.decode("utf-8")))

    assert all(event["object"] == "chat.completion.chunk" for event in decrypted_events)

    role_chunks = [
        event["choices"][0]["delta"].get("role")
        for event in decrypted_events
        if "role" in event["choices"][0]["delta"]
    ]
    assert role_chunks and role_chunks[0] == "assistant"

    content_chunks = [
        event["choices"][0]["delta"].get("content")
        for event in decrypted_events
        if "content" in event["choices"][0]["delta"]
    ]
    assert any("Mock response" in chunk for chunk in content_chunks)

    assert decrypted_events[-1]["choices"][0]["finish_reason"] == "stop"

def test_public_key_rotation_rejected_without_operator_token_config(client, monkeypatch):
    """Rotation should fail fast if operator authentication is not configured."""

    clear_operator_token_env(monkeypatch)

    response = client.post("/api/v1/public-key/rotate")
    assert response.status_code == 503
    payload = response.get_json()
    assert payload["error"]["code"] == "operator_auth_not_configured"


def test_public_key_rotation_rejects_invalid_token(client, monkeypatch):
    """Configured rotation should reject requests without a valid operator token."""

    clear_operator_token_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_KEY_ROTATION_TOKEN", "expected-token")

    response = client.post(
        "/api/v1/public-key/rotate",
        headers={"X-Token-Place-Operator": "wrong-token"},
    )

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"]["code"] == "operator_token_invalid"


def test_public_key_rotation_accepts_custom_header(client, monkeypatch):
    """The X-Token-Place-Operator header should authorize key rotation across APIs."""

    clear_operator_token_env(monkeypatch)
    # Ensure we exercise the fallback environment variables, not just the primary one.
    monkeypatch.setenv("TOKEN_PLACE_OPERATOR_TOKEN", "", prepend=False)
    monkeypatch.setenv("TOKEN_PLACE_KEY_ROTATION_TOKEN", "expected-token")

    response = client.post(
        "/api/v2/public-key/rotate",
        headers={"X-Token-Place-Operator": "expected-token"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert "public_key" in payload
    assert isinstance(payload["public_key"], str)


def test_public_key_rotation_updates_encryption_flow(client, client_keys, mock_llama, monkeypatch):
    """Rotating the public key should issue a new key and keep encrypted flows working."""

    monkeypatch.setenv("TOKEN_PLACE_OPERATOR_TOKEN", "test-operator-token")

    # Capture the original key
    original_key_resp = client.get("/api/v2/public-key")
    assert original_key_resp.status_code == 200
    original_key = original_key_resp.get_json()["public_key"]

    # Rotate via the v1 endpoint to ensure backwards compatibility
    rotate_resp = client.post(
        "/api/v1/public-key/rotate",
        headers={"Authorization": "Bearer test-operator-token"},
    )
    assert rotate_resp.status_code == 200
    rotated_key = rotate_resp.get_json()["public_key"]

    assert rotated_key != original_key

    # The v2 endpoint should now expose the rotated key as well
    follow_up_resp = client.get("/api/v2/public-key")
    assert follow_up_resp.status_code == 200
    assert follow_up_resp.get_json()["public_key"] == rotated_key

    # Encrypt a new chat payload with the rotated key
    server_public_key_bytes = base64.b64decode(rotated_key)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Demonstrate key rotation."}
    ]

    ciphertext_dict, cipherkey, iv = encrypt(
        json.dumps(messages).encode("utf-8"),
        server_public_key_bytes,
    )

    payload = {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "client_public_key": client_keys["public_key_b64"],
        "messages": {
            "ciphertext": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
            "cipherkey": base64.b64encode(cipherkey).decode("utf-8"),
            "iv": base64.b64encode(iv).decode("utf-8"),
        },
    }

    response = client.post("/api/v2/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.get_json()

    assert data.get("encrypted") is True
    encrypted_body = data["data"]

    decrypted_bytes = decrypt(
        {
            "ciphertext": base64.b64decode(encrypted_body["ciphertext"]),
            "iv": base64.b64decode(encrypted_body["iv"]),
        },
        base64.b64decode(encrypted_body["cipherkey"]),
        client_keys["private_key"],
    )

    assert decrypted_bytes is not None
    decrypted_data = json.loads(decrypted_bytes.decode("utf-8"))

    assert decrypted_data["choices"][0]["message"]["role"] == "assistant"
    assert "Mock response" in decrypted_data["choices"][0]["message"]["content"]


def test_v1_chat_completion_rejects_stream_flag(client, mock_llama):
    """API v1 chat completions should reject the stream flag with a clear error."""

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Count from 1 to 5"}
        ],
        "stream": True,
    }

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 400
    error = response.get_json()
    assert error["error"]["type"] == "invalid_request_error"
    assert error["error"].get("param") == "stream"
    assert "Streaming is not supported for API v1" in error["error"]["message"]


def test_v1_completions_reject_stream_flag(client, mock_llama):
    """Legacy completions endpoint should also reject streaming attempts."""

    payload = {
        "model": "llama-3-8b-instruct",
        "prompt": "Return a short response.",
        "stream": True,
    }

    response = client.post("/api/v1/completions", json=payload)

    assert response.status_code == 400
    error = response.get_json()
    assert error["error"]["type"] == "invalid_request_error"
    assert error["error"].get("param") == "stream"
    assert "Streaming is not supported for API v1" in error["error"]["message"]


def test_v1_chat_completion_uses_fixed_llama_3_1_8b_model(client, monkeypatch):
    """API v1 chat completions should execute using the canonical token.place model."""

    monkeypatch.setattr(
        "api.v1.routes.get_models_info",
        lambda: [{"id": "llama-3-8b-instruct"}],
    )
    monkeypatch.setattr("api.v1.routes.validate_chat_messages", lambda msgs: None)

    captured = {}

    class FakeProvider:
        def complete_chat(self, *, model_id, messages, options=None):
            captured["model_id"] = model_id
            captured["messages"] = messages
            captured["options"] = options
            return {
                "role": "assistant",
                "content": "Llama response",
            }

    monkeypatch.setattr(
        "api.v1.routes.get_api_v1_compute_provider",
        lambda: FakeProvider(),
    )
    monkeypatch.setattr(
        "api.v1.routes.get_api_v1_resolved_provider_path",
        lambda _provider: "local",
    )
    monkeypatch.setattr(
        "api.v1.routes.get_api_v1_last_backend_path",
        lambda: "local",
    )

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.is_json

    body = response.get_json()
    assert body["model"] == "llama-3-8b-instruct"
    assert captured["model_id"] == "llama-3-8b-instruct"
    assert captured["messages"] == payload["messages"]
    assert body["choices"][0]["message"]["content"] == "Llama response"


def test_v1_chat_completion_rejects_unsupported_gpt_model_ids(client, monkeypatch):
    """API v1 should reject unsupported GPT-branded model IDs."""

    monkeypatch.setattr(
        "api.v1.routes.get_models_info",
        lambda: [{"id": "llama-3-8b-instruct"}],
    )

    class ProviderShouldNotBeCalled:
        def complete_chat(self, **kwargs):
            raise AssertionError("provider should not be called for unsupported GPT model IDs")

    monkeypatch.setattr(
        "api.v1.routes.get_api_v1_compute_provider",
        lambda: ProviderShouldNotBeCalled(),
    )

    for gpt_model in ("gpt-4",):
        response = client.post(
            "/api/v1/chat/completions",
            json={
                "model": gpt_model,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"].get("param") == "model"


def test_streaming_chat_completion(client, mock_llama):
    """Streaming chat completions should return Server-Sent Events chunks."""

    payload = {
        "model": "llama-3-8b-instruct",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Count from 1 to 5"}
        ],
        "stream": True
    }

    response = client.post("/api/v2/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    events = []
    for raw_chunk in response.iter_encoded():
        text = raw_chunk.decode("utf-8")
        if not text.strip():
            continue
        assert text.startswith("data: ")
        events.append(text[len("data: "):].strip())

    assert events[-1] == "[DONE]"

    role_event = json.loads(events[0])
    content_event = json.loads(events[1])
    stop_event = json.loads(events[2])

    assert role_event["choices"][0]["delta"] == {"role": "assistant"}
    assert "Mock response" in content_event["choices"][0]["delta"]["content"]
    assert stop_event["choices"][0]["finish_reason"] == "stop"


def test_v1_encrypted_chat_completion_rejects_stream_flag(client, client_keys, mock_llama):
    """Encrypted chat completions should also reject the stream flag in API v1."""

    response = client.get("/api/v2/public-key")
    assert response.status_code == 200
    server_public_key = response.get_json()['public_key']

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say hello."}
    ]

    server_public_key_bytes = base64.b64decode(server_public_key)
    ciphertext_dict, cipherkey, iv = encrypt(json.dumps(messages).encode('utf-8'), server_public_key_bytes)

    payload = {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "stream": True,
        "client_public_key": client_keys['public_key_b64'],
        "messages": {
            "ciphertext": base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
            "cipherkey": base64.b64encode(cipherkey).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8')
        }
    }

    response = client.post("/api/v1/chat/completions", json=payload)

    assert response.status_code == 400
    error = response.get_json()
    assert error["error"]["type"] == "invalid_request_error"
    assert error["error"].get("param") == "stream"
    assert "Streaming is not supported for API v1" in error["error"]["message"]

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
    monkeypatch.setattr(
        'api.v1.compute_provider.generate_response',
        lambda m, msgs, **kwargs: msgs + [{'role':'assistant','content':'ok'}],
    )
    monkeypatch.setattr('api.v1.routes.encryption_manager.encrypt_message', lambda data, key: {'ciphertext':'a','cipherkey':'b','iv':'c'})
    payload = {'model':'llama-3-8b-instruct','prompt':'hi','encrypted':True,'client_public_key':'x'}
    res = client.post('/api/v1/completions', json=payload)
    assert res.status_code == 200
    d = res.get_json()
    assert d['encrypted'] is True


def test_create_chat_completion_model_error(client, monkeypatch, mock_llama):
    from api.v1.models import ModelError
    monkeypatch.setattr(
        'api.v1.compute_provider.generate_response',
        lambda m, msgs, **kwargs: (_ for _ in ()).throw(
            ModelError('boom', status_code=404, error_type='model_not_found')
        ),
    )
    payload = {'model':'llama-3-8b-instruct','messages':[{'role':'user','content':'hi'}]}
    res = client.post('/api/v1/chat/completions', json=payload)
    assert res.status_code == 404
    body = res.get_json()
    assert 'error' in body
    assert body['error']['type'] == 'model_not_found'


def test_create_completion_exception(client, monkeypatch, mock_llama):
    monkeypatch.setattr(
        'api.v1.compute_provider.generate_response',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('oops')),
    )
    payload = {'model':'llama-3-8b-instruct','prompt':'hi'}
    res = client.post('/api/v1/completions', json=payload)
    assert res.status_code == 400
    assert 'error' in res.get_json()


def test_format_error_response_function():
    with app.app_context():
        resp = format_error_response(
            "bad request", code="oops", param="field", status_code=418
        )
        data = resp.get_json()
        assert resp.status_code == 418
        assert data["error"]["message"] == "bad request"
        assert data["error"]["code"] == "oops"
        assert data["error"]["param"] == "field"


def test_openai_alias_routes(client):
    alias_resp = client.get("/v1/models")
    api_resp = client.get("/api/v1/models")
    assert alias_resp.status_code == 200
    assert alias_resp.get_json() == api_resp.get_json()

def test_chat_completion_invalid_body(client):
    res = client.post('/api/v1/chat/completions', json={})
    assert res.status_code == 400
    data = res.get_json()
    assert data['error']['message'].startswith('Invalid request body')


def test_chat_completion_decrypt_failure(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.encryption_manager.decrypt_message', lambda *a, **k: None)
    payload = {
        'model': 'llama-3-8b-instruct',
        'encrypted': True,
        'client_public_key': base64.b64encode(b'x').decode(),
        'messages': {'ciphertext': base64.b64encode(b'c').decode(), 'cipherkey': base64.b64encode(b'k').decode(), 'iv': base64.b64encode(b'i').decode()}
    }
    res = client.post('/api/v1/chat/completions', json=payload)
    assert res.status_code == 400
    assert 'Failed to decrypt messages' in res.get_json()['error']['message']


def test_chat_completion_json_error(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.encryption_manager.decrypt_message', lambda *a, **k: b'not-json')
    payload = {
        'model': 'llama-3-8b-instruct',
        'encrypted': True,
        'client_public_key': base64.b64encode(b'x').decode(),
        'messages': {'ciphertext': base64.b64encode(b'c').decode(), 'cipherkey': base64.b64encode(b'k').decode(), 'iv': base64.b64encode(b'i').decode()}
    }
    res = client.post('/api/v1/chat/completions', json=payload)
    assert res.status_code == 400
    assert 'Failed to parse JSON' in res.get_json()['error']['message']

def test_chat_completion_missing_messages(client):
    payload = {"model": "llama-3-8b-instruct"}
    res = client.post("/api/v1/chat/completions", json=payload)
    assert res.status_code == 400
    data = res.get_json()
    assert 'messages' in data['error']['message']


def test_chat_completion_messages_wrong_type(client):
    payload = {"model": "llama-3-8b-instruct", "messages": "not-a-list"}
    res = client.post("/api/v1/chat/completions", json=payload)
    assert res.status_code == 400
    assert 'Invalid type for messages' in res.get_json()['error']['message']


def test_chat_completion_invalid_role(client):
    payload = {"model": "llama-3-8b-instruct", "messages": [{"role": "bad", "content": "hi"}]}
    res = client.post("/api/v1/chat/completions", json=payload)
    assert res.status_code == 400
    assert 'Invalid role' in res.get_json()['error']['message']


def test_chat_completion_encrypt_failure_on_response(client, monkeypatch):
    monkeypatch.setattr(
        'api.v1.compute_provider.generate_response',
        lambda m, msgs, **kwargs: msgs + [{'role': 'assistant', 'content': 'ok'}],
    )
    monkeypatch.setattr('api.v1.routes.encryption_manager.encrypt_message', lambda *a, **k: None)
    monkeypatch.setattr('api.v1.routes.encryption_manager.decrypt_message', lambda *a, **k: b'[{"role":"user","content":"hi"}]')
    monkeypatch.setattr('api.v1.validation.validate_encrypted_request', lambda data: None)
    payload = {
        'model': 'llama-3-8b-instruct',
        'encrypted': True,
        'client_public_key': base64.b64encode(b'x').decode(),
        'messages': {
            'ciphertext': base64.b64encode(b'c').decode(),
            'cipherkey': base64.b64encode(b'k').decode(),
            'iv': base64.b64encode(b'i').decode()
        }
    }
    res = client.post('/api/v1/chat/completions', json=payload)
    assert res.status_code == 500
    assert 'Failed to encrypt response' in res.get_json()['error']['message']


def test_openai_alias_routes_extended(client):
    endpoints = [
        ('/v1/public-key', '/api/v1/public-key'),
        ('/v1/health', '/api/v1/health'),
        ('/v1/completions', '/api/v1/completions'),
        ('/v1/chat/completions', '/api/v1/chat/completions')
    ]
    for alias, api in endpoints:
        if 'completions' in alias:
            if alias.endswith('/completions'):
                payload = {'model': 'llama-3-8b-instruct', 'prompt': 'hi'}
            else:
                payload = {'model': 'llama-3-8b-instruct', 'messages': [{'role': 'user', 'content': 'hi'}]}
            res_alias = client.post(alias, json=payload)
            res_api = client.post(api, json=payload)
        else:
            res_alias = client.get(alias)
            res_api = client.get(api)
        assert res_alias.status_code == res_api.status_code
        data_alias = res_alias.get_json()
        data_api = res_api.get_json()
        if isinstance(data_alias, dict) and 'id' in data_alias:
            data_alias['id'] = data_api.get('id')
            if 'created' in data_alias:
                data_alias['created'] = data_api.get('created')
        # If both responses contain a timestamp, allow slight drift
        if (
            isinstance(data_alias, dict)
            and 'timestamp' in data_alias
            and 'timestamp' in data_api
        ):
            assert data_alias['status'] == data_api['status']
            assert data_alias['version'] == data_api['version']
            assert abs(data_alias['timestamp'] - data_api['timestamp']) <= 2
        else:
            assert data_alias == data_api


def test_logging_prod_environment(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'prod')
    import importlib
    import api.v1.routes as routes
    importlib.reload(routes)
    assert routes.ENVIRONMENT == 'prod'
    assert routes.logger.handlers == []
    monkeypatch.setenv('ENVIRONMENT', 'dev')
    importlib.reload(routes)


def test_list_models_exception(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    resp = client.get('/api/v1/models')
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_get_model_exception(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    resp = client.get('/api/v1/models/foo')
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_get_public_key_exception(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.encryption_manager', None)
    resp = client.get('/api/v1/public-key')
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_chat_completion_validation_error(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'llama-3-8b-instruct'}])
    from api.v1.validation import ValidationError
    monkeypatch.setattr('api.v1.routes.validate_encrypted_request', lambda d: (_ for _ in ()).throw(ValidationError('bad', field='f', code='c')))
    payload = {
        'model': 'llama-3-8b-instruct',
        'encrypted': True,
        'client_public_key': base64.b64encode(b'x').decode(),
        'messages': {
            'ciphertext': base64.b64encode(b'c').decode(),
            'cipherkey': base64.b64encode(b'k').decode(),
            'iv': base64.b64encode(b'i').decode()
        }
    }
    resp = client.post('/api/v1/chat/completions', json=payload)
    assert resp.status_code == 400
    assert 'bad' in resp.get_json()['error']['message']


def test_chat_completion_unexpected_exception(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'x'}])
    monkeypatch.setattr('api.v1.routes.validate_chat_messages', lambda m: None)
    monkeypatch.setattr(
        'api.v1.compute_provider.generate_response',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fail')),
    )
    payload = {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]}
    resp = client.post('/api/v1/chat/completions', json=payload)
    assert resp.status_code == 500
    assert 'Internal server error' in resp.get_json()['error']['message']


def test_completions_invalid_body(client):
    resp = client.post('/api/v1/completions', json={})
    assert resp.status_code == 400
    assert 'Invalid request body' in resp.get_json()['error']['message']


def test_completions_missing_model(client):
    resp = client.post('/api/v1/completions', json={'prompt': 'hi'})
    assert resp.status_code == 400
    assert 'Missing required parameter' in resp.get_json()['error']['message']


def test_completions_model_error(client, monkeypatch):
    from api.v1.models import ModelError
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'foo'}])

    class ErrorProvider:
        def complete_chat(self, **kwargs):
            raise ModelError('no', status_code=404, error_type='model_not_found')

    monkeypatch.setattr('api.v1.routes.get_api_v1_compute_provider', lambda: ErrorProvider())
    monkeypatch.setattr('api.v1.routes.get_api_v1_resolved_provider_path', lambda _provider: 'local')
    resp = client.post('/api/v1/completions', json={'model': 'foo', 'prompt': 'hi'})
    assert resp.status_code == 404
    assert 'model_not_found' in resp.get_json()['error']['type']


def test_completions_generate_model_error(client, monkeypatch):
    from api.v1.models import ModelError
    monkeypatch.setattr('api.v1.routes.get_models_info', lambda: [{'id': 'foo'}])

    class ErrorProvider:
        def complete_chat(self, **kwargs):
            raise ModelError('bad', status_code=402)

    monkeypatch.setattr('api.v1.routes.get_api_v1_compute_provider', lambda: ErrorProvider())
    monkeypatch.setattr('api.v1.routes.get_api_v1_resolved_provider_path', lambda _provider: 'local')
    resp = client.post('/api/v1/completions', json={'model': 'foo', 'prompt': 'hi'})
    assert resp.status_code == 402
    assert 'bad' in resp.get_json()['error']['message']


def test_health_check_exception(client, monkeypatch):
    monkeypatch.setattr('api.v1.routes.time', None)
    resp = client.get('/api/v1/health')
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_alias_get_model(client, monkeypatch):
    """Alias endpoint should mirror the canonical model details endpoint."""
    fixed_time = 1234567890
    monkeypatch.setattr(time, "time", lambda: fixed_time)
    resp = client.get('/api/v1/models')
    model_id = resp.get_json()['data'][0]['id']
    resp_alias = client.get(f'/v1/models/{model_id}')
    resp_api = client.get(f'/api/v1/models/{model_id}')
    assert resp_alias.status_code == resp_api.status_code
    assert resp_alias.get_json() == resp_api.get_json()


def test_desktop_bridge_metadata_routes_to_same_origin_api_v1_relay_when_env_unset(
    monkeypatch, client, client_keys
):
    """Explicit desktop API v1 E2EE metadata may use loopback same-origin locally."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    import api.v1.routes as routes_module

    class EmptyConfig:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(routes_module, "get_config", lambda: EmptyConfig())

    captured = {}

    class FakeDistributedProvider:
        def complete_chat(self, *, model_id, messages, options=None):
            captured["model_id"] = model_id
            captured["messages"] = messages
            captured["options"] = options
            return {"role": "assistant", "content": "desktop bridge response"}

    def fake_get_provider_for_mode(**kwargs):
        captured["provider_kwargs"] = kwargs
        return FakeDistributedProvider()

    monkeypatch.setattr(
        routes_module,
        "get_api_v1_compute_provider_for_mode",
        fake_get_provider_for_mode,
    )
    monkeypatch.setattr(
        routes_module,
        "get_api_v1_resolved_provider_path",
        lambda _provider: "distributed",
    )
    monkeypatch.setattr(
        routes_module,
        "get_api_v1_last_backend_path",
        lambda: "distributed_relay_e2ee",
    )

    public_key = client.get("/api/v1/public-key").get_json()["public_key"]
    encrypted_messages = _encrypt_messages_for_server(
        [{"role": "user", "content": "hello desktop"}],
        public_key,
    )

    response = client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": client_keys["public_key_b64"],
            "messages": encrypted_messages,
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        },
        base_url="http://127.0.0.1:5010",
    )

    assert response.status_code == 200
    assert captured["provider_kwargs"] == {
        "mode": "distributed",
        "distributed_url": "http://127.0.0.1:5010",
        "distributed_fallback_enabled": False,
        "distributed_target_source": "request_loopback_origin",
        "distributed_relay_only": True,
    }
    assert captured["messages"] == [{"role": "user", "content": "hello desktop"}]
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"] == "distributed_relay_e2ee"


def test_desktop_bridge_staging_request_uses_relay_public_not_production_default(
    monkeypatch,
):
    """Staging public relay URL is safe for forced bridge when no internal target is set."""

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://staging.token.place")
    import api.v1.routes as routes_module

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "relay.server_url": "https://token.place/",
                "api.relay_url": "",
                "relay.server_pool": [],
            }
            return values.get(key, default)

    monkeypatch.setattr(routes_module, "get_config", lambda: FakeConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="https://staging.token.place",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        selection = routes_module._request_relay_target_selection()

    assert selection.url == "https://staging.token.place"
    assert selection.url != "https://token.place"
    assert selection.source == "relay_public_env:TOKENPLACE_RELAY_PUBLIC_URL"
    assert selection.relay_only is True


def test_desktop_bridge_request_prefers_internal_relay_env_over_public_url(
    monkeypatch,
):
    """Same-pod relay dispatch uses internal target when both internal and public URLs exist."""

    _clear_distributed_target_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKENPLACE_RELAY_INTERNAL_URL", "http://127.0.0.1:5010")
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://token.place")
    import api.v1.routes as routes_module

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="https://token.place",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        selection = routes_module._request_relay_target_selection()

    assert selection.url == "http://127.0.0.1:5010"
    assert selection.source == "relay_internal_env:TOKENPLACE_RELAY_INTERNAL_URL"
    assert selection.relay_only is True


def test_normalise_relay_origin_preserves_ipv6_loopback_brackets():
    """IPv6 literal origins must keep brackets when reconstructed with ports."""

    import api.v1.routes as routes_module

    assert (
        routes_module._normalise_relay_origin("http://[::1]:5010")
        == "http://[::1]:5010"
    )


def test_normalise_relay_origin_preserves_api_v1_base_paths():
    """Explicit API v1 relay bases remain trusted instead of being ignored."""

    import api.v1.routes as routes_module

    assert (
        routes_module._normalise_relay_origin("https://relay.example/api/v1/")
        == "https://relay.example/api/v1"
    )
    assert (
        routes_module._normalise_relay_origin("http://[::1]:5010/api/v1")
        == "http://[::1]:5010/api/v1"
    )
    assert routes_module._normalise_relay_origin("https://relay.example/api/v2") == ""


def test_desktop_bridge_relay_base_url_preserves_env_api_v1_base_over_loopback(
    monkeypatch,
):
    """Path-scoped explicit relay URLs must not fall back to same-origin routing."""

    monkeypatch.setenv(
        "TOKENPLACE_DISTRIBUTED_COMPUTE_URL",
        "https://relay.example/api/v1/",
    )
    import api.v1.routes as routes_module

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="http://127.0.0.1:5010",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert routes_module._request_relay_base_url() == "https://relay.example/api/v1"


def test_desktop_bridge_relay_base_url_preserves_config_api_v1_base_for_untrusted_host(
    monkeypatch,
):
    """Path-scoped trusted config relay URLs must not be ignored for forged hosts."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    import api.v1.routes as routes_module

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "relay.server_url": "https://relay.example/api/v1/",
                "api.relay_url": "",
                "relay.server_pool": [],
            }
            return values.get(key, default)

    monkeypatch.setattr(routes_module, "get_config", lambda: FakeConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="https://attacker.example",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        assert routes_module._request_relay_base_url() == "https://relay.example/api/v1"


def test_desktop_bridge_relay_base_url_allows_ipv6_loopback_same_origin_only_from_loopback(
    monkeypatch,
):
    """Bracketed IPv6 localhost fallback is limited to loopback clients."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    import api.v1.routes as routes_module
    from api.v1.compute_provider import ComputeProviderError

    class EmptyConfig:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(routes_module, "get_config", lambda: EmptyConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="http://[::1]:5010",
        environ_base={"REMOTE_ADDR": "::1"},
    ):
        assert routes_module._request_relay_base_url() == "http://[::1]:5010"

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="http://[::1]:5010",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        with pytest.raises(ComputeProviderError) as exc_info:
            routes_module._request_relay_base_url()

    assert exc_info.value.code == "untrusted_relay_origin"


def test_desktop_bridge_relay_base_url_uses_production_default_only_in_production(
    monkeypatch,
):
    """Production default token.place routing must not leak into staging/dev."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    import api.v1.routes as routes_module

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "relay.server_url": "https://token.place/",
                "api.relay_url": "",
                "relay.server_pool": ["https://token.place/"],
            }
            return values.get(key, default)

    monkeypatch.setattr(routes_module, "get_config", lambda: FakeConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="https://attacker.example",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        assert routes_module._request_relay_base_url() == "https://token.place"


def test_desktop_bridge_relay_base_url_rejects_production_default_in_staging(
    monkeypatch,
):
    """Staging desktop bridge routing must not silently use production token.place."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "staging")
    import api.v1.routes as routes_module
    from api.v1.compute_provider import ComputeProviderError

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "relay.server_url": "https://token.place/",
                "api.relay_url": "",
                "relay.server_pool": ["https://token.place/"],
            }
            return values.get(key, default)

    monkeypatch.setattr(routes_module, "get_config", lambda: FakeConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="https://staging.token.place",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        with pytest.raises(ComputeProviderError) as exc_info:
            routes_module._request_relay_base_url()

    assert exc_info.value.code == "untrusted_relay_origin"


def test_desktop_bridge_relay_base_url_prefers_loopback_host_over_default_config(
    monkeypatch,
):
    """Verified loopback desktop requests stay on their same-origin local relay."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    import api.v1.routes as routes_module

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "relay.server_url": "https://token.place/",
                "api.relay_url": "",
                "relay.server_pool": [],
            }
            return values.get(key, default)

    monkeypatch.setattr(routes_module, "get_config", lambda: FakeConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="http://127.0.0.1:5010",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert routes_module._request_relay_base_url() == "http://127.0.0.1:5010"


def test_desktop_bridge_relay_base_url_prefers_env_over_loopback_host(
    monkeypatch,
):
    """The explicit distributed relay URL remains highest precedence."""

    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "https://relay.example/")
    import api.v1.routes as routes_module

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="http://127.0.0.1:5010",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert routes_module._request_relay_base_url() == "https://relay.example"


def test_desktop_bridge_relay_base_url_fails_closed_without_trusted_origin(
    monkeypatch,
):
    """Desktop distributed override must fail closed when no trusted relay exists."""

    monkeypatch.delenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", raising=False)
    import api.v1.routes as routes_module
    from api.v1.compute_provider import ComputeProviderError

    class EmptyConfig:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(routes_module, "get_config", lambda: EmptyConfig())

    with app.test_request_context(
        "/api/v1/chat/completions",
        base_url="https://attacker.example",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    ):
        with pytest.raises(ComputeProviderError) as exc_info:
            routes_module._request_relay_base_url()

    assert exc_info.value.code == "untrusted_relay_origin"
    assert exc_info.value.status_code == 400
