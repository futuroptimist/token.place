from flask import Flask, send_from_directory, request, jsonify, g
from datetime import datetime, timezone
import secrets
import argparse
import os
import sys
import threading
import time
import logging
import json
import signal
from typing import Any, Dict

from prometheus_client import Counter, REGISTRY


class JsonFormatter(logging.Formatter):
    """Format log records as structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "logger": record.name,
        }

        json_fields = getattr(record, "json_fields", None)
        if isinstance(json_fields, dict):
            base.update(json_fields)

        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(base, ensure_ascii=False)


def _configure_logging() -> None:
    """Configure application logging to emit structured JSON."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(logging.WARNING)
    werkzeug_logger.propagate = True


_configure_logging()

# Parse command line arguments early to set environment variables before imports
parser = argparse.ArgumentParser(description="token.place relay server")
parser.add_argument("--port", type=int, default=5010, help="Port to run the relay server on")
parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind the relay server")
parser.add_argument("--use_mock_llm", action="store_true", help="Use mock LLM for testing")

if __name__ == "__main__":  # pragma: no cover
    args = parser.parse_args()
else:
    args = parser.parse_args([])

# Set environment variable based on the command line argument or existing env
if args.use_mock_llm or os.environ.get("USE_MOCK_LLM") == "1":
    os.environ["USE_MOCK_LLM"] = "1"
    print("Running with USE_MOCK_LLM=1 (mock mode enabled)")

from api import init_app
from config import get_config


shutdown_event = threading.Event()


def _handle_sigterm(signum, _frame):
    logging.getLogger(__name__).info(
        "Received termination signal",
        extra={"json_fields": {"event": "shutdown", "signal": signum}},
    )
    shutdown_event.set()


signal.signal(signal.SIGTERM, _handle_sigterm)


_REQUEST_METRIC_NAME = "tokenplace_relay_requests_total"


def _get_request_counter() -> Counter:
    try:
        return Counter(
            "tokenplace_relay_requests_total",
            "Total HTTP requests processed by the relay",
            ("method", "endpoint", "status"),
        )
    except ValueError:
        existing = REGISTRY._names_to_collectors.get(_REQUEST_METRIC_NAME)
        if existing:
            return existing
        raise


REQUEST_COUNTER = _get_request_counter()


def _load_server_registration_token():
    """Return the configured relay server token, if any."""

    token = None
    try:
        token = get_config().get('relay.server_registration_token')
    except Exception:
        token = None

    if not token:
        token = os.environ.get('TOKEN_PLACE_RELAY_SERVER_TOKEN')

    if isinstance(token, str):
        token = token.strip()
        if not token:
            return None

    return token


SERVER_REGISTRATION_TOKEN = _load_server_registration_token()


def _validate_server_registration():
    """Ensure relay compute nodes present the expected token when configured."""

    if not SERVER_REGISTRATION_TOKEN:
        return None

    provided = request.headers.get('X-Relay-Server-Token', '')
    candidate = provided.strip()
    if candidate and secrets.compare_digest(candidate, SERVER_REGISTRATION_TOKEN):
        return None

    return jsonify({
        'error': {
            'message': 'Missing or invalid relay server token',
            'code': 401,
        }
    }), 401


app = Flask(__name__)
app.logger.handlers = []
app.logger.setLevel(logging.INFO)
app.logger.propagate = True
app.config['UPSTREAM_URL'] = os.environ.get('UPSTREAM_URL', 'http://gpu-server:8000')
app.config['GPU_SERVER_HOST'] = os.environ.get('GPU_SERVER_HOST', 'gpu-server')

logging.getLogger(__name__).info(
    'Relay configuration loaded',
    extra={
        'json_fields': {
            'event': 'config',
            'upstream_url': app.config['UPSTREAM_URL'],
            'gpu_server_host': app.config['GPU_SERVER_HOST'],
        }
    },
)

# Initialize the API
init_app(app)

