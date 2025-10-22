from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlparse

from flask import Flask, Response, g, jsonify, request, send_from_directory
from prometheus_client import Counter, REGISTRY
from werkzeug.serving import make_server

# Logging --------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class JsonFormatter(logging.Formatter):
    """Render log records as structured JSON."""

    _RESERVED = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 - logging API
        payload: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value

        return json.dumps(payload, default=_json_default)


def setup_logging() -> logging.Logger:
    """Configure application logging with JSON formatting."""

    logger = logging.getLogger("tokenplace.relay")
    if logger.handlers:
        return logger

    log_level = os.environ.get("TOKENPLACE_LOG_LEVEL", "INFO").upper()
    logger.setLevel(log_level)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.captureWarnings(True)

    return logger


LOGGER = setup_logging()


DRAINING = threading.Event()
_ORIGINAL_SIGNAL_HANDLERS: dict[int, Any] = {}


def _handle_shutdown_signal(signum: int, frame: Any) -> None:
    """Mark the process as draining and defer to the original handler."""

    if not DRAINING.is_set():
        LOGGER.info("relay.shutdown.signal", extra={"signal": signum})
        DRAINING.set()

    original = _ORIGINAL_SIGNAL_HANDLERS.get(signum)
    if callable(original) and original not in (signal.SIG_DFL, signal.SIG_IGN, _handle_shutdown_signal):
        original(signum, frame)
        return

    if original in (signal.SIG_DFL, None):
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _install_shutdown_handlers() -> None:
    """Install signal handlers that record draining state for readiness probes."""

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)
            _ORIGINAL_SIGNAL_HANDLERS[sig] = previous
            signal.signal(sig, _handle_shutdown_signal)
        except (OSError, RuntimeError, ValueError, AttributeError):
            LOGGER.debug("relay.signal_handler.install_failed", extra={"signal": sig})


_install_shutdown_handlers()


def configure_app_logging(flask_app: Flask) -> None:
    """Ensure Flask's logger shares the JSON formatter."""

    flask_app.logger.handlers = []
    for handler in LOGGER.handlers:
        flask_app.logger.addHandler(handler)
    flask_app.logger.setLevel(LOGGER.level)
    flask_app.logger.propagate = False


def _configure_mock_mode(enable_mock: bool) -> None:
    should_enable = enable_mock or os.environ.get("USE_MOCK_LLM") == "1"
    if not should_enable:
        return

    if os.environ.get("USE_MOCK_LLM") != "1":
        os.environ["USE_MOCK_LLM"] = "1"
        LOGGER.info("mock.llm.enabled", extra={"use_mock_llm": True})


def _build_cli_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="token.place relay server", add_help=add_help)
    parser.add_argument(
        "--port",
        type=int,
        default=5010,
        help="Port to run the relay server on",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind the relay server",
    )
    parser.add_argument(
        "--use_mock_llm",
        action="store_true",
        help="Use mock LLM for testing",
    )
    return parser


