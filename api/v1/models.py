"""
Model management for token.place API v1
This module provides model information and management.
"""

import os
import random
import logging
import time
from typing import Dict, Iterator, List

from llama_cpp import Llama

# Check environment
ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Configure logging based on environment
if ENVIRONMENT != 'prod':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
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

# Check if we're using mock LLM
USE_MOCK_LLM = os.environ.get('USE_MOCK_LLM', '0') == '1'
if ENVIRONMENT != 'prod':
    raw_value = os.environ.get('USE_MOCK_LLM', 'NOT_SET')
    logger.info(
        "API v1 Models module loaded with USE_MOCK_LLM=%s, raw env value: '%s'",
        USE_MOCK_LLM,
        raw_value,
    )

# Available model metadata
AVAILABLE_MODELS = [
    {
        "id": "llama-3-8b-instruct",
        "name": "Meta Llama 3 8B Instruct",
        "description": "Llama 3 8B Instruct model from Meta AI",
        "parameters": "8B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/QuantFactory/Meta-Llama-3-8B-Instruct-GGUF/"
            "resolve/main/Meta-Llama-3-8B-Instruct.Q4_K_M.gguf"
        ),
        "file_name": "Meta-Llama-3-8B-Instruct.Q4_K_M.gguf"
    }
]

# Dictionary mapping model IDs to loaded model instances
_loaded_models = {}

# Shared mock responses so the streaming and non-streaming paths stay aligned
_MOCK_RESPONSES: List[str] = [
    (
        "Mock response: Paris is the capital of France and one of the most visited "
        "cities in the world."
    ),
    (
        "Mock response: The capital of France is Paris, known for its iconic Eiffel "
        "Tower and the Louvre Museum."
    ),
    (
        "Mock response: Paris, the City of Light, serves as France's capital and "
        "cultural center."
    ),
]

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
    # Return a copy of the models list to avoid external modification
    return AVAILABLE_MODELS.copy()

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
        raise ModelError(
            "Model ID cannot be empty",
            status_code=400,
            error_type="invalid_request_error",
        )

    # First check if the model ID exists in available models
    model_meta = next((m for m in AVAILABLE_MODELS if m["id"] == model_id), None)
    if not model_meta:
        available_ids = [m["id"] for m in AVAILABLE_MODELS]
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
    if model_id in _loaded_models:
        logger.info(f"Using cached model instance for {model_id}")
        return _loaded_models[model_id]

    # Load the model from disk if not already loaded
    try:
        model_path = model_meta["file_name"]
        if not os.path.isabs(model_path):
            model_path = os.path.join("models", model_meta["file_name"])
        logger.info(f"Loading model from {model_path}")
        llama = Llama(model_path=model_path)
        _loaded_models[model_id] = llama
        return llama
    except Exception as e:
        logger.exception(f"Failed to load model {model_id}: {e}")
        raise ModelError(
            f"Failed to load model '{model_id}': {str(e)}",
            status_code=500,
            error_type="model_load_error",
        )

def _get_model_and_mode(model_id: str):
    """Return the model instance and whether mock mode is active."""

    model = get_model_instance(model_id)
    mock_mode = USE_MOCK_LLM or model == "MOCK_MODEL"
    logger.debug(f"Using mock_mode={mock_mode} for model_id={model_id}")
    return model, mock_mode


def generate_response(model_id, messages):
    """
    Generate a response using the specified model

    Args:
        model_id: The ID of the model to use
        messages: List of message dictionaries with 'role' and 'content' keys

    Returns:
        list: Updated messages list with the model's response appended

    Raises:
        ModelError: If there's an error with the model or input
    """
    start_time = time.time()
    logger.info(f"Generating response using model: {model_id}")

    # Validate input
    if not messages:
        raise ModelError(
            "Messages cannot be empty",
            status_code=400,
            error_type="invalid_request_error",
        )

    # Validate message format
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
            raise ModelError(
                (
                    "Invalid message format at position {idx}. Each message must have 'role' "
                    "and 'content' fields."
                ),
                status_code=400,
                error_type="invalid_request_error"
            )

    try:
        # Get the model instance (or mock)
        model, mock_mode = _get_model_and_mode(model_id)
        logger.debug(f"Generate response using mock_mode={mock_mode}, model={model}")

        # If we're using a mock model, generate a mock response
        if mock_mode:
            logger.info("Generating mock response")
            assistant_message = {
                "role": "assistant",
                "content": random.choice(_MOCK_RESPONSES)
            }
            messages.append(assistant_message)

            # Log completion time
            elapsed = time.time() - start_time
            logger.info(f"Response generated in {elapsed:.2f}s (mock mode)")
            return messages

        # Generate response with the real model
        logger.info("Generating response with real model")
        response = model.create_chat_completion(messages=messages)

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


def stream_chat_completion(
    model_id: str,
    messages: List[Dict[str, str]],
) -> Iterator[Dict[str, Dict]]:
    """Yield streaming chat completion chunks from the requested model."""

    logger.info(f"Streaming response using model: {model_id}")

    if not messages:
        raise ModelError(
            "Messages cannot be empty",
            status_code=400,
            error_type="invalid_request_error",
        )

    model, mock_mode = _get_model_and_mode(model_id)

    if mock_mode:
        logger.info("Streaming mock response")
        selected = random.choice(_MOCK_RESPONSES)

        # Yield initial assistant role declaration for parity with OpenAI streaming format
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }
            ]
        }

        for token in selected.split():
            yield {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": f"{token} "},
                        "finish_reason": None,
                    }
                ]
            }

        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ]
        }
        return

    try:
        stream = model.create_chat_completion(messages=messages, stream=True)
    except AttributeError as exc:
        logger.exception("Model does not support streaming")
        raise ModelError(
            f"Model '{model_id}' does not support streaming: {exc}",
            status_code=500,
            error_type="model_stream_error",
        )

    try:
        for chunk in stream:
            if not isinstance(chunk, dict):
                continue
            yield chunk
    except Exception as exc:  # pragma: no cover - passthrough for llama.cpp errors
        logger.exception("Error during streaming inference")
        raise ModelError(
            f"Failed to stream response: {exc}",
            status_code=500,
            error_type="model_stream_error",
        )