known_servers = {}
client_inference_requests = {}
client_responses = {}
streaming_sessions = {}
streaming_sessions_by_client = {}
stream_lock = threading.Lock()


@app.before_request
def _track_request_start():
    g.request_start_time = time.time()


@app.after_request
def _log_request(response):
    duration_ms = None
    start_time = getattr(g, "request_start_time", None)
    if start_time is not None:
        duration_ms = round((time.time() - start_time) * 1000, 2)

    endpoint = request.endpoint or request.path
    REQUEST_COUNTER.labels(request.method, endpoint, str(response.status_code)).inc()

    logging.getLogger("relay.http").info(
        "request",
        extra={
            "json_fields": {
                "event": "http_request",
                "method": request.method,
                "path": request.path,
                "endpoint": endpoint,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
            }
        },
    )
    return response


@app.route('/livez', methods=['GET'])
def livez():
    """Report basic liveness for Kubernetes probes."""

    status = {
        'status': 'ok' if not shutdown_event.is_set() else 'shutting_down',
    }
    http_status = 200 if not shutdown_event.is_set() else 503
    return jsonify(status), http_status


@app.route('/healthz', methods=['GET'])
def healthz():
    """Report readiness for Kubernetes probes."""

    if shutdown_event.is_set():
        return jsonify({'status': 'shutting_down'}), 503

    try:
        cfg = get_config()
        config_loaded = bool(cfg)
    except Exception as exc:  # pragma: no cover - defensive readiness logging
        logging.getLogger(__name__).warning(
            "Configuration unavailable during readiness check",
            extra={'json_fields': {'event': 'readiness', 'error': str(exc)}},
        )
        config_loaded = False

    status = {'status': 'ok' if config_loaded else 'config_unavailable'}
    return jsonify(status), 200 if config_loaded else 503


def _register_stream_session(server_public_key, client_public_key):
    """Create or replace the streaming session for a client/server pair."""

    if not client_public_key:
        return None

    session_id = secrets.token_urlsafe(16)
    now = time.time()
    session = {
        'session_id': session_id,
        'server_public_key': server_public_key,
        'client_public_key': client_public_key,
        'chunks': [],
        'status': 'open',
        'created_at': now,
        'updated_at': now,
    }

    with stream_lock:
        existing_session_id = streaming_sessions_by_client.get(client_public_key)
        if existing_session_id:
            streaming_sessions.pop(existing_session_id, None)
        streaming_sessions[session_id] = session
        streaming_sessions_by_client[client_public_key] = session_id

    return session


def _append_stream_chunk(session_id, chunk, final=False):
    """Append a streaming chunk to the active session."""

    with stream_lock:
        session = streaming_sessions.get(session_id)
        if not session:
            return False

        session['chunks'].append(chunk)
        session['updated_at'] = time.time()
        if final:
            session['status'] = 'closed'

    return True


