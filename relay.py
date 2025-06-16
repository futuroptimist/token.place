from flask import Flask, send_from_directory, request, jsonify
import requests
from datetime import datetime
import random
import argparse
import os
import sys
import threading
import time

# Parse command line arguments early to set environment variables before imports
parser = argparse.ArgumentParser(description="token.place relay server")
parser.add_argument("--port", type=int, default=5010, help="Port to run the relay server on")
parser.add_argument("--use_mock_llm", action="store_true", help="Use mock LLM for testing")

if __name__ == "__main__":
    args = parser.parse_args()
else:
    args = parser.parse_args([])

# Set environment variable based on the command line argument or existing env
if args.use_mock_llm or os.environ.get("USE_MOCK_LLM") == "1":
    os.environ["USE_MOCK_LLM"] = "1"
    print("Running with USE_MOCK_LLM=1 (mock mode enabled)")

from api import init_app

# Import configuration
try:
    from config import RELAY_PORT
except ImportError:
    RELAY_PORT = 5010

app = Flask(__name__)

# Initialize the API
init_app(app)

known_servers = {}
client_queue = []
client_inference_requests = {}
client_responses = {}

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# Generic route for serving static files
@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

@app.route('/next_server', methods=['GET'])
def next_server():
    """
    Endpoint for clients to get the next server to send a request to.
    This allows the relay to load-balance requests across heterogeneous servers and clients in a random manner.

    Returns: a json response with the following keys:
        - server_public_key: the RSA-2048 public key of the selected server to send a request to
        - error: an error message with a message and a code
    """
    if not known_servers:
        return jsonify({
            'error': {
                'message': 'No servers available',
                'code': 503
            }
        })

    # Select a server randomly
    server_public_key = random.choice(list(known_servers.keys()))
    return jsonify({
        'server_public_key': known_servers[server_public_key]['public_key']
    })

# deprecated; use /faucet instead
@app.route('/inference', methods=['POST'])
def inference():
    # Get JSON data from the incoming request
    data = request.get_json()

    # Define the URL to which we will forward the request
    url = 'http://localhost:3000/'

    # Forward the POST request to the other service and get the response
    response = requests.post(url, json=data)

    # Return the response received from the other service to the client
    return jsonify(response.json()), response.status_code

@app.route('/faucet', methods=['POST'])
def faucet():
    """
    Endpoint for clients to request inference given a public key.
    The public key uniquely identifies the server to send the request to,
    mitigating the need to expose server URLs and force the servers to expose certain ports (as these
    servers interact in a RESTful way to maximize ease of use on consumer devices).

    The request body should be application/json and contain the following keys:
        - client_public_key: a unique identifier for the client (RSA-2048 public key) requesting inference
        - server_public_key: a unique identifier for the server (RSA-2048 public key) from which to request
            inference
        - chat_history: a string of ciphertext, encrypted with the server_public_key, which when decrypted,
            conforms to a list of objects with the the following JSON format:
            - role: the role of the message sender (either 'user' or 'assistant')
            - content: the message content
        - cipherkey: the AES key used to encrypt the chat_history, encrypted with the server_public_key
        - iv: the initialization vector used in the AES encryption of the chat_history

    Returns: a json response with the following keys:
        - chat_history: a string of ciphertext encrypted with the client_public_key, which when decrypted,
            conforms to a list of objects with the the following JSON format:
            - role: the role of the message sender (either 'user' or 'assistant')
            - content: the message content

    The request can either start a conversation with a single user message, or continue a conversation
    with a list of chat messages between the user and the assistant. Clients and servers may implement
    diffing mechanisms to determine if conversations were altered, but this is intentionally built in a
    stateless manner to minimize complexity and maximize scalability. It's trivially easy to identify
    tampering from either party, so enforcing it on the relay is a non-goal, especially since communication
    between the client and the server is intended to be end-to-end-encrypted. Servers and clients can choose
    how to handle this as they see fit.

    Example request:

    {
        "client_public_key": "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQklqQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FROEFNSUlCQ2dLQ0FRRUF3ZFpidTljcGwvclk4dFVrM3BoQwoxNTVnRm02OTRJOTd5YUJURkZSZ25PQjhlbXlZWWJCbDdlTFNTcVJOUTg2cDQzK1hldXdYcHpTcnc4SXJRdTZaCjA2cWJ0SlJmcy93bC84Y1BJZzVWdWtVRjBPSEJ2MFFnRkxwdFBSZUVUOXlKMFNEbUcxQlhwazJieXE2YUI3bG4KbFBNSytZb1VxQ0dLSzVRMXlHVFUzNC9YOHE0Q1VlYWJjL0RVRFRsNEUxdlkwK3EzaTZIMEZrd1Z3TGQ1bWpoegpzeHlqNjZxRU5kblF5RkEyVTZlU0tORHhaOGdLMC84YzVHbGhDV3ZTUmF1ZE10R2ZVNkZTTzJoSmMyb0NKYW5vCmtFNWNGeEFLQjY1eHRWRXdJYUY1UTVYUm0zajg5Ym1tWGFSYzBjcGZlMFhJYW9qQ3YvdTcxWi9wRjU4clJKOGsKQndJREFRQUIKLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg==",
        "server_public_key": "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQklqQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FROEFNSUlCQ2dLQ0FRRUF4M0VLUGkvNVNGc3JsaUZVQnMvagphcW8xY2RUKzl4cUNoZUt2bHl1dVpGNG5JVFVLbW5ZSmtUVE9GL3JNME9nMTM3b1d6RzhwOWdBREtOMWxoYWtVCjBwdkNZeVh3c3dEV3JMU0ZOTVc0d1B0cWpjaUxIbGhrQ3REQ3N3WjhMazd4NE1IOExHYTVTVzkzdHc4eWQrSGIKNTd0N1NaL0pneEtIZE5QUmh4Tjh2Q1pOOXQ4OWIxaklxaHZyNVBIZk9LSC9pc0hxWXIwdUxsVW9XaTdzenVpVApJZEcrS1UyNFFqQkxCK1RZaDdpVy9XTWF2VEhzRSt6dUxlVkJKSmdYTmZuNk15K3ZxaEJyY2RDeWZ2VG1vQVZ1CkRjckFkZ1NoQ1plL01GT3RRdDlEb3loNUx3ck03U3NmQVoxa0x1Rm43VjloNGw4V3JVOFdWaHdGYmE4TlNmUFMKalFJREFRQUIKLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg==",
        "chat_history": "v61G8y7z1WYGLnGJ27f+A0daaxNWexT9Tm6uN/yibmvZTuQQGuQPaoczVXigZayK",
        "cipherkey": "B+ewTtXyl0dezVTQ1gTXxASj4PqKKfqdfcBrSV5yyKQnIz8voK2+dFUnJx6EXxEIpyXZ/BymXCs9YLOJceCsaYyQCRvWYEzLWrKDJpGpkKWZNkpKigqsGBwD+qZlW7Vxjj91eqVunLBxpUTB3rcsw7zuuW/jTtWnRe8UW/y0c8ZDw8rYbIHmDs3IykNfThWhE2K0olMLkUTOhr6+yfRh4fb3WHvTdUtCzIrjOSwaA7OgpdlaqiZ/qbLsdfaSmNCKNh6AL4eJN0ifYq89ETeTA77IDyww2YIvJqWm4DdlgV4I14Ker5RCmdTBabPLJjFuXm7YaI57IfSsTAghLYX+Ww==",
        "iv": "yUR11oNkM/ZQeGuRF6JHAw=="
    }
    """
    # Parse the request data
    data = request.get_json()
    
    if not data or 'server_public_key' not in data or 'chat_history' not in data or 'cipherkey' not in data or 'iv' not in data:
        return jsonify({
            'error': {
                'message': 'Invalid request data',
                'code': 400
            }
        }), 400

    server_public_key = data['server_public_key']
    chat_history_ciphertext = data['chat_history']
    cipherkey = data['cipherkey']
    iv = data['iv']  # Extract the IV from the request data

    # Check if the server with the specified public key is known
    if server_public_key not in known_servers:
        return jsonify({'error': 'Server with the specified public key not found'}), 404
    
    # Append the client's request to the list of requests for the server
    if server_public_key not in client_inference_requests:
        client_inference_requests[server_public_key] = []
    client_inference_requests[server_public_key].append({
        'chat_history': chat_history_ciphertext,
        'client_public_key': data.get('client_public_key', None),
        'cipherkey': cipherkey,
        'iv': iv  # Include the IV in the saved client's request
    })
    return jsonify({'message': 'Request received'}), 200

