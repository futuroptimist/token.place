"""
Model management for token.place API v1
This module provides model information and management.
"""

import os
import random
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from llama_cpp import Llama
from utils.vision import analyze_base64_image, summarize_analysis

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


def _extract_base64_payload(block: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "encoded": None,
        "skipped_remote": False,
    }

    block_type = block.get("type")

    if block_type == "input_image" or block_type == "image":
        image_payload = block.get("image") or block.get("image_url") or {}
        if isinstance(image_payload, dict):
            encoded = (
                image_payload.get("b64_json")
                or image_payload.get("base64")
                or image_payload.get("data")
            )
            if isinstance(encoded, str) and encoded.strip():
                payload["encoded"] = encoded
    elif block_type == "image_url":
        image_url = block.get("image_url")
        if isinstance(image_url, dict):
            url_value = image_url.get("url")
        else:
            url_value = image_url

        if isinstance(url_value, str):
            if url_value.startswith("data:"):
                payload["encoded"] = url_value
            else:
                payload["skipped_remote"] = True

    return payload


def _build_vision_summary(messages: Sequence[Dict[str, Any]]) -> Optional[str]:
    analyses: List[Dict[str, Any]] = []
    skipped_remote = False

    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            payload = _extract_base64_payload(block)
            encoded = payload.get("encoded")
            if encoded:
                try:
                    analyses.append(analyze_base64_image(encoded))
                except ValueError as exc:
                    log_warning(f"Skipping invalid image payload: {exc}")
                continue

            if payload.get("skipped_remote"):
                skipped_remote = True

    if analyses:
        summary_text = summarize_analysis(analyses)
        if skipped_remote:
            summary_text += (
                " Additional attachments reference remote URLs; "
                "provide base64 data for inline analysis."
            )
        return summary_text

    if skipped_remote:
        return (
            "Vision analysis unavailable: remote image URLs require base64 "
            "data URIs for inspection."
        )

    return None


def _stringify_content_blocks(content: Any) -> Any:
    """Normalise structured OpenAI message content into newline-delimited text."""

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

        if block_type == "image_url":
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                url_value = image_url.get("url")
            else:
                url_value = image_url

            if isinstance(url_value, str) and url_value.strip():
                if url_value.lstrip().lower().startswith("data:"):
                    segments.append("[Inline image attached]")
                else:
                    segments.append(f"[Image: {url_value.strip()}]")
            continue

        if block_type in {"input_image", "image"}:
            segments.append("[Inline image attached]")

    if not segments:
        return ""

    return "\n\n".join(segments)


def _normalise_chat_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse list-based content blocks in-place for llama.cpp compatibility."""

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
AVAILABLE_MODELS = [
    {
        "id": "llama-3-8b-instruct",
        "name": "Meta Llama 3.1 8B Instruct",
        "description": (
            "Meta's July 2024 refresh of the 8B instruction-tuned model using the "
            "Q4_K_M quantisation that comfortably fits within a 24 GB RTX 4090."
        ),
        "parameters": "8B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/"
            "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        ),
        "file_name": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "adapters": [
            {
                "id": "llama-3-8b-instruct:alignment",
                "name": "Meta Llama 3.1 8B Alignment Assistant",
                "description": (
                    "Alignment-tuned variant emphasising helpful, honest, and harmless replies "
                    "using constitutional guardrails."
                ),
                "instructions": (
                    "You are the alignment-focused variant of Meta Llama 3.1 8B. Follow the "
                    "provided safety charter to remain helpful, honest, harmless, and to call "
                    "out uncertain answers."
                ),
                "share_base": True,
            }
        ],
    }
]


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

    # Fall back to the v2 catalogue so chat completions can load any model
    # surfaced via the API v2 listings. Import lazily to avoid a hard dependency
    # when the v2 module is unused (e.g. in legacy API v1 only deployments).
    try:
        from api.v2.models import get_models_info as get_v2_models_info  # type: ignore
    except Exception as exc:  # pragma: no cover - defensive logging branch
        log_warning(f"Unable to load API v2 catalogue: {exc}")
        return None

    for entry in get_v2_models_info():
        if entry["id"] == model_id:
            return entry
    return None

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
    logger.info(f"Generating response using model: {model_id}")

    # Validate input
    if not messages:
        raise ModelError("Messages cannot be empty", status_code=400, error_type="invalid_request_error")

    # Validate message format
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
            raise ModelError(
                f"Invalid message format at position {idx}. Each message must have 'role' and 'content' fields.",
                status_code=400,
                error_type="invalid_request_error"
            )

    model_meta = _get_model_metadata(model_id)
    adapter_meta = (model_meta or {}).get("adapter")

    try:
        vision_summary = _build_vision_summary(messages)
        if vision_summary:
            logger.info("Generated inline vision analysis without invoking model")
            messages.append({
                "role": "assistant",
                "content": vision_summary,
            })
            elapsed = time.time() - start_time
            logger.info(f"Response generated in {elapsed:.2f}s (vision analysis)")
            return messages

        # Collapse multi-part text blocks so llama.cpp receives plain strings.
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
