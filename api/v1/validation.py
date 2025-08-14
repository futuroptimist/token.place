"""
Input validation utilities for the token.place API
"""

import json
import base64
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
    """
    Validate that a field contains valid base64 data.

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
        base64.b64decode(value)
    except Exception:
        raise ValidationError(
            f"Invalid base64 encoding for {field}",
            field=field
        )


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

    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValidationError(
                f"messages[{i}] must be an object",
                field="messages"
            )

        validate_required_fields(message, ["role", "content"])
        validate_field_type(message, "role", str)
        validate_field_type(message, "content", str)

        if message.get("role") not in ["system", "user", "assistant", "function"]:
            raise ValidationError(
                f"Invalid role in messages[{i}]: {message.get('role')}",
                field="messages"
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
