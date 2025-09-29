"""
API routes for token.place API v1
This module follows OpenAI API conventions to serve as a drop-in replacement.
"""

from flask import Blueprint, request, jsonify, Response, stream_with_context
import base64
import time
import json
import uuid
import logging
import os

from api.v1.encryption import encryption_manager
from api.v1.models import (
    get_models_info,
    generate_response,
    get_model_instance,
    stream_chat_completion,
    ModelError,
)
from api.v1.validation import (
    ValidationError, validate_required_fields, validate_field_type,
    validate_chat_messages, validate_encrypted_request, validate_model_name
)

# Check environment
ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set
SERVICE_NAME = os.getenv('SERVICE_NAME', 'token.place')

# Configure logging based on environment
if ENVIRONMENT != 'prod':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    logger = logging.getLogger('api.v1.routes')
else:
    # In production, set up a null handler to suppress all logs
    logging.basicConfig(handlers=[logging.NullHandler()])
    logger = logging.getLogger('api.v1.routes')

def log_info(message):
    """Log info only in non-production environments"""
    if ENVIRONMENT != 'prod':
        logger.info(message)

def log_warning(message):
    """Log warnings only in non-production environments"""
    if ENVIRONMENT != 'prod':
        logger.warning(message)

def log_error(message, exc_info=False):
    """Log errors only in non-production environments"""
    if ENVIRONMENT != 'prod':
        logger.error(message, exc_info=exc_info)


def _coerce_stream_flag(data, override=None):
    """Normalize the `stream` flag from the request payload."""

    if override is not None:
        return bool(override)

    stream_requested = data.get('stream', False)
    if isinstance(stream_requested, str):
        stream_requested = stream_requested.lower() not in {"false", "0", "no", "off", ""}

    return bool(stream_requested)


def _format_sse(payload_json, event=None):
    lines = []
    if event:
        lines.append(f"event: {event}")
    for line in payload_json.splitlines() or [payload_json]:
        lines.append(f"data: {line}")
    lines.append("")
    return "\n".join(lines)


