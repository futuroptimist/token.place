"""token.place API package."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from typing import Any

from flask import jsonify, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from limits.util import parse
from prometheus_flask_exporter import PrometheusMetrics

from api.v1 import routes as v1_routes
from api.v2 import routes as v2_routes
from config import get_config

RATE_LIMIT_STORAGE_URI_ENV = "TOKENPLACE_RATE_LIMIT_STORAGE_URI"
LOGGER = logging.getLogger("tokenplace.api")

CONTROL_PLANE_ROUTE_CLASS = "compute_node_control_plane"
CONTROL_PLANE_DEFAULT_LIMIT_ENV = "API_RELAY_CONTROL_PLANE_RATE_LIMIT"
CONTROL_PLANE_IP_LIMIT_ENV = "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT"
CONTROL_PLANE_ROUTE_LIMIT_ENVS = {
    "/api/v1/relay/servers/register": "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT",
    "/api/v1/relay/servers/poll": "API_RELAY_CONTROL_PLANE_POLL_RATE_LIMIT",
    "/api/v1/relay/responses": "API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT",
}
CONTROL_PLANE_ROUTE_DEFAULT_LIMITS = {
    "/api/v1/relay/servers/register": "240/hour",
    "/api/v1/relay/servers/poll": "1200/hour",
    "/api/v1/relay/responses": "1200/hour",
}
CONTROL_PLANE_DEFAULT_LIMIT = "1200/hour"
CONTROL_PLANE_IP_DEFAULT_LIMIT = "10000/hour"

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

# API v1 compute-node control-plane routes bypass the low public user quota and
# are protected by a separate, higher budget below. This keeps healthy desktop
# nodes from being treated like user chat traffic while preserving abuse limits.
RELAY_CONTROL_PLANE_RATE_LIMIT_PATHS = frozenset(CONTROL_PLANE_ROUTE_LIMIT_ENVS)


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


def _resolve_rate_limit_storage_uri() -> str | None:
    raw_value = os.environ.get(RATE_LIMIT_STORAGE_URI_ENV, "")
    storage_uri = raw_value.strip()
    return storage_uri or None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _parse_rate_limit_item(raw_limit: str, fallback: str):
    candidate = (raw_limit or fallback).strip() or fallback
    try:
        return parse(candidate)
    except ValueError:
        LOGGER.warning(
            "rate_limit.invalid_control_plane_limit",
            extra={"limit": candidate, "fallback": fallback},
        )
        return parse(fallback)


def _control_plane_limits_from_env() -> dict[str, dict[str, Any]]:
    route_limits: dict[str, dict[str, Any]] = {}
    inherited_default = os.environ.get(CONTROL_PLANE_DEFAULT_LIMIT_ENV)
    ip_limit = _parse_rate_limit_item(
        os.environ.get(CONTROL_PLANE_IP_LIMIT_ENV, CONTROL_PLANE_IP_DEFAULT_LIMIT),
        CONTROL_PLANE_IP_DEFAULT_LIMIT,
    )
    for route, env_name in CONTROL_PLANE_ROUTE_LIMIT_ENVS.items():
        route_default = CONTROL_PLANE_ROUTE_DEFAULT_LIMITS.get(
            route,
            inherited_default,
        )
        route_limit = _parse_rate_limit_item(
            os.environ.get(env_name) or inherited_default or route_default,
            route_default,
        )
        route_limits[route] = {"identity": route_limit, "ip": ip_limit}
    return route_limits


def _control_plane_identity_for_request(path: str, data: Any) -> tuple[str, str]:
    if isinstance(data, dict):
        if path in {"/api/v1/relay/servers/register", "/api/v1/relay/servers/poll"}:
            server_public_key = data.get("server_public_key")
            if isinstance(server_public_key, str) and server_public_key.strip():
                return "server_public_key", server_public_key.strip()
        if path == "/api/v1/relay/responses":
            client_public_key = data.get("client_public_key")
            request_id = data.get("request_id")
            if isinstance(client_public_key, str) and client_public_key.strip():
                return "client_public_key", client_public_key.strip()
            if isinstance(request_id, str) and request_id.strip():
                return "request_id", request_id.strip()
    return "client_ip", get_remote_address()


def _control_plane_window_seconds(limit_item: Any) -> int:
    return int(limit_item.multiples * limit_item.GRANULARITY.seconds)


def _check_control_plane_bucket(
    buckets: dict[tuple[str, str, str], tuple[int, float]],
    lock: threading.Lock,
    *,
    route: str,
    bucket_kind: str,
    bucket_value: str,
    limit_item: Any,
    now: float,
) -> tuple[bool, int, str]:
    window_seconds = _control_plane_window_seconds(limit_item)
    bucket_key = (route, bucket_kind, bucket_value)
    with lock:
        count, reset_at = buckets.get(bucket_key, (0, now + window_seconds))
        if now >= reset_at:
            count = 0
            reset_at = now + window_seconds
        if count >= int(limit_item.amount):
            retry_after = max(int(reset_at - now), 1)
            return False, retry_after, ":".join(bucket_key)
        buckets[bucket_key] = (count + 1, reset_at)
    return True, 0, ":".join(bucket_key)


def _build_control_plane_rate_limit_response(limit_item: Any, retry_after: int):
    rate_limit_description = str(limit_item)
    payload = {
        "error": {
            "message": (
                f"Rate limit exceeded: {rate_limit_description}. "
                f"Try again in {retry_after} seconds."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
            "param": None,
        }
    }
    response = jsonify(payload)
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def _install_control_plane_rate_limiter(app) -> None:
    route_limits = _control_plane_limits_from_env()
    buckets: dict[tuple[str, str, str], tuple[int, float]] = {}
    lock = threading.Lock()
    app.config["relay_control_plane_rate_limit_buckets"] = buckets
    app.config["relay_control_plane_rate_limit_lock"] = lock

    @app.before_request
    def _enforce_control_plane_rate_limit():
        route = _normalized_path(request.path)
        route_limit = route_limits.get(route)
        if route_limit is None:
            return None

        data = request.get_json(silent=True)
        identity_kind, identity_value = _control_plane_identity_for_request(route, data)
        remote_address = get_remote_address()
        now = time.time()
        checks = [(identity_kind, identity_value, route_limit["identity"])]
        if identity_kind != "client_ip" or identity_value != remote_address:
            checks.append(("client_ip", remote_address, route_limit["ip"]))
        for bucket_kind, bucket_value, limit_item in checks:
            allowed, retry_after, raw_bucket_key = _check_control_plane_bucket(
                buckets,
                lock,
                route=route,
                bucket_kind=bucket_kind,
                bucket_value=bucket_value,
                limit_item=limit_item,
                now=now,
            )
            if allowed:
                continue
            bucket_fingerprint = _fingerprint(raw_bucket_key)
            LOGGER.warning(
                "relay_control_plane_rate_limited",
                extra={
                    "route": route,
                    "route_class": CONTROL_PLANE_ROUTE_CLASS,
                    "limiter_bucket_kind": bucket_kind,
                    "limiter_bucket_fingerprint": bucket_fingerprint,
                    "retry_after": retry_after,
                },
            )
            return _build_control_plane_rate_limit_response(limit_item, retry_after)
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

    @app.errorhandler(RateLimitExceeded)
    def _handle_rate_limit(exc: RateLimitExceeded):
        return _build_rate_limit_response(exc)

    _install_control_plane_rate_limiter(app)

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
