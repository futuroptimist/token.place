import pytest
import subprocess
import time
import requests
import json
import base64
from encrypt import generate_keys, encrypt, decrypt

@pytest.fixture(scope="module")
def setup_servers():
    relay_port = 5001
    server_port = 3001

    # Start the relay server
    relay_process = subprocess.Popen(["python", "relay.py", "--port", str(relay_port)])
    time.sleep(2)  # Give the relay server some time to start

    # Start the server
    server_process = subprocess.Popen(["python", "server.py", "--server_port", str(server_port), "--relay_port", str(relay_port)])
    time.sleep(5)  # Give the server some time to start and download the model (if needed)

    yield relay_port, server_port

    # Stop the servers after the test is done
    relay_process.terminate()
    server_process.terminate()

def test_end_to_end(setup_servers):
    relay_port, server_port = setup_servers

    # Generate client RSA keys
    private_key, public_key = generate_keys()
    public_key_b64 = base64.b64encode(public_key).decode('utf-8')

    # Get the server's public key
    start_time = time.time()
    while True:
        response = requests.get(f'http://localhost:{relay_port}/next_server')
        if response.status_code == 200 and 'server_public_key' in response.json():
            server_public_key_b64 = response.json()['server_public_key']
            server_public_key = base64.b64decode(server_public_key_b64)
            break

        elapsed_time = time.time() - start_time
        assert elapsed_time < 60, "Timeout while waiting for server's public key"

        time.sleep(2)  # Wait for a short interval before trying again

    # Create a test chat history
    chat_history = [{"role": "user", "content": "Hello, how are you?"}]

    # Encrypt the chat history
    ciphertext, cipherkey = encrypt(json.dumps(chat_history).encode('utf-8'), server_public_key)
    encrypted_chat_history_b64 = base64.b64encode(ciphertext['ciphertext']).decode('utf-8')
    encrypted_cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')

    # Send the encrypted chat history to the faucet endpoint
    response = requests.post(f'http://localhost:{relay_port}/faucet', json={
        "client_public_key": public_key_b64,
        "server_public_key": server_public_key_b64,
        "chat_history": encrypted_chat_history_b64,
        "cipherkey": encrypted_cipherkey_b64
    })
    assert response.status_code == 200

    decrypted_chat_history = None
    start_time = time.time()
    while decrypted_chat_history is None:
        response = requests.post(f'http://localhost:{relay_port}/retrieve', json={"client_public_key": public_key_b64, "cipherkey": encrypted_cipherkey_b64})  
        assert response.status_code == 200
        data = response.json()
        if 'chat_history' in data and 'iv' in data:
            encrypted_chat_history_b64 = data['chat_history']
            encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
            iv = base64.b64decode(data['iv'])
            cipherkey = base64.b64decode(encrypted_cipherkey_b64)
            decrypted_chat_history = decrypt({'ciphertext': encrypted_chat_history, 'iv': iv}, cipherkey, private_key)
            decrypted_chat_history = json.loads(decrypted_chat_history.decode('utf-8'))
        
        elapsed_time = time.time() - start_time
        assert elapsed_time < 60, "Timeout while waiting for response"
        
        time.sleep(2)  # Wait for a short interval before trying again
        
        time.sleep(2)  # Wait for a short interval before trying again

    # Assert that the decrypted chat history contains the user's message and the assistant's response
    assert len(decrypted_chat_history) == 2
    assert decrypted_chat_history[0]['role'] == 'user'
    assert decrypted_chat_history[0]['content'] == 'Hello, how are you?'
    assert decrypted_chat_history[1]['role'] == 'assistant'