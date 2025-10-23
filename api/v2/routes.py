"""
API routes for token.place API v2.
This module extends the OpenAI-compatible surface with enhanced capabilities.
"""

from flask import Blueprint, request, jsonify, Response, stream_with_context
import base64
import time
import json
import uuid
import logging
import os

from typing import Any, Dict, Iterable, Optional

import encrypt

from api.v1.encryption import encryption_manager
from api.security import ensure_operator_access
from api.v1.moderation import evaluate_messages_for_policy
from api.v1.community import (
    get_provider_directory as _get_community_provider_directory,
    CommunityDirectoryError,
)
from api.v1.models import generate_response, get_model_instance, ModelError
from api.v2.models import get_models_info
from api.v1.validation import (
    ValidationError, validate_required_fields, validate_field_type,
    validate_chat_messages, validate_encrypted_request, validate_model_name
)
from utils.providers import (
    get_provider_directory as _get_registry_provider_directory,
    ProviderRegistryError,
)

# Expose directory loaders for tests and backwards compatibility
get_community_provider_directory = _get_community_provider_directory
get_registry_provider_directory = _get_registry_provider_directory

# Maintain compatibility with existing tests that expect a get_provider_directory
# attribute exposed on the routes module.
get_provider_directory = get_registry_provider_directory

# Check environment
ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set
SERVICE_NAME = os.getenv('SERVICE_NAME', 'token.place')

# Configure logging based on environment
if ENVIRONMENT != 'prod':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger('api.v2.routes')
else:
    # In production, set up a null handler to suppress all logs
    logging.basicConfig(handlers=[logging.NullHandler()])
    logger = logging.getLogger('api.v2.routes')

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

# Streaming helpers
def iter_stream_content_chunks(content: str, *, max_chunk_size: int = 512):
    """Yield successive slices of ``content`` respecting the configured chunk size."""

    if not content:
        return

    content_length = len(content)
    for start in range(0, content_length, max_chunk_size):
        yield content[start:start + max_chunk_size]

# Create a Blueprint for v2 API
v2_bp = Blueprint('v2', __name__, url_prefix='/api/v2')

def format_error_response(message, error_type="invalid_request_error", param=None, code=None, status_code=400):
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

@v2_bp.route('/models', methods=['GET'])
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
            base_model_id = model.get("base_model_id", model["id"])
            parent_id = base_model_id if base_model_id != model["id"] else None
            adapter_meta = model.get("adapter")
            metadata = {}
            if adapter_meta:
                metadata["adapter"] = {
                    "id": adapter_meta.get("id", model["id"]),
                    "share_base": adapter_meta.get("share_base", False),
                }
            entry = {
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
                "root": base_model_id,
                "parent": parent_id,
            }
            if metadata:
                entry["metadata"] = metadata
            formatted_models.append(entry)

        log_info(f"Returning {len(formatted_models)} models")
        return jsonify({
            "object": "list",
            "data": formatted_models
        })
    except Exception:
        log_error("Error in list_models endpoint", exc_info=True)
        return format_error_response(
            "Internal server error",
            error_type="internal_server_error",
            status_code=500,
        )

@v2_bp.route('/models/<model_id>', methods=['GET'])
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
        base_model_id = model.get("base_model_id", model["id"])
        parent_id = base_model_id if base_model_id != model["id"] else None
        adapter_meta = model.get("adapter")
        metadata = {}
        if adapter_meta:
            metadata["adapter"] = {
                "id": adapter_meta.get("id", model["id"]),
                "share_base": adapter_meta.get("share_base", False),
            }

        entry = {
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
            "root": base_model_id,
            "parent": parent_id,
        }
        if metadata:
            entry["metadata"] = metadata

        return jsonify(entry)
    except Exception:
        log_error(
            f"Error in get_model endpoint for model {model_id}",
            exc_info=True,
        )
        return format_error_response(
            "Internal server error",
            error_type="internal_server_error",
            status_code=500,
        )

