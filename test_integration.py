import pytest
import subprocess
import time
import requests
from encrypt import generate_keys, encrypt, decrypt
from client import ChatClient
from relay import app as relay_app
from server import app as server_app
from flask import g

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

    # Start the server
    server_process = subprocess.Popen(["python", "server.py", "--server_port", str(server_port), "--relay_port", str(relay_port)])
    print("Launched server. Waiting for 10 seconds...")
    time.sleep(10)  # Give the server some time to start and download the model (if needed)

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