def _pop_stream_chunks_for_client(client_public_key):
    """Retrieve queued streaming chunks for a client."""

    with stream_lock:
        session_id = streaming_sessions_by_client.get(client_public_key)
        if not session_id:
            return None

        session = streaming_sessions.get(session_id)
        if not session:
            streaming_sessions_by_client.pop(client_public_key, None)
            return None

        chunks = list(session['chunks'])
        session['chunks'].clear()
        session['updated_at'] = time.time()
        final = session['status'] == 'closed'

        if final:
            streaming_sessions.pop(session_id, None)
            streaming_sessions_by_client.pop(client_public_key, None)

    return session_id, chunks, final

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

    # Select a server randomly using cryptographically secure randomness
    server_public_key = secrets.choice(list(known_servers.keys()))
    return jsonify({
        'server_public_key': known_servers[server_public_key]['public_key']
    })

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
    stream_requested = bool(data.get('stream', False))
    client_public_key = data.get('client_public_key', None)

    if stream_requested and not client_public_key:
        return jsonify({
            'error': {
                'message': 'Streaming requests require a client public key',
                'code': 400,
            }
        }), 400

    # Check if the server with the specified public key is known
    if server_public_key not in known_servers:
        return jsonify({'error': 'Server with the specified public key not found'}), 404

    # Append the client's request to the list of requests for the server
    if server_public_key not in client_inference_requests:
        client_inference_requests[server_public_key] = []
    client_inference_requests[server_public_key].append({
        'chat_history': chat_history_ciphertext,
        'client_public_key': client_public_key,
        'cipherkey': cipherkey,
        'iv': iv,  # Include the IV in the saved client's request
        'stream': stream_requested,
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
    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json()
    public_key = data.get('server_public_key', None)

    raw_batch_size = data.get('max_batch_size') if isinstance(data, dict) else None
    max_batch_size = 1
    if raw_batch_size is not None:
        try:
            max_batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid max_batch_size'}), 400
        if max_batch_size < 1:
            return jsonify({'error': 'Invalid max_batch_size'}), 400

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
    queued_requests = client_inference_requests.get(public_key, [])
    if queued_requests:
        batch = []
        while queued_requests and len(batch) < max_batch_size:
            request_payload = queued_requests.pop(0)
            if request_payload.get('stream'):
                session = _register_stream_session(
                    public_key,
                    request_payload.get('client_public_key'),
                )
                if session is not None:
                    request_payload['stream_session_id'] = session['session_id']
            batch.append(request_payload)

        first_request = batch[0]
        response_data.update({
            'client_public_key': first_request['client_public_key'],
            'chat_history': first_request['chat_history'],
            'cipherkey': first_request['cipherkey'],
            'iv': first_request.get('iv', ''),
        })

        if first_request.get('stream') and first_request.get('stream_session_id'):
            response_data['stream'] = True
            response_data['stream_session_id'] = first_request['stream_session_id']

        if max_batch_size > 1:
            response_data['batch'] = batch

    return jsonify(response_data)

@app.route('/source', methods=['POST'])
def source():
    """
    Receives encrypted responses from the server and queues them for the client to retrieve.
    """
    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

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


@app.route('/stream/source', methods=['POST'])
def stream_source():
    """Accept streaming chunks emitted by compute nodes."""

    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json()
    if not data or 'session_id' not in data or 'chunk' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    session_id = data['session_id']
    chunk = data['chunk']
    final = bool(data.get('final', False))

    if not _append_stream_chunk(session_id, chunk, final=final):
        return jsonify({'error': 'Unknown stream session'}), 404

    return jsonify({'message': 'Chunk stored', 'final': final}), 200


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


@app.route('/stream/retrieve', methods=['POST'])
def stream_retrieve():
    """Return queued streaming chunks for the requesting client."""

    data = request.get_json()
    if not data or 'client_public_key' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    client_public_key = data['client_public_key']
    popped = _pop_stream_chunks_for_client(client_public_key)
    if popped is None:
        return jsonify({'error': 'No active stream for the given public key'}), 200

    session_id, chunks, final = popped
    response_payload = {
        'stream': True,
        'session_id': session_id,
        'chunks': chunks,
    }
    if final:
        response_payload['final'] = True

    return jsonify(response_payload), 200


def create_app():
    """Return the Flask application for WSGI servers."""

    return app


if __name__ == '__main__':  # pragma: no cover
    port_value = os.environ.get('RELAY_PORT', str(args.port))
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            'Invalid RELAY_PORT provided; falling back to default',
            extra={'json_fields': {'event': 'startup', 'port': port_value}},
        )
        port = args.port

    logging.getLogger(__name__).info(
        'Starting relay in standalone mode',
        extra={
            'json_fields': {
                'event': 'startup',
                'host': args.host,
                'port': port,
                'use_mock_llm': os.environ.get('USE_MOCK_LLM') == '1',
            }
        },
    )
    app.run(host=args.host, port=port)
