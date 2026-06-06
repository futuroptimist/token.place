"""token.place API package."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from flask import jsonify, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from prometheus_flask_exporter import PrometheusMetrics

from config import get_config
from api.v1 import routes as v1_routes
from api.v2 import routes as v2_routes

RATE_LIMIT_STORAGE_URI_ENV = "TOKENPLACE_RATE_LIMIT_STORAGE_URI"
RELAY_CONTROL_PLANE_RATE_LIMIT_ENV = "TOKENPLACE_RELAY_CONTROL_PLANE_RATE_LIMIT"
RELAY_CONTROL_PLANE_RATE_LIMIT_DEFAULT = "1200/hour"
LOGGER = logging.getLogger("tokenplace.api")

# Paths that support operations, health checking, metrics scraping, and
# diagnostics must not consume the public user API quota. Kubernetes readiness
# probes can call /healthz every few seconds.
RATE_LIMIT_EXEMPT_PATHS = frozenset(
    {
        "/livez",
        "/healthz",
        "/metrics",
        "/relay/diagnostics",
    }
)

# API v1 relay client read/poll routes should not consume the public user quota.
# They do not mutate relay-owned state and are used by clients while waiting for
# an encrypted response envelope or discovering a compute node.
CLIENT_RELAY_READ_RATE_LIMIT_EXEMPT_PATHS = frozenset(
    {
        "/api/v1/relay/servers/next",
        "/api/v1/relay/responses/retrieve",
    }
)

# API v1 compute-node control-plane routes bypass the public user quota and
# receive a higher route-specific budget below. Route handlers still enforce
# strict body validation and registration-token auth when configured.
RELAY_CONTROL_PLANE_RATE_LIMIT_PATHS = frozenset(
    {
        "/api/v1/relay/servers/register",
        "/api/v1/relay/servers/poll",
        "/api/v1/relay/responses",
        "/unregister",
    }
)


def _normalized_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _loaded_relay_server_registration_tokens() -> list[str] | None:
    """Return relay.py's active token snapshot when the relay module is loaded."""

    for module_name in ("relay", "__main__"):
        module = sys.modules.get(module_name)
        if module is None or not hasattr(module, "SERVER_REGISTRATION_TOKENS"):
            continue
        tokens = getattr(module, "SERVER_REGISTRATION_TOKENS")
        if isinstance(tokens, (list, tuple, set, frozenset)):
            return [token for token in tokens if isinstance(token, str) and token]
    return None


def _load_relay_server_registration_tokens() -> list[str]:
    """Return configured relay compute-node tokens from config and env."""

    loaded_tokens = _loaded_relay_server_registration_tokens()
    if loaded_tokens is not None:
        return loaded_tokens

    tokens: list[str] = []
    try:
        configured = get_config().get("relay.server_registration_token")
    except (AttributeError, KeyError, TypeError):
        configured = None
    if isinstance(configured, str):
        tokens.extend(configured.split(","))

    plural_tokens = os.environ.get("TOKEN_PLACE_RELAY_SERVER_TOKENS", "")
    if plural_tokens:
        tokens.extend(plural_tokens.replace("\n", ",").split(","))

    singular_token = os.environ.get("TOKEN_PLACE_RELAY_SERVER_TOKEN", "")
    if singular_token:
        tokens.append(singular_token)

    normalized = [
        candidate.strip() for candidate in tokens if isinstance(candidate, str)
    ]
    return [token for token in normalized if token]


def _is_public_api_rate_limit_exempt_path(path: str) -> bool:
    """Return True when a route should not consume the public API quota."""

    normalized_path = _normalized_path(path)
    return (
        normalized_path in RATE_LIMIT_EXEMPT_PATHS
        or normalized_path in CLIENT_RELAY_READ_RATE_LIMIT_EXEMPT_PATHS
        or normalized_path in RELAY_CONTROL_PLANE_RATE_LIMIT_PATHS
    )