def _public_key_response(log_label: str | None = None):
    try:
        if log_label is None:
            log_label = f"{request.method.upper()} {request.path}"
        log_info(f"API request: {log_label}")
        return jsonify({'public_key': encryption_manager.public_key_b64})
    except Exception:
        log_error("Error in get_public_key endpoint", exc_info=True)
        return format_error_response(
            "Failed to retrieve public key",
            error_type="internal_server_error",
            status_code=500,
        )


def _rotate_public_key_response(log_label: str | None = None):
    try:
        if log_label is None:
            log_label = f"{request.method.upper()} {request.path}"
        log_info(f"API request: {log_label}")
        encryption_manager.rotate_keys()
        return jsonify({'public_key': encryption_manager.public_key_b64})
    except Exception:
        log_error("Error rotating public key", exc_info=True)
        return format_error_response(
            "Failed to rotate public key",
            error_type="internal_server_error",
            status_code=500,
        )


@v2_bp.route('/public-key', methods=['GET'])
def get_public_key():
    """Expose the current public key used for encrypted requests."""
    return _public_key_response()


@v2_bp.route('/public-key/rotate', methods=['POST'])
def rotate_public_key():
    """Rotate the RSA key pair powering encrypted API traffic."""
    auth_error = ensure_operator_access(format_error_response, log_warning)
    if auth_error:
        return auth_error
    return _rotate_public_key_response()


@v2_bp.route('/community/providers', methods=['GET'])
def list_community_providers():
    """Expose the community-operated relay and server provider directory."""

    try:
        log_info("API request: GET /community/providers")
        directory = get_community_provider_directory()
    except CommunityDirectoryError:
        log_error("Error loading community provider directory", exc_info=True)
        return format_error_response(
            "Community directory temporarily unavailable",
            error_type="internal_server_error",
            status_code=500,
        )

    response_payload = {
        "object": "list",
        "data": directory.get("providers", []),
    }

    updated = directory.get("updated")
    if updated:
        response_payload["updated"] = updated

    return jsonify(response_payload)


@v2_bp.route('/server-providers', methods=['GET'])
def list_server_providers():
    """Expose the self-hosted relay provider registry."""

    try:
        log_info("API request: GET /server-providers")
        directory = get_provider_directory()
    except ProviderRegistryError as exc:
        log_error("Error loading provider registry", exc_info=True)
        return format_error_response(
            f"Failed to load provider registry: {exc}",
            error_type="internal_error",
            code="provider_registry_unavailable",
            status_code=500,
        )

    response_payload = {
        "object": "list",
        "data": directory.get("providers", []),
    }

    metadata = directory.get("metadata")
    if metadata:
        response_payload["metadata"] = metadata

    return jsonify(response_payload)


