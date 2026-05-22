"""token.place API package."""

import os
from flask import jsonify, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from prometheus_flask_exporter import PrometheusMetrics

from api.v1 import routes as v1_routes
from api.v2 import routes as v2_routes


RATE_LIMIT_STORAGE_URI_ENV = "TOKENPLACE_RATE_LIMIT_STORAGE_URI"
_PRODUCTION_ENV_VALUES = {"production"}


def _is_production_environment() -> bool:
    env = os.environ.get("TOKEN_PLACE_ENV", "development").strip().lower()
    return env in _PRODUCTION_ENV_VALUES


def _resolve_rate_limit_storage_uri() -> str | None:
    raw_value = os.environ.get(RATE_LIMIT_STORAGE_URI_ENV, "")
    storage_uri = raw_value.strip()
    return storage_uri or None


def _configure_rate_limit_storage() -> str | None:
    storage_uri = _resolve_rate_limit_storage_uri()

    if _is_production_environment():
        if not storage_uri:
            raise RuntimeError(
                f"{RATE_LIMIT_STORAGE_URI_ENV} must be configured when TOKEN_PLACE_ENV is production "
                "to avoid in-memory rate-limit storage."
            )

        if storage_uri.startswith("memory://"):
            raise RuntimeError(
                f"{RATE_LIMIT_STORAGE_URI_ENV} must not use in-memory backends when TOKEN_PLACE_ENV "
                "is production."
            )

    return storage_uri


def _build_rate_limit_response(exc: RateLimitExceeded):
    """Return an OpenAI-style JSON error response for rate limit breaches."""

    rate_limit_description = str(exc.limit.limit)
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        # Fall back to the configured window length when precise timing is unavailable.
        retry_after = int(exc.limit.limit.get_expiry())

    payload = {
        "error": {
            "message": (
                f"Rate limit exceeded: {rate_limit_description}. Try again in {retry_after} seconds."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
            "param": None,
        }
    }

    response = jsonify(payload)
    response.status_code = exc.code
    response.headers["Retry-After"] = str(retry_after)
    return response


def init_app(app):
    """Initialize the API with the Flask app."""

    limiter_storage_uri = _configure_rate_limit_storage()
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[
            os.environ.get("API_RATE_LIMIT", "60/hour"),
            os.environ.get("API_DAILY_QUOTA", "1000/day"),
        ],
        storage_uri=limiter_storage_uri,
    )

    @app.errorhandler(RateLimitExceeded)
    def _handle_rate_limit(exc: RateLimitExceeded):
        return _build_rate_limit_response(exc)

    PrometheusMetrics(app)
    app.register_blueprint(v1_routes.v1_bp)
    app.register_blueprint(v1_routes.openai_v1_bp)
    app.register_blueprint(v2_routes.v2_bp)
    app.register_blueprint(v2_routes.openai_v2_bp)

    stream_limit_value = os.environ.get("API_STREAM_RATE_LIMIT", "30/minute").strip()

    if stream_limit_value:
        def _stream_limit_exempt() -> bool:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return True
            return not bool(data.get("stream"))

        shared_stream_limit = limiter.shared_limit(
            stream_limit_value,
            scope="chat-completions-stream",
            methods=["POST"],
            per_method=True,
            exempt_when=_stream_limit_exempt,
            override_defaults=False,
        )

        for endpoint in (
            "v2.create_chat_completion",
            "openai_v2.create_chat_completion_openai",
        ):
            view_func = app.view_functions.get(endpoint)
            if view_func and not getattr(view_func, "_stream_rate_limit_attached", False):
                decorated = shared_stream_limit(view_func)
                setattr(decorated, "_stream_rate_limit_attached", True)
                app.view_functions[endpoint] = decorated

    return limiter
