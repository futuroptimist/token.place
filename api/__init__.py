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


def _token_place_env() -> str:
    """Return the normalized deployment environment name."""

    return (os.environ.get("TOKEN_PLACE_ENV", "development") or "development").strip().lower()


def _is_production_env() -> bool:
    return _token_place_env() == "production"


def _resolve_rate_limit_storage_uri() -> str | None:
    """Return the limiter storage URI while enforcing production guardrails."""

    storage_uri = (os.environ.get(RATE_LIMIT_STORAGE_URI_ENV, "") or "").strip()
    if storage_uri:
        return storage_uri

    if _is_production_env():
        raise RuntimeError(
            f"{RATE_LIMIT_STORAGE_URI_ENV} must be configured when TOKEN_PLACE_ENV=production. "
            "Refusing to start relay/API with in-memory rate-limit storage."
        )

    return None


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

    limiter_kwargs = {
        "key_func": get_remote_address,
        "app": app,
        "default_limits": [
            os.environ.get("API_RATE_LIMIT", "60/hour"),
            os.environ.get("API_DAILY_QUOTA", "1000/day"),
        ],
    }
    storage_uri = _resolve_rate_limit_storage_uri()
    if storage_uri:
        limiter_kwargs["storage_uri"] = storage_uri

    limiter = Limiter(**limiter_kwargs)

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