@v2_bp.route('/chat/completions', methods=['POST'])
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

        # Validate request
        if not data:
            return format_error_response(
                "Invalid request body: empty or not JSON",
                error_type="invalid_request_error",
                status_code=400
            )

        try:
            # Validate required fields
            validate_required_fields(data, ["model"])

            is_encrypted_request = bool(data.get('encrypted', False))
            stream_requested = bool(data.get('stream', False))

            # Get available models
            models = get_models_info()
            available_model_ids = [model["id"] for model in models]

            # Validate model
            model_id = data['model']
            validate_model_name(model_id, available_model_ids)

            # Get model instance - will raise ModelError if not found
            model_instance = get_model_instance(model_id)
            log_info(f"Model instance obtained for {model_id}")

            # Process message payload based on encryption flag
            messages = None
            client_public_key = None

            if is_encrypted_request:
                log_info("Processing encrypted request")

                try:
                    # Validate encrypted request
                    validate_encrypted_request(data)
                    client_public_key = data['client_public_key']

                    # Decrypt the messages
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

                    # Parse JSON from decrypted data
                    try:
                        messages = json.loads(decrypted_data.decode('utf-8'))
                    except json.JSONDecodeError:
                        return format_error_response(
                            "Failed to parse JSON from decrypted messages",
                            error_type="encryption_error",
                            status_code=400
                        )

                except ValidationError as e:
                    return format_error_response(
                        e.message,
                        param=e.field,
                        code=e.code,
                        status_code=400
                    )

            else:
                log_info("Processing standard (non-encrypted) request")

                try:
                    # Validate messages field
                    validate_required_fields(data, ["messages"])
                    validate_field_type(data, "messages", list)
                    messages = data["messages"]
                except ValidationError as e:
                    return format_error_response(
                        e.message,
                        param=e.field,
                        code=e.code,
                        status_code=400
                    )

            # Validate messages format
            try:
                validate_chat_messages(messages)
            except ValidationError as e:
                return format_error_response(
                    e.message,
                    param=e.field,
                    code=e.code,
                    status_code=400
                )

            decision = evaluate_messages_for_policy(messages)
            if not decision.allowed:
                log_warning(
                    "Blocking chat completion request due to content policy violation: %s"
                    % (decision.matched_term or "unknown term")
                )
                return format_error_response(
                    decision.reason or "Request blocked by content moderation policy.",
                    error_type="content_policy_violation",
                    code="content_blocked",
                    status_code=400,
                )

            # Generate response using the specified model
            log_info(f"Generating response using model {model_id}")

            openai_option_keys = {
                "frequency_penalty",
                "logit_bias",
                "max_tokens",
                "n",
                "presence_penalty",
                "response_format",
                "seed",
                "stop",
                "temperature",
                "tool_choice",
                "tools",
                "top_p",
                "user",
                "function_call",
                "functions",
            }
            model_request_options = {
                key: data[key]
                for key in openai_option_keys
                if key in data
            }

            updated_messages = generate_response(
                model_id,
                messages,
                **model_request_options,
            )

            # Extract the last message (the model's response)
            assistant_message = updated_messages[-1]
            log_info("Response generated successfully")

            # Create response in OpenAI format
            tool_calls = assistant_message.get("tool_calls")
            finish_reason = "tool_calls" if tool_calls else "stop"

            message_payload = {
                "role": assistant_message.get("role", "assistant"),
                "content": assistant_message.get("content")
            }

            if tool_calls:
                message_payload["tool_calls"] = tool_calls

            response_data = {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": message_payload,
                        "finish_reason": finish_reason
                    }
                ],
                "usage": {
                    "prompt_tokens": -1,  # We don't track tokens
                    "completion_tokens": -1,
                    "total_tokens": -1
                }
            }

            if stream_requested:
                stream_id = f"chatcmpl-{uuid.uuid4()}"
                created_ts = int(time.time())
                role = assistant_message.get("role", "assistant")
                content_text = assistant_message.get("content") or ""

                def serialize_tool_call(call, index):
                    function = call.get("function") if isinstance(call, dict) else {}
                    if not isinstance(function, dict):
                        function = {}
                    return {
                        "index": index,
                        "id": call.get("id"),
                        "type": call.get("type", "function"),
                        "function": {
                            "name": function.get("name"),
                            "arguments": function.get("arguments", ""),
                        },
                    }

                def build_chunk(delta, finish_reason=None):
                    return {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": finish_reason
                            }
                        ]
                    }

                def iter_chunk_payloads() -> Iterable[tuple[str, Dict[str, Any]]]:
                    yield "delta", build_chunk({"role": role}, None)

                    if content_text:
                        for content_segment in iter_stream_content_chunks(content_text):
                            yield "delta", build_chunk({"content": content_segment}, None)

                    if tool_calls:
                        for idx, call in enumerate(tool_calls):
                            call_delta = {
                                "tool_calls": [serialize_tool_call(call, idx)]
                            }
                            yield "delta", build_chunk(call_delta, None)

                    yield "delta", build_chunk({}, finish_reason)

                if is_encrypted_request:
                    log_info("Returning encrypted streaming response")

                    if not client_public_key:
                        return format_error_response(
                            "Client public key required for encrypted streaming",
                            error_type="encryption_error",
                            status_code=400,
                        )

                    try:
                        client_public_key_bytes = base64.b64decode(client_public_key)
                    except (TypeError, ValueError):
                        return format_error_response(
                            "Client public key is not valid base64",
                            error_type="encryption_error",
                            status_code=400,
                        )

                    stream_session_id = f"stream-{uuid.uuid4()}"

                    def encrypted_event_stream():
                        stream_session: Optional[encrypt.StreamSession] = None

                        for event_name, payload in iter_chunk_payloads():
                            try:
                                plaintext_bytes = json.dumps(payload).encode('utf-8')
                            except (TypeError, ValueError):
                                log_error("Failed to serialise streaming chunk for encryption", exc_info=True)
                                yield "data: {\"event\": \"error\", \"reason\": \"serialization_failed\"}\n\n"
                                return

                            try:
                                ciphertext_dict, encrypted_key, stream_session = encrypt.encrypt_stream_chunk(
                                    plaintext_bytes,
                                    client_public_key_bytes,
                                    session=stream_session,
                                )
                            except Exception:
                                log_error("Failed to encrypt streaming chunk", exc_info=True)
                                yield "data: {\"event\": \"error\", \"reason\": \"encryption_failed\"}\n\n"
                                return

                            payload_dict = {
                                "encrypted": True,
                                "ciphertext": base64.b64encode(ciphertext_dict["ciphertext"]).decode('utf-8'),
                                "iv": base64.b64encode(ciphertext_dict["iv"]).decode('utf-8'),
                                "stream_session_id": stream_session_id,
                            }

                            if 'tag' in ciphertext_dict:
                                payload_dict['tag'] = base64.b64encode(ciphertext_dict['tag']).decode('utf-8')

                            mode_value = ciphertext_dict.get('mode')
                            if isinstance(mode_value, str):
                                payload_dict['mode'] = mode_value

                            session_ad = getattr(stream_session, 'associated_data', None)
                            if session_ad:
                                payload_dict['associated_data'] = base64.b64encode(session_ad).decode('utf-8')

                            if encrypted_key is not None:
                                payload_dict['cipherkey'] = base64.b64encode(encrypted_key).decode('utf-8')

                            envelope = {
                                "event": event_name,
                                "encrypted": True,
                                "stream_session_id": stream_session_id,
                                "data": payload_dict,
                            }
                            yield f"data: {json.dumps(envelope)}\n\n"

                        yield "data: [DONE]\n\n"

                    response = Response(
                        stream_with_context(encrypted_event_stream()),
                        mimetype='text/event-stream',
                    )
                    response.headers['Cache-Control'] = 'no-cache'
                    return response

                log_info("Returning streaming response")

                def event_stream():
                    for _, payload in iter_chunk_payloads():
                        yield f"data: {json.dumps(payload)}\n\n"
                    yield "data: [DONE]\n\n"

                response = Response(stream_with_context(event_stream()), mimetype='text/event-stream')
                response.headers['Cache-Control'] = 'no-cache'
                return response

            if is_encrypted_request and client_public_key:
                log_info("Encrypting response for client")
                encrypted_response = encryption_manager.encrypt_message(response_data, client_public_key)
                if encrypted_response is None:
                    return format_error_response(
                        "Failed to encrypt response",
                        error_type="encryption_error",
                        status_code=500
                    )

                # Wrap the encrypted data in a standard format
                return jsonify({
                    "encrypted": True,
                    "data": encrypted_response
                })
            else:
                # Return standard response
                return jsonify(response_data)

        except ValidationError as e:
            return format_error_response(
                e.message,
                param=e.field,
                code=e.code,
                status_code=400
            )

        except ModelError as e:
            return format_error_response(
                e.message,
                error_type="model_error",
                status_code=400
            )

    except Exception:
        log_error("Unexpected error in create_chat_completion endpoint", exc_info=True)
        return format_error_response(
            "Internal server error",
            error_type="server_error",
            status_code=500
        )

