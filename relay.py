from flask import Flask, send_from_directory, request, jsonify
import requests
from datetime import datetime

app = Flask(__name__)

# Changing known_servers to a dict to uniquely identify servers by their public keys
known_servers = {}
client_queue = []
next_server_index = 0  # This will no longer be needed but kept for reference
client_inference_requests = {}

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
    This allows the relay to load-balance requests across heterogeneous servers and clients.

    Returns: a json response with the following keys:
        - server_public_key: the RSA-2048 public key of the next server to send a request to
        - error: an error message with a message and a code
    """
    global next_server_index
    if not known_servers:
        return jsonify({
            'error': {
                'message': 'No servers available',
                'code': 503
            }
        })

    server_keys = list(known_servers.keys())
    return_index = next_server_index
    next_server_index = (next_server_index + 1) % len(server_keys)
    server_public_key = server_keys[return_index]
    return jsonify({
        'server_public_key': known_servers[server_public_key]['public_key']
    })

# deprecated; use request_inference instead
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
    """
    # Parse the request data
    data = request.get_json()
    if not data or 'server_public_key' not in data or 'chat_history' not in data:
        return jsonify({
            'error': {
                'message': 'Invalid request data',
                'code': 400
            }
        }), 400

    server_public_key = data['server_public_key']
    chat_history_ciphertext = data['chat_history']

    # Check if the server with the specified public key is known
    if server_public_key not in known_servers:
        return jsonify({'error': 'Server with the specified public key not found'}), 404
    
    # Save the client's requests to client_inference_requests
    client_inference_requests[server_public_key] = {
        'chat_history': chat_history_ciphertext,
        'client_public_key': data.get('client_public_key', None)
    }
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
    if public_key in client_inference_requests:
        request_data = client_inference_requests.pop(public_key)
        response_data.update({
            'client_public_key': request_data['client_public_key'],
            'chat_history': request_data['chat_history']
        })

    return jsonify(response_data)
 
if __name__ == '__main__':
    app.run(port=5000)  # Flask app runs on port 5000 internally
