"""
Input validation utilities for the token.place API
"""

import json
import base64
import re
from typing import Dict, List, Union, Any, Optional, Tuple

class ValidationError(Exception):
    """Exception raised for validation errors."""
    def __init__(self, message: str, field: Optional[str] = None, code: str = "invalid_request_error"):
        self.message = message
        self.field = field
        self.code = code
        super().__init__(self.message)


def validate_required_fields(data: Dict[str, Any], required_fields: List[str]) -> None:
    """
    Validate that all required fields are present in the data.

    Args:
        data: The data to validate
        required_fields: List of required field names

    Raises:
        ValidationError: If any required field is missing
    """
    for field in required_fields:
        if field not in data:
            raise ValidationError(f"Missing required parameter: {field}", field=field)


def validate_field_type(data: Dict[str, Any], field: str, expected_type: type,
                      allow_none: bool = False) -> None:
    """
    Validate that a field is of the expected type.

    Args:
        data: The data containing the field
        field: The field name to validate
        expected_type: The expected type
        allow_none: Whether to allow None value

    Raises:
        ValidationError: If field is of wrong type
    """
    if field not in data:
        return

    value = data[field]

    if value is None and allow_none:
        return

    if not isinstance(value, expected_type):
        type_name = expected_type.__name__
        raise ValidationError(
            f"Invalid type for {field}: expected {type_name}",
            field=field
        )


def validate_string_length(data: Dict[str, Any], field: str,
                         min_length: Optional[int] = None,
                         max_length: Optional[int] = None) -> None:
    """
    Validate string length within specified bounds.

    Args:
        data: The data containing the field
        field: The field name to validate
        min_length: Minimum allowed length
        max_length: Maximum allowed length

    Raises:
        ValidationError: If string length is outside bounds
    """
    if field not in data or not isinstance(data[field], str):
        return

    value = data[field]
    length = len(value)

    if min_length is not None and length < min_length:
        raise ValidationError(
            f"{field} must be at least {min_length} characters",
            field=field
        )

    if max_length is not None and length > max_length:
        raise ValidationError(
            f"{field} must be at most {max_length} characters",
            field=field
        )


def validate_base64(data: Dict[str, Any], field: str) -> None:
    """Validate that a field contains valid base64 data.

    Uses strict decoding to reject any characters outside the base64 alphabet.

    Args:
        data: The data containing the field
        field: The field name to validate

    Raises:
        ValidationError: If field does not contain valid base64
    """
    if field not in data or not isinstance(data[field], str):
        return

    value = data[field]

    try:
        # Check if it can be decoded
        base64.b64decode(value, validate=True)
    except Exception:
        raise ValidationError(
            f"Invalid base64 encoding for {field}",
            field=field
        )


_DIMENSION_RANGE: Tuple[int, int] = (32, 1024)
_SIZE_PATTERN = re.compile(r"^(?P<width>\d{2,4})x(?P<height>\d{2,4})$")


def _ensure_dimension(value: Any, field: str) -> int:
    if not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer", field=field)

    low, high = _DIMENSION_RANGE
    if value < low or value > high:
        raise ValidationError(
            f"{field} must be between {low} and {high} pixels",
            field=field,
        )

    return value


def _parse_size_field(size_value: Any) -> Tuple[int, int]:
    if not isinstance(size_value, str):
        raise ValidationError("size must be a string formatted as WIDTHxHEIGHT", field="size")

    match = _SIZE_PATTERN.match(size_value.strip().lower())
    if not match:
        raise ValidationError("size must follow the WIDTHxHEIGHT format", field="size")

    width = int(match.group("width"))
    height = int(match.group("height"))
    width = _ensure_dimension(width, "size")
    height = _ensure_dimension(height, "size")
    return width, height


def _derive_dimensions(payload: Dict[str, Any]) -> Tuple[int, int]:
    if "size" in payload and payload["size"] is not None:
        return _parse_size_field(payload["size"])

    width = payload.get("width")
    height = payload.get("height")

    if width is None and height is None:
        return 512, 512

    if width is None:
        raise ValidationError("Missing required parameter: width", field="width")
    if height is None:
        raise ValidationError("Missing required parameter: height", field="height")

    width = _ensure_dimension(width, "width")
    height = _ensure_dimension(height, "height")
    return width, height