@v2_bp.route('/completions', methods=['POST'])
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

        # Extract necessary data
        model_id = data.get("model")
        prompt = data.get("prompt", "")
        client_public_key = data.get("client_public_key") # For potential encryption
        is_encrypted_request = data.get("encrypted", False)

        # Validate model ID
        if not model_id:
            log_warning("Missing required parameter: model")
            return format_error_response(
                "Missing required parameter: model",
                error_type="invalid_request_error",
                param="model",
                status_code=400
            )

        try:
            # Check if model exists - will raise ModelError if not found
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

        # Prepare messages for chat format
        messages = [
            {
                "role": "user",
                "content": prompt,
            }
        ]

        decision = evaluate_messages_for_policy(messages)
        if not decision.allowed:
            log_warning(
                "Blocking legacy completion request due to content policy violation: %s"
                % (decision.matched_term or "unknown term")
            )
            return format_error_response(
                decision.reason or "Request blocked by content moderation policy.",
                error_type="content_policy_violation",
                code="content_blocked",
                status_code=400,
            )

        # Generate response
        try:
            log_info(f"Generating response using model {model_id}")
            updated_messages = generate_response(model_id, messages)

            assistant_message = updated_messages[-1]
            log_info("Response generated successfully")

            # Create response in OpenAI text completion format
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

            # Encrypt response if client_public_key was provided
            if is_encrypted_request and client_public_key:
                log_info("Encrypting response for client")
                # Note: We assume the original request might set 'encrypted:true' and 'client_public_key'
                # even though it only sends a 'prompt', to signal it wants an encrypted response.
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

        except ModelError as e:
            log_warning(f"Model error during response generation: {e.message}")
            return format_error_response(
                e.message,
                error_type=e.error_type,
                status_code=e.status_code
            )
    except Exception:
        log_error("Unexpected error in create_completion endpoint", exc_info=True)
        return format_error_response(
            "Internal server error",
            error_type="server_error",
            status_code=500,
        )