def _safe_rate_limit_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _relay_control_plane_rate_limit_key() -> str:
    """Return a route-aware safe rate-limit key for compute-node control traffic."""

    normalized_path = _normalized_path(request.path)
    data = request.get_json(silent=True)
    server_public_key = None
    if isinstance(data, dict):
        candidate = data.get("server_public_key")
        if isinstance(candidate, str) and candidate.strip():
            server_public_key = candidate.strip()

    if server_public_key:
        principal = f"server:{_safe_rate_limit_fingerprint(server_public_key)}"
    else:
        token = request.headers.get("X-Relay-Server-Token", "").strip()
        if token:
            principal = f"token:{_safe_rate_limit_fingerprint(token)}"
        else:
            principal = f"ip:{get_remote_address()}"

    key = f"compute_node_control_plane:{normalized_path}:{principal}"
    request.environ["tokenplace.rate_limit_route_class"] = "compute_node_control_plane"
    request.environ["tokenplace.rate_limit_bucket_fingerprint"] = (
        _safe_rate_limit_fingerprint(key)
    )
    return key


def _is_not_relay_control_plane_route() -> bool:
    return _normalized_path(request.path) not in RELAY_CONTROL_PLANE_RATE_LIMIT_PATHS


def _retry_after_seconds(exc: RateLimitExceeded) -> int:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        retry_after = int(exc.limit.limit.get_expiry())
    return int(retry_after)


def _resolve_rate_limit_storage_uri() -> str | None:
    raw_value = os.environ.get(RATE_LIMIT_STORAGE_URI_ENV, "")
    storage_uri = raw_value.strip()
    return storage_uri or None


def _build_rate_limit_response(exc: RateLimitExceeded):
    """Return an OpenAI-style JSON error response for rate limit breaches."""

    rate_limit_description = str(exc.limit.limit)
    retry_after = _retry_after_seconds(exc)

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

    limiter_storage_uri = _resolve_rate_limit_storage_uri()
    limiter_kwargs = {
        "default_limits": [
            os.environ.get("API_RATE_LIMIT", "60/hour"),
            os.environ.get("API_DAILY_QUOTA", "1000/day"),
        ],
        "default_limits_exempt_when": (
            lambda: _is_public_api_rate_limit_exempt_path(request.path)
        ),
    }
    if limiter_storage_uri:
        limiter_kwargs["storage_uri"] = limiter_storage_uri

    limiter = Limiter(
        get_remote_address,
        app=app,
        **limiter_kwargs,
    )

    control_plane_limit_value = os.environ.get(
        RELAY_CONTROL_PLANE_RATE_LIMIT_ENV,
        RELAY_CONTROL_PLANE_RATE_LIMIT_DEFAULT,
    ).strip()
    if control_plane_limit_value:

        @app.before_request
        @limiter.limit(
            control_plane_limit_value,
            key_func=_relay_control_plane_rate_limit_key,
            per_method=True,
            methods=["POST"],
            exempt_when=_is_not_relay_control_plane_route,
            override_defaults=False,
        )
        def _enforce_relay_control_plane_rate_limit():
            return None

    @app.errorhandler(RateLimitExceeded)
    def _handle_rate_limit(exc: RateLimitExceeded):
        retry_after = _retry_after_seconds(exc)
        route_class = request.environ.get(
            "tokenplace.rate_limit_route_class", "public_api"
        )
        bucket_fingerprint = request.environ.get(
            "tokenplace.rate_limit_bucket_fingerprint"
        )
        LOGGER.warning(
            "api.rate_limit_exceeded",
            extra={
                "route": _normalized_path(request.path),
                "route_class": route_class,
                "limiter_bucket_fingerprint": bucket_fingerprint,
                "retry_after": retry_after,
            },
        )
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
            if view_func and not getattr(
                view_func, "_stream_rate_limit_attached", False
            ):
                decorated = shared_stream_limit(view_func)
                setattr(decorated, "_stream_rate_limit_attached", True)
                app.view_functions[endpoint] = decorated

    return limiter