def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments when running the relay directly."""

    parser = _build_cli_parser()
    return parser.parse_args(argv)


def _detect_mock_flag(argv: list[str]) -> bool:
    parser = _build_cli_parser(add_help=False)
    try:
        args, _ = parser.parse_known_args(argv)
    except SystemExit:
        return False
    return bool(getattr(args, "use_mock_llm", False))


_configure_mock_mode(_detect_mock_flag(sys.argv[1:]))


GPU_HOST_ENV = "TOKENPLACE_GPU_HOST"
GPU_PORT_ENV = "TOKENPLACE_GPU_PORT"
UPSTREAM_URL_ENV = "TOKENPLACE_RELAY_UPSTREAM_URL"


def _load_upstream_config() -> Dict[str, Any]:
    upstream_override = os.environ.get(UPSTREAM_URL_ENV)
    parsed_host = None
    parsed_port: int | None = None

    if upstream_override:
        try:
            parsed = urlparse(upstream_override)
        except ValueError:
            parsed = None
        if parsed and parsed.hostname:
            parsed_host = parsed.hostname
            parsed_port = parsed.port

    host = (
        os.environ.get(GPU_HOST_ENV)
        or os.environ.get("GPU_SERVER_HOST")
        or parsed_host
        or "gpu-server"
    )
    port_source = (
        os.environ.get(GPU_PORT_ENV)
        or os.environ.get("GPU_SERVER_PORT")
        or (str(parsed_port) if parsed_port is not None else None)
        or "3000"
    )
    port = int(port_source)
    upstream_url = upstream_override or f"http://{host}:{port}"
    return {
        "gpu_host": host,
        "gpu_port": port,
        "upstream_url": upstream_url,
    }


UPSTREAM_CONFIG = _load_upstream_config()


def create_app() -> Flask:
    """Instantiate and configure the Flask application."""

    flask_app = Flask(__name__)
    configure_app_logging(flask_app)
    flask_app.config.update(UPSTREAM_CONFIG)

    from api import init_app  # Imported lazily to honor mock-mode configuration

    init_app(flask_app)
    LOGGER.info(
        "relay.app.initialized",
        extra={"upstream": flask_app.config.get("upstream_url")},
    )
    return flask_app


app = create_app()


def _get_request_counter() -> Counter:
    metric_name = "tokenplace_relay_requests_total"
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(metric_name)
    if existing is not None:
        return existing  # type: ignore[return-value]
    return Counter(
        metric_name,
        "Total HTTP requests processed by token.place relay",
        ["method", "endpoint", "status"],
    )


REQUEST_COUNTER = _get_request_counter()


def _load_server_registration_token():
    """Return the configured relay server token, if any."""

    token = None
    try:
        from config import get_config

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


known_servers = {}
client_inference_requests = {}
client_responses = {}
streaming_sessions = {}
streaming_sessions_by_client = {}
stream_lock = threading.Lock()

IGNORED_LOG_ENDPOINTS = {"livez", "healthz", "metrics"}


def _can_resolve_gpu_host(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except socket.gaierror:
        return False


@app.before_request
def _record_request_start():
    g.request_start_time = time.time()
    g.request_id = request.headers.get("X-Request-Id") or secrets.token_hex(8)


@app.after_request
def _log_request(response: Response):
    endpoint = request.endpoint or "unknown"
    status_code = str(response.status_code)

    try:
        REQUEST_COUNTER.labels(request.method, endpoint, status_code).inc()
    except Exception:  # pragma: no cover - defensive metric increment
        LOGGER.debug(
            "metrics.increment_failed",
            extra={"endpoint": endpoint, "status": status_code},
        )

    duration = None
    if hasattr(g, "request_start_time"):
        duration = max(time.time() - g.request_start_time, 0)

    if endpoint not in IGNORED_LOG_ENDPOINTS:
        LOGGER.info(
            "http.request",
            extra={
                "http_method": request.method,
                "http_path": request.path,
                "http_status": int(status_code),
                "duration_ms": round((duration or 0) * 1000, 2),
                "request_id": getattr(g, "request_id", None),
                "user_agent": request.headers.get("User-Agent"),
            },
        )

    if getattr(g, "request_id", None):
        response.headers.setdefault("X-Request-Id", g.request_id)

    if endpoint == "metrics":
        response.headers.setdefault("Cache-Control", "no-store")

    return response


@app.route("/healthz", methods=["GET"])
def healthz():
    gpu_host = app.config.get("gpu_host")
    status = {
        "status": "ok",
        "upstream": app.config.get("upstream_url"),
        "gpuHost": gpu_host,
        "knownServers": len(known_servers),
    }

    if DRAINING.is_set():
        status["status"] = "draining"
        status.setdefault("details", {})["shutdown"] = True
        response = jsonify(status)
        response.status_code = 503
        response.headers["Retry-After"] = "0"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    if gpu_host and not _can_resolve_gpu_host(gpu_host):
        status["status"] = "degraded"
        status.setdefault("details", {})["gpuHostResolution"] = "failed"
        LOGGER.warning(
            "healthz.resolution_failed",
            extra={"gpu_host": gpu_host},
        )
        return jsonify(status), 503

    if not known_servers:
        status.setdefault("details", {})["knownServers"] = "empty"

    return jsonify(status)


@app.route("/livez", methods=["GET"])
def livez():
    return jsonify({"status": "alive"})
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


def serve(host: str, port: int) -> None:
    """Run the relay application using Werkzeug's production server."""

    server = make_server(host, port, app, threaded=True)
    ctx = app.app_context()
    ctx.push()

    shutdown_requested = threading.Event()

    def _handle_signal(signum, _frame):
        LOGGER.info("relay.shutdown_signal", extra={"signal": signum})
        shutdown_requested.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    LOGGER.info(
        "relay.startup",
        extra={
            "host": host,
            "port": port,
            "upstream": app.config.get("upstream_url"),
        },
    )

    try:
        server.serve_forever()
    finally:
        ctx.pop()
        LOGGER.info(
            "relay.shutdown",
            extra={"requested": shutdown_requested.is_set()},
        )


def main(argv: list[str] | None = None) -> None:
    args = parse_cli_args(argv)
    host = os.environ.get("RELAY_HOST") or args.host
    port_value = os.environ.get("RELAY_PORT") or str(args.port)
    try:
        port = int(port_value)
    except ValueError:
        LOGGER.warning("relay.invalid_port", extra={"port": port_value})
        port = args.port

    _configure_mock_mode(args.use_mock_llm)
    serve(host, port)


if __name__ == '__main__':  # pragma: no cover
    main()