@v2_bp.route('/health', methods=['GET'])
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
            'version': 'v2',
            'service': SERVICE_NAME,
            'timestamp': int(time.time())
        })
    except Exception:
        log_error("Error in health_check endpoint", exc_info=True)
        return format_error_response(
            "Health check failed",
            error_type="internal_server_error",
            status_code=500,
        )

# --- OpenAI-compatible alias routes ---

# Create a second blueprint that mirrors the /api/v2 endpoints at /v2 so
# the OpenAI Python client can talk to token.place by simply changing the
# base URL.
openai_v2_bp = Blueprint('openai_v2', __name__, url_prefix='/v2')

@openai_v2_bp.route('/models', methods=['GET'])
def list_models_openai():
    return list_models()

@openai_v2_bp.route('/models/<model_id>', methods=['GET'])
def get_model_openai(model_id):
    return get_model(model_id)

@openai_v2_bp.route('/public-key', methods=['GET'])
def get_public_key_openai():
    return get_public_key()


@openai_v2_bp.route('/public-key/rotate', methods=['POST'])
def rotate_public_key_openai():
    return rotate_public_key()

@openai_v2_bp.route('/chat/completions', methods=['POST'])
def create_chat_completion_openai():
    return create_chat_completion()

@openai_v2_bp.route('/completions', methods=['POST'])
def create_completion_openai():
    return create_completion()

@openai_v2_bp.route('/health', methods=['GET'])
def health_check_openai():
    return health_check()
