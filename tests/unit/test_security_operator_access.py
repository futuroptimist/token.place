"""Tests for the operator authentication helper."""

from typing import Any, Dict, List

import pytest
from flask import Flask

from api import security


@pytest.fixture
def app() -> Flask:
    """Provide a minimal Flask app for request context management."""

    return Flask(__name__)


@pytest.fixture
def error_calls() -> List[Dict[str, Any]]:
    """Collect calls made to the error formatter for assertions."""

    return []


@pytest.fixture
def error_formatter(error_calls):
    """Return a formatter that records invocations and mimics a response object."""

    class DummyResponse:
        def __init__(self, payload: Dict[str, Any]):
            self.payload = payload
            self.status_code = payload["status_code"]

    def _formatter(message: str, *, error_type: str, code: str, status_code: int):
        call = {
            "message": message,
            "error_type": error_type,
            "code": code,
            "status_code": status_code,
        }
        error_calls.append(call)
        return DummyResponse(call)

    return _formatter


def test_missing_configuration_returns_service_unavailable(app, monkeypatch, error_formatter, error_calls):
    """Requests should be rejected when no operator token is configured."""

    for env_var in security._OPERATOR_TOKEN_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)

    with app.test_request_context("/secure"):
        response = security.ensure_operator_access(error_formatter)

    assert response.status_code == 503
    assert error_calls == [
        {
            "message": "Operator authentication is not configured",
            "error_type": "authentication_error",
            "code": "operator_auth_not_configured",
            "status_code": 503,
        }
    ]


def test_accepts_trimmed_configured_token(app, monkeypatch, error_formatter):
    """Whitespace around configured operator tokens should be ignored."""

    monkeypatch.setenv("TOKEN_PLACE_OPERATOR_TOKEN", "  secret-token  ")
    monkeypatch.delenv("TOKEN_PLACE_KEY_ROTATION_TOKEN", raising=False)
    monkeypatch.delenv("PUBLIC_KEY_ROTATION_TOKEN", raising=False)

    with app.test_request_context(
        "/secure",
        headers={security.OPERATOR_TOKEN_HEADER: "secret-token"},
    ):
        response = security.ensure_operator_access(error_formatter)

    assert response is None


def test_bearer_authorization_header_is_supported(app, monkeypatch, error_formatter, error_calls):
    """The helper should respect Authorization Bearer tokens when headers are missing."""

    monkeypatch.setenv("TOKEN_PLACE_OPERATOR_TOKEN", "api-secret")
    for env_var in ("TOKEN_PLACE_KEY_ROTATION_TOKEN", "PUBLIC_KEY_ROTATION_TOKEN"):
        monkeypatch.delenv(env_var, raising=False)

    with app.test_request_context(
        "/secure",
        headers={"Authorization": "Bearer api-secret"},
    ):
        response = security.ensure_operator_access(error_formatter)

    assert response is None
    assert error_calls == []