@app.route('/sink', methods=['POST'])
def sink():
    """
    Endpoint for server instances to announce their availability (offering a compute sink).
    The request body should be application/json and contain the following keys:
        - server_public_key: a unique identifier for the server

    Returns: a json response with the following keys:
        - client_public_key: if present, chat_history will be present as well, and the client_public_key
            will be the public key used by the server when it returns the chat_history in ciphertext.
        - chat_history: a string of ciphertext, encrypted with the server's public key.
            Conforms to the same JSON format as the request body.
        - next_ping_in_x_seconds: the number of seconds after which the server should send the next ping
    """
    data = request.get_json()
    public_key = data.get('server_public_key', None)

    if public_key is None:
        return jsonify({'error': 'Invalid public key'}), 400
    
    # Update or add the server to known_servers
    if public_key in known_servers:
        known_servers[public_key]['last_ping'] = datetime.now()
    else:
        known_servers[public_key] = {
            'public_key': public_key,
            'last_ping': datetime.now(),
            'last_ping_duration': 10
        }

    response_data = {
        'next_ping_in_x_seconds': known_servers[public_key]['last_ping_duration']
    }

    # Check if there are any client requests for this server
    if public_key in client_inference_requests and client_inference_requests[public_key]:
        request_data = client_inference_requests[public_key].pop(0)
        response_data.update({
            'client_public_key': request_data['client_public_key'],
            'chat_history': request_data['chat_history'],
            'cipherkey': request_data['cipherkey'],
            'iv': request_data.get('iv', ''),
        })

    return jsonify(response_data)

@app.route('/source', methods=['POST'])
def source():
    """
    Receives encrypted responses from the server and queues them for the client to retrieve.
    """
    data = request.get_json()
    if not data or 'client_public_key' not in data or 'chat_history' not in data or 'cipherkey' not in data or 'iv' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    client_public_key = data['client_public_key']
    encrypted_chat_history = data['chat_history']
    encrypted_cipherkey = data['cipherkey']
    iv = data['iv']

    # Store the response in the client_responses dictionary
    client_responses[client_public_key] = {
        'chat_history': encrypted_chat_history,
        'cipherkey': encrypted_cipherkey,
        'iv': iv
    }
    return jsonify({'message': 'Response received and queued for client'}), 200

@app.route('/retrieve', methods=['POST'])
def retrieve():
    """
    Endpoint for clients to retrieve responses queued by the /source endpoint.
    """
    data = request.get_json()
    if not data or 'client_public_key' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    client_public_key = data['client_public_key']

    # Check if there's a response for the given client public key
    if client_public_key in client_responses:
        response_data = client_responses.pop(client_public_key)
        return jsonify(response_data), 200
    else:
        return jsonify({'error': 'No response available for the given public key'}), 200
 
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=args.port)
