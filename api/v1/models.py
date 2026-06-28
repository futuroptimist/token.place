"""
Model management for token.place API v1
This module provides model information and management.
"""

import os
import random
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from utils.llm.model_profiles import MODEL_ALIASES, public_api_v1_profiles

try:
    import llama_cpp as _llama_cpp_module
    from llama_cpp import Llama as _llama_runtime
except ImportError:  # pragma: no cover - exercised in relay-only installs
    Llama = None  # type: ignore[assignment]
else:
    _repo_root = Path(__file__).resolve().parents[2]
    _shim_path = _repo_root / "llama_cpp.py"
    _module_origin = Path(getattr(_llama_cpp_module, "__file__", "")).resolve()
    if _module_origin == _shim_path:
        Llama = None  # type: ignore[assignment]
    else:
        Llama = _llama_runtime

# Check environment
ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Configure logging based on environment
if ENVIRONMENT != 'prod':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger('api.v1.models')
else:
    # In production, set up a null handler to suppress all logs
    logging.basicConfig(handlers=[logging.NullHandler()])
    logger = logging.getLogger('api.v1.models')

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


def _stringify_content_blocks(content: Any) -> Any:
    """Normalise text-only structured message content into newline-delimited text."""

    if isinstance(content, str) or content is None:
        return content

    if not isinstance(content, list):
        return content

    segments: List[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if block_type in {"input_text", "text"}:
            text_value = block.get("text")
            if isinstance(text_value, str) and text_value.strip():
                segments.append(text_value.strip())
            continue

    if not segments:
        return ""

    return "\n\n".join(segments)


def _normalise_chat_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse list-based text content blocks in-place for llama.cpp compatibility."""

    normalised: List[Dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            normalised.append(message)
            continue

        updated = dict(message)
        updated["content"] = _stringify_content_blocks(message.get("content"))
        normalised.append(updated)

    messages[:] = normalised
    return messages

# Check if we're using mock LLM
USE_MOCK_LLM = os.environ.get('USE_MOCK_LLM', '0') == '1'
if ENVIRONMENT != 'prod':
    logger.info(f"API v1 Models module loaded with USE_MOCK_LLM={USE_MOCK_LLM}, raw env value: '{os.environ.get('USE_MOCK_LLM', 'NOT_SET')}'")

# Available model metadata
def _profile_to_api_entry(profile):
    return {
        "id": profile.api_model_id,
        "name": profile.display_name,
        "description": profile.description,
        "owner": profile.owner,
        "owned_by": profile.owner,
        "provider": profile.provider,
        "source_model": profile.source_model,
        "parameters": profile.parameters,
        "quantization": profile.quantization,
        "license": profile.license,
        "gguf_repo": profile.gguf_repo,
        "context_length": profile.default_context_tokens,
        "native_context_tokens": profile.native_context_tokens,
        "maximum_validated_context_tokens": profile.maximum_validated_context_tokens,
        "supported_context_tiers": list(profile.supported_context_tiers),
        "chat_template_policy": profile.chat_template_policy,
        "thinking_mode": profile.thinking_mode,
        "url": profile.download_url,
        "file_name": profile.filename,
        "adapters": [],
    }


AVAILABLE_MODELS = [_profile_to_api_entry(profile) for profile in public_api_v1_profiles()]


# Dictionary mapping model IDs to loaded model instances
_loaded_models = {}


def _iter_model_entries() -> Iterable[Dict[str, Any]]:
    """Yield flattened model entries, expanding adapter metadata."""

    for base in AVAILABLE_MODELS:
        base_entry = {key: value for key, value in base.items() if key != "adapters"}
        base_entry["base_model_id"] = base["id"]
        yield base_entry

        for adapter in base.get("adapters", []):
            derived = {key: value for key, value in base.items() if key != "adapters"}
            derived["id"] = adapter["id"]
            derived["name"] = adapter.get("name", base_entry["name"])
            derived["description"] = adapter.get("description", base_entry["description"])
            derived["file_name"] = adapter.get("file_name", base_entry.get("file_name"))
            derived["parameters"] = adapter.get("parameters", base_entry.get("parameters"))
            derived["quantization"] = adapter.get("quantization", base_entry.get("quantization"))
            derived["context_length"] = adapter.get("context_length", base_entry.get("context_length"))
            derived["url"] = adapter.get("url", base_entry.get("url"))
            derived["base_model_id"] = base["id"]
            derived["adapter"] = {
                "id": adapter["id"],
                "instructions": adapter.get("instructions"),
                "prompt_template": adapter.get("prompt_template"),
                "share_base": adapter.get("share_base", False),
            }
            yield derived


def _get_model_metadata(model_id: str) -> Optional[Dict[str, Any]]:
    """Return metadata for a model or adapter by ID."""

    for entry in _iter_model_entries():
        if entry["id"] == model_id:
            return entry

    return None


def resolve_model_alias(model_id: str) -> Optional[str]:
    """Resolve a requested model identifier to the canonical catalogue entry."""

    target_id = MODEL_ALIASES.get(model_id)
    if not target_id:
        return None

    if _get_model_metadata(target_id) is None:
        warning_msg = (
            f"Ignoring alias '{model_id}' because the target model '{target_id}' is unavailable"
        )
        log_warning(warning_msg)  # pragma: no cover - defensive logging
        return None

    return target_id

class ModelError(Exception):
    """Custom exception for model-related errors"""
    def __init__(self, message, status_code=500, error_type="model_error"):
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        super().__init__(self.message)

def get_models_info():
    """
    Get information about available models

    Returns:
        list: List of model metadata dictionaries
    """
    logger.debug("Retrieving models information")
    return list(_iter_model_entries())

def get_model_instance(model_id):
    """
    Get or load a model instance by ID

    Args:
        model_id: The ID of the model to get

    Returns:
        Llama: The model instance

    Raises:
        ModelError: If the model is not found or cannot be loaded
    """
    # Add detailed debug info
    logger.info(f"Getting model instance for: {model_id}")

    # Check input
    if not model_id:
        raise ModelError("Model ID cannot be empty", status_code=400, error_type="invalid_request_error")

    model_id = resolve_model_alias(model_id) or model_id

    # First check if the model ID exists in available models
    model_meta = _get_model_metadata(model_id)
    if not model_meta:
        available_ids = [m["id"] for m in _iter_model_entries()]
        logger.warning(f"Model {model_id} not found. Available models: {available_ids}")
        raise ModelError(
            f"Model '{model_id}' not found. Available models: {', '.join(available_ids)}",
            status_code=400,
            error_type="model_not_found"
        )

    # For testing or when mock is enabled, return a mock model
    if USE_MOCK_LLM:
        logger.info(f"Using mock LLM for model_id: {model_id} (mock mode enabled)")
        return "MOCK_MODEL"

    if Llama is None:
        raise ModelError(
            "llama-cpp-python is not installed. Install full server dependencies to load local models.",
            status_code=503,
            error_type="model_unavailable",
        )

    # In real operation, check if model is already loaded
    adapter_meta = model_meta.get("adapter")
    cache_key = model_id
    if adapter_meta and adapter_meta.get("share_base"):
        cache_key = model_meta["base_model_id"]

    if cache_key in _loaded_models:
        logger.info(f"Using cached model instance for {cache_key}")
        llama = _loaded_models[cache_key]
        _loaded_models[model_id] = llama
        return llama

    # Load the model from disk if not already loaded
    try:
        model_path = model_meta.get("file_name")
        if adapter_meta and adapter_meta.get("share_base"):
            base_meta = _get_model_metadata(model_meta["base_model_id"])
            if base_meta:
                model_path = base_meta.get("file_name")

        if not model_path:
            raise ValueError("Model metadata missing file name")

        if not os.path.isabs(model_path):
            model_path = os.path.join("models", model_path)
        logger.info(f"Loading model from {model_path}")
        llama = Llama(model_path=model_path)
        _loaded_models[cache_key] = llama
        _loaded_models[model_id] = llama
        return llama
    except Exception as e:
        logger.exception(f"Failed to load model {model_id}: {e}")
        raise ModelError(
            f"Failed to load model '{model_id}': {str(e)}",
            status_code=500,
            error_type="model_load_error",
        )

def generate_response(model_id, messages, **options):
    """
    Generate a response using the specified model

    Args:
        model_id: The ID of the model to use
        messages: List of message dictionaries with 'role' and 'content' keys
        **options: Additional OpenAI-compatible parameters to pass through to the
            underlying model implementation (e.g. temperature, tools)

    Returns:
        list: Updated messages list with the model's response appended

    Raises:
        ModelError: If there's an error with the model or input
    """
    start_time = time.time()
    model_id = resolve_model_alias(model_id) or model_id
    logger.info(f"Generating response using model: {model_id}")

    # Validate input
    if not messages:
        raise ModelError("Messages cannot be empty", status_code=400, error_type="invalid_request_error")

    # Validate message format. API v1 chat is text-only; structured content
    # blocks are accepted only for text segmentation and must not imply image
    # or multimodal support for the single Llama runtime target.
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
            raise ModelError(
                f"Invalid message format at position {idx}. Each message must have 'role' and 'content' fields.",
                status_code=400,
                error_type="invalid_request_error"
            )

        content = msg.get('content')
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for block_idx, block in enumerate(content):
                if (
                    not isinstance(block, dict)
                    or block.get('type') not in {'input_text', 'text'}
                    or not isinstance(block.get('text'), str)
                    or not block.get('text')
                ):
                    raise ModelError(
                        (
                            f"Invalid text-only content block at messages[{idx}].content[{block_idx}]. "
                            "API v1 chat completions do not support image content."
                        ),
                        status_code=400,
                        error_type="invalid_request_error",
                    )
            continue
        raise ModelError(
            f"Invalid content type at messages[{idx}]. API v1 chat content must be text-only.",
            status_code=400,
            error_type="invalid_request_error",
        )

    model_meta = _get_model_metadata(model_id)
    adapter_meta = (model_meta or {}).get("adapter")

    try:
        # Collapse multi-part text-only content blocks so llama.cpp receives plain strings.
        # API v1 intentionally has no image/multimodal chat support; validators
        # reject image blocks before this runtime path.
        messages = _normalise_chat_messages(messages)

        # Get the model instance (or mock)
        model = get_model_instance(model_id)

        if adapter_meta and adapter_meta.get("instructions"):
            adapter_name = f"adapter:{adapter_meta.get('id', model_id)}"
            already_injected = any(
                msg.get("role") == "system" and msg.get("name") == adapter_name
                for msg in messages
            )
            if not already_injected:
                messages.insert(0, {
                    "role": "system",
                    "name": adapter_name,
                    "content": adapter_meta["instructions"],
                })

        # Check if we're using a mock model - either through env variable or the returned model is the string "MOCK_MODEL"
        mock_mode = USE_MOCK_LLM or model == "MOCK_MODEL"
        logger.debug(f"Generate response using mock_mode={mock_mode}, model={model}")

        # If we're using a mock model, generate a mock response
        if mock_mode:
            logger.info("Generating mock response")
            # Create a mock response that specifically mentions Paris for our tests
            mock_responses = [
                "Mock response: Paris is the capital of France and one of the most visited cities in the world.",
                "Mock response: The capital of France is Paris, known for its iconic Eiffel Tower and the Louvre Museum.",
                "Mock response: Paris, the City of Light, serves as France's capital and cultural center.",
            ]
            assistant_message = {
                "role": "assistant",
                "content": random.choice(mock_responses)
            }
            messages.append(assistant_message)

            # Log completion time
            elapsed = time.time() - start_time
            logger.info(f"Response generated in {elapsed:.2f}s (mock mode)")
            return messages

        # Generate response with the real model
        logger.info("Generating response with real model")
        response = model.create_chat_completion(messages=messages, **options)

        # Extract and append the assistant's message
        if response and 'choices' in response and response['choices']:
            assistant_message = response['choices'][0]['message']
            messages.append(assistant_message)

            # Log completion time
            elapsed = time.time() - start_time
            logger.info(f"Response generated in {elapsed:.2f}s")
            return messages
        else:
            raise ModelError(
                "Model returned an invalid response structure",
                status_code=500,
                error_type="model_response_error"
            )

    except ModelError:
        # Re-raise ModelError exceptions without wrapping
        raise
    except Exception as e:
        logger.exception(f"Error generating response: {str(e)}")
        raise ModelError(
            f"Failed to generate response: {str(e)}",
            status_code=500,
            error_type="model_inference_error"
        )
