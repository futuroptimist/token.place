import pytest
import subprocess
import time
import requests
from client import ChatClient
import base64
import json
from encrypt import encrypt, decrypt, generate_keys
import os # Import os

@pytest.fixture(scope="session")
def setup_servers():
    # use new port numbers to avoid conflicts with running servers/relays locally (which use 5000 and 3000, respectively)
    relay_port = 5001
    server_port = 3001

    # Create a ChatClient instance with the test relay port
    client = ChatClient('http://localhost', relay_port)

    # Start the relay server
    relay_process = subprocess.Popen(["python", "relay.py", "--port", str(relay_port)])
    print("Launched relay server. Waiting for 5 seconds...")
    time.sleep(5)  # Give the relay server some time to start

    # Start the server with the mock LLM environment variable
    server_env = os.environ.copy() # Get a copy of the current environment
    server_env["USE_MOCK_LLM"] = "1" # Set the variable
    server_process = subprocess.Popen(
        ["python", "server.py", "--server_port", str(server_port), "--relay_port", str(relay_port)],
        env=server_env # Pass the modified environment
    )
    print("Launched server with USE_MOCK_LLM=1. Waiting for 5 seconds...") # Reduced wait time as no model download needed
    time.sleep(5)  # Reduced wait time

    # --- Wait for server to register with relay --- 
    print("Waiting for server to register with relay...")
    base_relay_url = f'http://localhost:{relay_port}'
    max_wait_time = 30 # seconds
    start_wait_time = time.time()
    server_registered = False
    while time.time() - start_wait_time < max_wait_time:
        try:
            response = requests.get(f'{base_relay_url}/next_server')
            if response.status_code == 200 and 'server_public_key' in response.json():
                print("Server registered successfully.")
                server_registered = True
                break
            else:
                print(f"Waiting... (Status: {response.status_code}, JSON: {response.text})")
        except requests.exceptions.ConnectionError:
            print("Waiting... (Relay not responding yet)")
        except Exception as e:
            print(f"Waiting... (Error checking registration: {e})")
        time.sleep(1) # Wait 1 second before retrying

    if not server_registered:
        # If the server doesn't register, terminate processes and raise error
        print("Error: Server failed to register with relay within timeout.")
        relay_process.terminate()
        server_process.terminate()
        raise TimeoutError("Server did not register with relay.")
    # --- End wait for server --- 

    yield client, relay_port, server_port, relay_process, server_process

@pytest.fixture(scope="function")
def client(setup_servers):
    client, _, _, _, _ = setup_servers
    return client

@pytest.fixture(scope="session", autouse=True)
def teardown_servers(setup_servers):
    yield
    _, _, _, relay_process, server_process = setup_servers
    # Stop the servers after all tests are done
    relay_process.terminate()
    server_process.terminate()

def test_send_message(client):
    # Send a message and check the response
    response = client.send_message("Hello, how are you?")
    assert response is not None
    assert len(response) == 2
    assert response[0]['role'] == 'user'
    assert response[0]['content'] == 'Hello, how are you?'
    assert response[1]['role'] == 'assistant'

def test_send_another_message(client):
    # Send another message and check the response
    response = client.send_message("What is the capital of France?")
    assert response is not None
    assert len(response) == 4
    assert response[2]['role'] == 'user'
    assert response[2]['content'] == 'What is the capital of France?'
    assert response[3]['role'] == 'assistant'
    assert 'Paris' in response[3]['content']

def test_root_endpoint_returns_html(setup_servers):
    # Assuming your relay server is running on port 5001 as set up in your fixture
    base_url = 'http://localhost:5001/'
    response = requests.get(base_url)

    # Check if the response status code is 200
    assert response.status_code == 200, "Expected a 200 OK response."

    # Check if the 'Content-Type' header is 'text/html'
    assert 'text/html' in response.headers['Content-Type'], "Expected HTML content to be returned."

def test_faucet_endpoint(setup_servers):
    _, relay_port, _, _, _ = setup_servers

    # Generate a new key pair for the test
    private_key, public_key = generate_keys()
    public_key_b64 = base64.b64encode(public_key).decode('utf-8')

    # Fetch the server's public key from the /next_server endpoint
    base_url = f'http://localhost:{relay_port}'
    response = requests.get(f'{base_url}/next_server')
    assert response.status_code == 200, "Expected a 200 OK response from /next_server endpoint."
    server_public_key_b64 = response.json()['server_public_key']
    server_public_key = base64.b64decode(server_public_key_b64)

    # Prepare the chat history with the "hello" message
    chat_history = [{"role": "user", "content": "hello"}]

    # Encrypt the chat history
    ciphertext_dict, cipherkey, iv = encrypt(json.dumps(chat_history).encode('utf-8'), server_public_key)
    encrypted_chat_history_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
    iv_b64 = base64.b64encode(iv).decode('utf-8')
    encrypted_cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')

    # Prepare the request payload
    payload = {
        "client_public_key": public_key_b64,
        "server_public_key": server_public_key_b64,
        "chat_history": encrypted_chat_history_b64,
        "cipherkey": encrypted_cipherkey_b64,
        "iv": iv_b64,
    }

    # Send the request to the /faucet endpoint
    response = requests.post(f'{base_url}/faucet', json=payload)

    # Check if the response status code is 200
    assert response.status_code == 200, "Expected a 200 OK response from /faucet endpoint."

    # Check if the response contains the expected message
    assert response.json() == {'message': 'Request received'}, "Expected 'Request received' message."

    # Poll the /retrieve endpoint for a response
    start_time = time.time()
    timeout = 60  # Timeout in seconds
    while True:
        response = requests.post(f'{base_url}/retrieve', json={"client_public_key": public_key_b64})
        if response.status_code == 200:
            data = response.json()
            if 'chat_history' in data and 'iv' in data and 'cipherkey' in data:
                encrypted_chat_history_b64 = data['chat_history']
                encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                iv = base64.b64decode(data['iv'])
                cipherkey = base64.b64decode(data['cipherkey'])
                decrypted_chat_history = decrypt({'ciphertext': encrypted_chat_history, 'iv': iv}, cipherkey, private_key)
                
                if decrypted_chat_history is not None:
                    decrypted_response = json.loads(decrypted_chat_history.decode('utf-8'))
                    assert len(decrypted_response) == 2
                    assert decrypted_response[0]['role'] == 'user'
                    assert decrypted_response[0]['content'] == 'hello'
                    assert decrypted_response[1]['role'] == 'assistant'
                    return  # Exit the loop if the response is successfully decrypted
        
        elapsed_time = time.time() - start_time
        if elapsed_time > timeout:
            raise AssertionError("Timeout while waiting for response.")
        
        time.sleep(2)  # Wait for 2 seconds before the next attempt