def validate_image_generation_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalise payloads for the image generation endpoint."""

    if not isinstance(data, dict):
        raise ValidationError("Invalid request body: expected a JSON object")

    validate_required_fields(data, ["prompt"])
    validate_field_type(data, "prompt", str)

    prompt = data.get("prompt", "")
    prompt = prompt.strip()
    if not prompt:
        raise ValidationError("prompt must be a non-empty string", field="prompt")

    validate_field_type(data, "seed", int, allow_none=True)
    seed = data.get("seed")
    if isinstance(seed, int) and seed < 0:
        raise ValidationError("seed must be a non-negative integer", field="seed")

    width, height = _derive_dimensions(data)

    return {
        "prompt": prompt,
        "seed": seed,
        "width": width,
        "height": height,
    }


def validate_json_string(data: Dict[str, Any], field: str) -> None:
    """
    Validate that a field contains a valid JSON string.

    Args:
        data: The data containing the field
        field: The field name to validate

    Raises:
        ValidationError: If field does not contain valid JSON
    """
    if field not in data or not isinstance(data[field], str):
        return

    value = data[field]

    try:
        json.loads(value)
    except json.JSONDecodeError:
        raise ValidationError(
            f"Invalid JSON in {field}",
            field=field
        )


def validate_chat_messages(messages: List[Dict[str, Any]]) -> None:
    """
    Validate chat messages format.

    Args:
        messages: List of message objects

    Raises:
        ValidationError: If messages format is invalid
    """
    if not isinstance(messages, list):
        raise ValidationError("messages must be an array", field="messages")

    if not messages:
        raise ValidationError("messages must contain at least one item", field="messages")

    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValidationError(
                f"messages[{i}] must be an object",
                field="messages"
            )

        validate_required_fields(message, ["role", "content"])
        validate_field_type(message, "role", str)

        if message.get("role") not in ["system", "user", "assistant", "function"]:
            raise ValidationError(
                f"Invalid role in messages[{i}]: {message.get('role')}",
                field="messages"
            )

        content = message.get("content")
        if isinstance(content, str):
            continue

        if isinstance(content, list):
            if not content:
                raise ValidationError(
                    f"messages[{i}].content must contain at least one item",
                    field="messages",
                )

            for j, item in enumerate(content):
                if not isinstance(item, dict):
                    raise ValidationError(
                        f"messages[{i}].content[{j}] must be an object",
                        field="messages",
                    )

                item_type = item.get("type")
                if item_type in {"input_text", "text"}:
                    text_value = item.get("text")
                    if not isinstance(text_value, str) or not text_value:
                        raise ValidationError(
                            f"messages[{i}].content[{j}].text must be a non-empty string",
                            field="messages",
                        )
                    continue

                if item_type == "image_url":
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict):
                        url_value = image_url.get("url")
                    else:
                        url_value = image_url
                    if not isinstance(url_value, str) or not url_value:
                        raise ValidationError(
                            f"messages[{i}].content[{j}].image_url.url must be a non-empty string",
                            field="messages",
                        )
                    continue

                if item_type == "input_image":
                    image_payload = item.get("image") or item.get("image_url")
                    if not isinstance(image_payload, dict):
                        raise ValidationError(
                            f"messages[{i}].content[{j}].image must be an object",
                            field="messages",
                        )

                    encoded = (
                        image_payload.get("b64_json")
                        or image_payload.get("base64")
                        or image_payload.get("data")
                    )

                    if not isinstance(encoded, str) or not encoded:
                        raise ValidationError(
                            f"messages[{i}].content[{j}].image must include base64 data",
                            field="messages",
                        )

                    try:
                        base64.b64decode(encoded, validate=True)
                    except Exception as exc:  # pragma: no cover - defensive branch
                        raise ValidationError(
                            f"messages[{i}].content[{j}].image must contain valid base64 data",
                            field="messages",
                        ) from exc

                    continue

                raise ValidationError(
                    f"Unsupported content type in messages[{i}]: {item_type}",
                    field="messages",
                )

            continue

        raise ValidationError(
            f"messages[{i}].content must be a string or array of content blocks",
            field="messages",
        )


def validate_encrypted_request(data: Dict[str, Any]) -> None:
    """
    Validate an encrypted API request.

    Args:
        data: The request data

    Raises:
        ValidationError: If request format is invalid
    """
    validate_required_fields(data, ["client_public_key", "messages"])
    validate_field_type(data, "client_public_key", str)

    # Check if messages has required encryption fields
    messages = data.get("messages", {})
    if not isinstance(messages, dict):
        raise ValidationError("messages must be an object for encrypted requests", field="messages")

    validate_required_fields(messages, ["ciphertext", "cipherkey", "iv"])

    # Validate base64 encoding of encrypted fields
    for field in ["ciphertext", "cipherkey", "iv"]:
        if field in messages:
            validate_base64(messages, field)


def validate_model_name(model_name: str, available_models: List[str]) -> None:
    """
    Validate that the requested model is available.

    Args:
        model_name: The model name to validate
        available_models: List of available model names

    Raises:
        ValidationError: If model is not found
    """
    if model_name not in available_models:
        raise ValidationError(
            f"Model '{model_name}' not found",
            field="model",
            code="model_not_found"
        )
