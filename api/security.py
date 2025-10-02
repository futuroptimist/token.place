"""Security helpers for guarding privileged API endpoints."""

from __future__ import annotations

import os
from typing import Callable, Optional

from flask import Response, request

OperatorAuthFormatter = Callable[..., Response]

# Headers / environment variable names that can supply the operator token.
OPERATOR_TOKEN_HEADER = "X-Token-Place-Operator"
_OPERATOR_TOKEN_ENV_VARS = (
    "TOKEN_PLACE_OPERATOR_TOKEN",
    "TOKEN_PLACE_KEY_ROTATION_TOKEN",
    "PUBLIC_KEY_ROTATION_TOKEN",
)


def _expected_operator_token() -> Optional[str]:
    """Return the configured operator token, if present."""

    for env_var in _OPERATOR_TOKEN_ENV_VARS:
        token = os.getenv(env_var)
        if token:
            return token
    return None


def ensure_operator_access(
    format_error_response: OperatorAuthFormatter,
    log_warning: Optional[Callable[[str], None]] = None,
) -> Optional[Response]:
    """Validate that the request is authorized to perform privileged actions."""

    expected_token = _expected_operator_token()
    if not expected_token:
        if log_warning:
            log_warning("Operator token not configured; rejecting privileged request")
        return format_error_response(
            "Operator authentication is not configured",
            error_type="authentication_error",
            code="operator_auth_not_configured",
            status_code=503,
        )

    provided_token = request.headers.get(OPERATOR_TOKEN_HEADER)
    if not provided_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided_token = auth_header.split(" ", 1)[1]

    if provided_token != expected_token:
        if log_warning:
            log_warning("Operator token missing or invalid for privileged request")
        return format_error_response(
            "Operator token missing or invalid",
            error_type="authentication_error",
            code="operator_token_invalid",
            status_code=401,
        )

    return None