def _stream_sse_response(chunk_iterable, *, encrypted, client_public_key, builder):
    """Wrap a streaming iterator in an SSE Flask response."""

    def event_stream():
        for raw_chunk in chunk_iterable:
            built = builder(raw_chunk)
            if not built:
                continue

            payload, finished = built

            if encrypted:
                encrypted_payload = encryption_manager.encrypt_message(
                    payload,
                    client_public_key,
                )
                if not encrypted_payload:
                    log_error("Failed to encrypt streaming chunk")
                    break
                payload_json = json.dumps(
                    {"encrypted": True, "chunk": encrypted_payload},
                    separators=(",", ":"),
                )
            else:
                payload_json = json.dumps(payload, separators=(",", ":"))

            yield _format_sse(payload_json)

            if finished:
                break

        yield "data: [DONE]\n\n"

    response = Response(stream_with_context(event_stream()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


def _stream_chat_response(model_id, messages, *, encrypted, client_public_key):
    request_id = f"chatcmpl-{uuid.uuid4()}"
    created = int(time.time())

    def builder(raw_chunk):
        choices = raw_chunk.get('choices') if isinstance(raw_chunk, dict) else None
        if not choices:
            return None

        choice = choices[0]
        delta = choice.get('delta', {}) or {}
        finish_reason = choice.get('finish_reason')

        payload = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [
                {
                    "index": choice.get('index', 0),
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

        return payload, finish_reason == 'stop'

    return _stream_sse_response(
        stream_chat_completion(model_id, messages),
        encrypted=encrypted,
        client_public_key=client_public_key,
        builder=builder,
    )


def _stream_completion_response(model_id, prompt, *, encrypted, client_public_key):
    request_id = f"cmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    messages = [
        {
            "role": "user",
            "content": prompt or "",
        }
    ]

    def builder(raw_chunk):
        choices = raw_chunk.get('choices') if isinstance(raw_chunk, dict) else None
        if not choices:
            return None

        choice = choices[0]
        delta = choice.get('delta', {}) or {}
        finish_reason = choice.get('finish_reason')
        content_piece = delta.get('content', '') or ''

        if not content_piece and finish_reason is None:
            return None

        payload = {
            "id": request_id,
            "object": "text_completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [
                {
                    "index": choice.get('index', 0),
                    "text": content_piece,
                    "finish_reason": finish_reason,
                }
            ],
        }

        return payload, finish_reason == 'stop'

    return _stream_sse_response(
        stream_chat_completion(model_id, messages),
        encrypted=encrypted,
        client_public_key=client_public_key,
        builder=builder,
    )

# Create a Blueprint for v1 API
v1_bp = Blueprint('v1', __name__, url_prefix='/api/v1')

def format_error_response(
    message,
    error_type="invalid_request_error",
    param=None,
    code=None,
    status_code=400,
):
    """Format an error response in a standardized way for the API"""
    error_obj = {
        "error": {
            "message": message,
            "type": error_type,
        }
    }

    if param is not None:
        error_obj["error"]["param"] = param

    if code is not None:
        error_obj["error"]["code"] = code

    response = jsonify(error_obj)
    response.status_code = status_code
    return response

@v1_bp.route('/models', methods=['GET'])
def list_models():
    """
    List available models (OpenAI-compatible)

    Returns:
        JSON response with list of available models in OpenAI format
    """
    try:
        log_info("API request: GET /models")
        models = get_models_info()

        # Transform to OpenAI format
        formatted_models = []
        for model in models:
            formatted_models.append({
                "id": model["id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "token.place",
                "permission": [{
                    "id": f"modelperm-{model['id']}",
                    "object": "model_permission",
                    "created": int(time.time()),
                    "allow_create_engine": False,
                    "allow_sampling": True,
                    "allow_logprobs": True,
                    "allow_search_indices": False,
                    "allow_view": True,
                    "allow_fine_tuning": False,
                    "organization": "*",
                    "group": None,
                    "is_blocking": False
                }],
                "root": model["id"],
                "parent": None
            })

        log_info(f"Returning {len(formatted_models)} models")
        return jsonify({
            "object": "list",
            "data": formatted_models
        })
    except Exception as e:
        log_error("Error in list_models endpoint")
        return format_error_response(f"Internal server error: {str(e)}")

@v1_bp.route('/models/<model_id>', methods=['GET'])
def get_model(model_id):
    """
    Get model information by ID (OpenAI-compatible)

    Args:
        model_id: The ID of the model to retrieve

    Returns:
        JSON response with model details in OpenAI format
    """
    try:
        log_info(f"API request: GET /models/{model_id}")
        models = get_models_info()
        model = next((m for m in models if m["id"] == model_id), None)

        if not model:
            log_warning(f"Model '{model_id}' not found")
            return format_error_response(
                f"Model '{model_id}' not found",
                error_type="invalid_request_error",
                param=None,
                code="model_not_found",
                status_code=404
            )

        log_info(f"Returning model details for {model_id}")
        return jsonify({
            "id": model["id"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": "token.place",
            "permission": [{
                "id": f"modelperm-{model['id']}",
                "object": "model_permission",
                "created": int(time.time()),
                "allow_create_engine": False,
                "allow_sampling": True,
                "allow_logprobs": True,
                "allow_search_indices": False,
                "allow_view": True,
                "allow_fine_tuning": False,
                "organization": "*",
                "group": None,
                "is_blocking": False
            }],
            "root": model["id"],
            "parent": None
        })
    except Exception as e:
        log_error(f"Error in get_model endpoint for model {model_id}")
        return format_error_response(f"Internal server error: {str(e)}")

@v1_bp.route('/public-key', methods=['GET'])
def get_public_key():
    """
    Get the public key for encryption (token.place specific)
    This endpoint is not part of the OpenAI API but is needed for our encryption.

    Returns:
        JSON response with the server's public key
    """
    try:
        log_info("API request: GET /public-key")
        return jsonify({
            'public_key': encryption_manager.public_key_b64
        })
    except Exception as e:
        log_error("Error in get_public_key endpoint")
        return format_error_response(f"Failed to retrieve public key: {str(e)}")


def _process_chat_completion_request(data, *, stream_override=None):
    stream_requested = _coerce_stream_flag(data, stream_override)

    try:
        validate_required_fields(data, ["model"])

        models = get_models_info()
        available_model_ids = [model["id"] for model in models]

        model_id = data['model']
        validate_model_name(model_id, available_model_ids)

        get_model_instance(model_id)
        log_info(f"Model instance obtained for {model_id}")

        messages = None
        client_public_key = None

        if data.get('encrypted', False):
            log_info("Processing encrypted request")

            validate_encrypted_request(data)
            client_public_key = data['client_public_key']

            encrypted_messages = data['messages']
            decrypted_data = encryption_manager.decrypt_message({
                'ciphertext': base64.b64decode(encrypted_messages['ciphertext']),
                'iv': base64.b64decode(encrypted_messages['iv']),
            }, base64.b64decode(encrypted_messages['cipherkey']))

            if decrypted_data is None:
                return format_error_response(
                    "Failed to decrypt messages",
                    error_type="encryption_error",
                    status_code=400
                )

            try:
                messages = json.loads(decrypted_data.decode('utf-8'))
            except json.JSONDecodeError:
                return format_error_response(
                    "Failed to parse JSON from decrypted messages",
                    error_type="encryption_error",
                    status_code=400
                )

        else:
            log_info("Processing standard (non-encrypted) request")
            validate_required_fields(data, ["messages"])
            validate_field_type(data, "messages", list)
            messages = data["messages"]

        validate_chat_messages(messages)

        if stream_requested:
            if data.get('encrypted', False) and not client_public_key:
                return format_error_response(
                    "Client public key required for encrypted streaming",
                    error_type="invalid_request_error",
                    status_code=400,
                )

            try:
                return _stream_chat_response(
                    model_id,
                    messages,
                    encrypted=data.get('encrypted', False),
                    client_public_key=client_public_key,
                )
            except ModelError as exc:
                return format_error_response(
                    exc.message,
                    error_type=exc.error_type,
                    status_code=exc.status_code,
                )

        updated_messages = generate_response(model_id, messages)

        assistant_message = updated_messages[-1]
        log_info("Response generated successfully")

        response_data = {
            "id": f"chatcmpl-{uuid.uuid4()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": assistant_message.get("role", "assistant"),
                        "content": assistant_message.get("content", "")
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": -1,
                "completion_tokens": -1,
                "total_tokens": -1
            }
        }

        if data.get('encrypted', False) and client_public_key:
            log_info("Encrypting response for client")
            encrypted_response = encryption_manager.encrypt_message(
                response_data,
                client_public_key,
            )
            if encrypted_response is None:
                return format_error_response(
                    "Failed to encrypt response",
                    error_type="encryption_error",
                    status_code=500
                )

            return jsonify({
                "encrypted": True,
                "data": encrypted_response
            })

        return jsonify(response_data)

    except ValidationError as e:
        return format_error_response(
            e.message,
            param=e.field,
            code=e.code,
            status_code=400
        )
    except ModelError as e:
        status = e.status_code if e.status_code not in (None, 500) else 400
        return format_error_response(
            e.message,
            error_type=e.error_type,
            status_code=status
        )


def _process_completion_request(data, *, stream_override=None):
    stream_requested = _coerce_stream_flag(data, stream_override)

    model_id = data.get("model")
    prompt = data.get("prompt", "")
    client_public_key = data.get("client_public_key")
    is_encrypted_request = data.get("encrypted", False)

    if not model_id:
        log_warning("Missing required parameter: model")
        return format_error_response(
            "Missing required parameter: model",
            error_type="invalid_request_error",
            param="model",
            status_code=400
        )

    try:
        get_model_instance(model_id)
        log_info(f"Model instance obtained for {model_id}")
    except ModelError as e:
        log_warning(f"Model error: {e.message}")
        return format_error_response(
            e.message,
            error_type=e.error_type,
            param="model",
            code="model_not_found" if e.error_type == "model_not_found" else None,
            status_code=e.status_code
        )

    if stream_requested:
        if is_encrypted_request and not client_public_key:
            return format_error_response(
                "Client public key required for encrypted streaming",
                error_type="invalid_request_error",
                status_code=400,
            )

        try:
            return _stream_completion_response(
                model_id,
                prompt,
                encrypted=is_encrypted_request,
                client_public_key=client_public_key,
            )
        except ModelError as exc:
            return format_error_response(
                exc.message,
                error_type=exc.error_type,
                status_code=exc.status_code,
            )

    messages = [
        {
            "role": "user",
            "content": prompt,
        }
    ]

    try:
        updated_messages = generate_response(model_id, messages)
    except ModelError as e:
        log_warning(f"Model error during response generation: {e.message}")
        status = e.status_code if e.status_code not in (None, 500) else 400
        return format_error_response(
            e.message,
            error_type=e.error_type,
            status_code=status
        )

    assistant_message = updated_messages[-1]
    log_info("Response generated successfully")

    response_data = {
        "id": f"cmpl-{uuid.uuid4().hex[:12]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "text": assistant_message.get("content", ""),
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }

    if is_encrypted_request and client_public_key:
        log_info("Encrypting response for client")
        encrypted_response = encryption_manager.encrypt_message(response_data, client_public_key)
        if encrypted_response is None:
            log_error("Failed to encrypt response")
            return format_error_response(
                "Failed to encrypt response",
                error_type="server_error",
                status_code=500
            )
        return jsonify({
            "encrypted": True,
            "data": encrypted_response
        })

    return jsonify(response_data)

@v1_bp.route('/chat/completions', methods=['POST'])
def create_chat_completion():
    """
    Create a chat completion. Compatible with OpenAI's API format.
    For encrypted requests, expects:
    - client_public_key: Base64 encoded client public key
    - messages: Object with encrypted message data
        - ciphertext: Base64 encoded ciphertext
        - cipherkey: Base64 encoded encrypted key
        - iv: Base64 encoded initialization vector
    """
    try:
        log_info("API request: POST /chat/completions")
        data = request.get_json()

        if not data:
            return format_error_response(
                "Invalid request body: empty or not JSON",
                error_type="invalid_request_error",
                status_code=400
            )

        return _process_chat_completion_request(data)

    except Exception as e:
        log_error("Unexpected error in create_chat_completion endpoint", exc_info=True)
        return format_error_response(
            f"Internal server error: {str(e)}",
            error_type="server_error",
            status_code=500
        )


@v1_bp.route('/chat/completions/stream', methods=['POST'])
def create_chat_completion_stream():
    """Dedicated endpoint for streaming chat completions."""

    try:
        log_info("API request: POST /chat/completions/stream")
        data = request.get_json()

        if not data:
            return format_error_response(
                "Invalid request body: empty or not JSON",
                error_type="invalid_request_error",
                status_code=400
            )

        return _process_chat_completion_request(data, stream_override=True)

    except Exception as e:
        log_error("Unexpected error in create_chat_completion_stream endpoint", exc_info=True)
        return format_error_response(
            f"Internal server error: {str(e)}",
            error_type="server_error",
            status_code=500
        )


@v1_bp.route('/completions', methods=['POST'])
def create_completion():
    """
    Text completion API (OpenAI-compatible).

    The request is converted to chat format internally and the response is
    returned in the legacy text completion schema.
    """
    try:
        log_info("API request: POST /completions")
        data = request.get_json()

        if not data:
            log_warning("Invalid request body: empty or not JSON")
            return format_error_response(
                "Invalid request body",
                error_type="invalid_request_error",
                status_code=400
            )

        return _process_completion_request(data)

    except Exception as e:
        log_error("Unexpected error in create_completion endpoint")
        return format_error_response(f"Internal server error: {str(e)}")


@v1_bp.route('/completions/stream', methods=['POST'])
def create_completion_stream():
    """Dedicated endpoint for streaming legacy completions."""

    try:
        log_info("API request: POST /completions/stream")
        data = request.get_json()

        if not data:
            log_warning("Invalid request body: empty or not JSON")
            return format_error_response(
                "Invalid request body",
                error_type="invalid_request_error",
                status_code=400
            )

        return _process_completion_request(data, stream_override=True)

    except Exception as e:
        log_error("Unexpected error in create_completion_stream endpoint")
        return format_error_response(f"Internal server error: {str(e)}")


@v1_bp.route('/health', methods=['GET'])
def health_check():
    """
    API health check endpoint (token.place specific)

    Returns:
        JSON response with API status
    """
    try:
        log_info("API request: GET /health")
        return jsonify({
            'status': 'ok',
            'version': 'v1',
            'service': SERVICE_NAME,
            'timestamp': int(time.time())
        })
    except Exception as e:
        log_error("Error in health_check endpoint")
        return format_error_response(f"Health check failed: {str(e)}")

# --- OpenAI-compatible alias routes ---

# Create a second blueprint that mirrors the /api/v1 endpoints at /v1 so
# the OpenAI Python client can talk to token.place by simply changing the
# base URL.
openai_v1_bp = Blueprint('openai_v1', __name__, url_prefix='/v1')

@openai_v1_bp.route('/models', methods=['GET'])
def list_models_openai():
    return list_models()

@openai_v1_bp.route('/models/<model_id>', methods=['GET'])
def get_model_openai(model_id):
    return get_model(model_id)

@openai_v1_bp.route('/public-key', methods=['GET'])
def get_public_key_openai():
    return get_public_key()

@openai_v1_bp.route('/chat/completions', methods=['POST'])
def create_chat_completion_openai():
    return create_chat_completion()

@openai_v1_bp.route('/chat/completions/stream', methods=['POST'])
def create_chat_completion_stream_openai():
    return create_chat_completion_stream()

@openai_v1_bp.route('/completions', methods=['POST'])
def create_completion_openai():
    return create_completion()

@openai_v1_bp.route('/completions/stream', methods=['POST'])
def create_completion_stream_openai():
    return create_completion_stream()

@openai_v1_bp.route('/health', methods=['GET'])
def health_check_openai():
    return health_check()
