import pytest
from server import app, _private_key, _public_key, public_key_from_pem, encrypt_message, decrypt_message
import json
import base64

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_home_endpoint(client):
    """Test that the home endpoint accepts POST requests and returns a valid response."""
    response = client.post('/', json={'chat_history': []})
    assert response.status_code == 200
    assert isinstance(response.json, list)  # Assuming the response should be a list

def test_encryption_decryption():
    """Test encryption and decryption functions."""
    message = "This is a test message."
    # Directly use the PEM string without re-encoding it
    public_key = public_key_from_pem(_public_key.save_pkcs1())
    encrypted_message = encrypt_message(message.encode('utf-8'), public_key)
    # Decrypt the message directly without base64 decoding it
    decrypted_message = decrypt_message(encrypted_message, _private_key)
    # Since decrypt_message already decodes the message to utf-8, no need to decode again
    assert message == decrypted_message

def test_model_integration(client):
    """Test the model integration by sending a simulated chat history."""
    # This assumes your model and endpoint are set up to handle this structure.
    chat_history = [{"role": "user", "content": "Hello, how are you?"}]
    response = client.post('/', json={'chat_history': chat_history})
    assert response.status_code == 200
    # Validate the structure of the response here, e.g., it should include the assistant's response.

if __name__ == '__main__':
    pytest.main()
