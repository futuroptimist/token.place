"""token.place API package."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sys
import time
from typing import Any

from flask import jsonify, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from limits.util import parse
from prometheus_flask_exporter import PrometheusMetrics

from api.v1 import routes as v1_routes
from api.v2 import routes as v2_routes
from config import get_config

RATE_LIMIT_STORAGE_URI_ENV = "TOKENPLACE_RATE_LIMIT_STORAGE_URI"
LOGGER = logging.getLogger("tokenplace.api")

PUBLIC_API_V1_CORS_PREFIXES = ("/api/v1/", "/v1/")
PUBLIC_API_V1_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Max-Age": "600",
}

CONTROL_PLANE_ROUTE_CLASS = "compute_node_control_plane"
CONTROL_PLANE_DEFAULT_LIMIT_ENV = "API_RELAY_CONTROL_PLANE_RATE_LIMIT"
CONTROL_PLANE_IP_LIMIT_ENV = "API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT"
CONTROL_PLANE_ROUTE_LIMIT_ENVS = {
    "/api/v1/relay/servers/register": "API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT",
    "/api/v1/relay/servers/unregister": "API_RELAY_CONTROL_PLANE_UNREGISTER_RATE_LIMIT",
    "/api/v1/relay/servers/poll": "API_RELAY_CONTROL_PLANE_POLL_RATE_LIMIT",
    "/api/v1/relay/responses": "API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT",
}
CONTROL_PLANE_ROUTE_DEFAULT_LIMITS = {
    "/api/v1/relay/servers/register": "240/hour",
    "/api/v1/relay/servers/unregister": "240/hour",
    "/api/v1/relay/servers/poll": "1200/hour",
    "/api/v1/relay/responses": "1200/hour",
}
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

# API v1 compute-node control-plane POST routes bypass the low public user
# quota only after passing the relay-server token boundary used by relay.py.
# They are protected by a separate, higher control-plane budget below, which
# keeps healthy desktop nodes from being treated like user chat traffic while
# preserving abuse limits.
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


def _relay_server_token_is_valid() -> bool:
    """Return True when configured relay tokens are absent or matched."""

    tokens = _load_relay_server_registration_tokens()
    if not tokens:
        return True

    candidate = request.headers.get("X-Relay-Server-Token", "").strip()
    if not candidate:
        return False
    return any(secrets.compare_digest(candidate, token) for token in tokens)


def _relay_server_token_boundary_has_configured_token() -> bool:
    """Return True only when this request matched an explicit relay token."""

    return bool(_load_relay_server_registration_tokens()) and _relay_server_token_is_valid()


def _is_public_api_rate_limit_exempt_path(path: str) -> bool:
    """Return True when a route should not consume the public API quota."""

    normalized_path = _normalized_path(path)
    if request.method == "OPTIONS" and _is_public_api_v1_cors_path(normalized_path):
        return True
    if normalized_path in RATE_LIMIT_EXEMPT_PATHS:
        return True
    if normalized_path in CLIENT_RELAY_READ_RATE_LIMIT_EXEMPT_PATHS:
        return True
    return (
        request.method == "POST"
        and normalized_path in RELAY_CONTROL_PLANE_RATE_LIMIT_PATHS
        and _relay_server_token_is_valid()
    )


def _is_public_api_v1_cors_path(path: str) -> bool:
    """Return True when the fixed public API v1 browser CORS policy applies."""

    return path.startswith(PUBLIC_API_V1_CORS_PREFIXES)


def _install_public_api_v1_cors(app) -> None:
    """Install application-owned wildcard CORS for public API v1 routes."""

    @app.before_request
    def _handle_public_api_v1_preflight():
        if request.method != "OPTIONS" or not _is_public_api_v1_cors_path(request.path):
            return None

        response = app.response_class(status=204)
        for header, value in PUBLIC_API_V1_CORS_HEADERS.items():
            response.headers[header] = value
        return response

    @app.after_request
    def _add_public_api_v1_cors_headers(response):
        if not _is_public_api_v1_cors_path(request.path):
            return response

        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
        if request.method == "OPTIONS":
            for header, value in PUBLIC_API_V1_CORS_HEADERS.items():
                response.headers.setdefault(header, value)
        return response


def _resolve_rate_limit_storage_uri() -> str | None:
    raw_value = os.environ.get(RATE_LIMIT_STORAGE_URI_ENV, "")
    storage_uri = raw_value.strip()
    return storage_uri or None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _response_envelope_identity_for_rate_limit(data: Any) -> tuple[str, str] | None:
    """Return a response identity only after minimal ciphertext-envelope checks.

    relay.py performs the authoritative response validation after this
    before_request hook. Keep this preflight intentionally narrow: it only allows
    the response route-specific budget to use client metadata once the payload has
    the expected ciphertext-envelope shape, so malformed requests cannot burn a
    spoofed victim client bucket before relay.py rejects them.
    """

    if not isinstance(data, dict):
        return None

    forbidden_plaintext_fields = {
        "messages",
        "prompt",
        "input",
        "content",
        "response",
        "text",
    }
    if any(field in data for field in forbidden_plaintext_fields):
        return None

    allowed_fields = {
        "client_public_key",
        "ciphertext",
        "chat_history",
        "cipherkey",
        "iv",
        "request_id",
        "protocol",
        "version",
        "cancel_token",
    }
    if any(field not in allowed_fields for field in data):
        return None
    if not ("ciphertext" in data or "chat_history" in data):
        return None
    if "cipherkey" not in data or "iv" not in data:
        return None

    client_public_key = data.get("client_public_key")
    if isinstance(client_public_key, str) and client_public_key.strip():
        return "client_public_key", client_public_key.strip()
    return None


def _control_plane_identity_for_request(path: str, data: Any) -> tuple[str, str]:
    if path == "/api/v1/relay/responses":
        identity = _response_envelope_identity_for_rate_limit(data)
        if identity is not None:
            return identity
        return "client_ip", get_remote_address()

    if isinstance(data, dict) and path in {
        "/api/v1/relay/servers/register",
        "/api/v1/relay/servers/unregister",
        "/api/v1/relay/servers/poll",
    }:
        server_public_key = data.get("server_public_key")
        if isinstance(server_public_key, str) and server_public_key.strip():
            return "server_public_key", server_public_key.strip()
    return "client_ip", get_remote_address()


def _control_plane_bucket_identifier(
    *,
    route: str,
    bucket_kind: str,
    bucket_value: str,
) -> tuple[str, str, str]:
    """Return bounded, non-raw storage identifiers for a control-plane bucket."""

    return (route, bucket_kind, _fingerprint(bucket_value))


def _control_plane_retry_after(
    rate_limiter: Any, limit_item: Any, identifiers: tuple[str, str, str]
) -> int:
    window = rate_limiter.get_window_stats(limit_item, *identifiers)
    return max(int(window.reset_time - time.time()), 1)


def _control_plane_storage_decr(
    rate_limiter: Any, limit_item: Any, identifiers: tuple[str, str, str]
) -> None:
    """Best-effort rollback for a control-plane bucket hit."""

    storage = getattr(rate_limiter, "storage", None)
    decr = getattr(storage, "decr", None)
    if decr is None:
        LOGGER.warning(
            "rate_limit.control_plane_rollback_unavailable",
            extra={"limiter_bucket_fingerprint": _fingerprint(":".join(identifiers))},
        )
        return
    decr(limit_item.key_for(*identifiers))


def _rollback_control_plane_hits(
    rate_limiter: Any,
    recorded_hits: list[tuple[tuple[str, str, str], Any]],
) -> None:
    """Remove hits recorded for a request that ultimately did not pass all buckets."""

    for identifiers, limit_item in reversed(recorded_hits):
        _control_plane_storage_decr(rate_limiter, limit_item, identifiers)


def _check_control_plane_limits(
    rate_limiter: Any,
    checks: list[tuple[str, str, Any]],
    *,
    route: str,
) -> tuple[bool, int, str, str, Any]:
    """Test all buckets and roll back partial accounting on later rejection.

    The limits backend gives shared storage and TTL for individual buckets. Testing
    every bucket before recording hits avoids charging obviously rejected requests,
    and compensating rollback prevents a later failed bucket from leaving earlier
    buckets charged for a request that returned 429.
    """

    planned_hits: list[tuple[str, tuple[str, str, str], Any]] = []
    for bucket_kind, bucket_value, limit_item in checks:
        identifiers = _control_plane_bucket_identifier(
            route=route,
            bucket_kind=bucket_kind,
            bucket_value=bucket_value,
        )
        if not rate_limiter.test(limit_item, *identifiers):
            retry_after = _control_plane_retry_after(
                rate_limiter, limit_item, identifiers
            )
            return False, retry_after, bucket_kind, ":".join(identifiers), limit_item
        planned_hits.append((bucket_kind, identifiers, limit_item))

    # Record identity buckets before the aggregate client-IP bucket. The limits
    # backend records each hit separately, so a concurrent over-limit identity
    # race must fail before it can consume the NAT-wide IP budget shared by
    # other valid compute nodes. If a later bucket still rejects, the
    # compensating rollback below removes the earlier per-request hit.
    ordered_hits = sorted(
        planned_hits, key=lambda planned_hit: planned_hit[0] == "client_ip"
    )

    recorded_hits: list[tuple[tuple[str, str, str], Any]] = []
    for bucket_kind, identifiers, limit_item in ordered_hits:
        if rate_limiter.hit(limit_item, *identifiers):
            recorded_hits.append((identifiers, limit_item))
            continue

        recorded_hits.append((identifiers, limit_item))
        retry_after = _control_plane_retry_after(rate_limiter, limit_item, identifiers)
        _rollback_control_plane_hits(rate_limiter, recorded_hits)
        return False, retry_after, bucket_kind, ":".join(identifiers), limit_item

    return True, 0, "", "", None


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


def _install_control_plane_rate_limiter(app, storage_uri: str | None) -> None:
    route_limits = _control_plane_limits_from_env()
    control_plane_storage_uri = storage_uri or "memory://"
    control_plane_storage = storage_from_string(control_plane_storage_uri)
    control_plane_rate_limiter = FixedWindowRateLimiter(control_plane_storage)
    app.config["relay_control_plane_rate_limit_storage_uri"] = control_plane_storage_uri
    app.config["relay_control_plane_rate_limiter"] = control_plane_rate_limiter

    @app.before_request
    def _enforce_control_plane_rate_limit():
        route = _normalized_path(request.path)
        route_limit = route_limits.get(route)
        if route_limit is None or request.method != "POST":
            return None

        remote_address = get_remote_address()
        checks: list[tuple[str, str, Any]] = [
            ("client_ip", remote_address, route_limit["ip"])
        ]

        # Only charge high-cardinality identity buckets after passing the same
        # configured-token boundary as relay.py. Invalid-token and tokenless
        # anonymous requests stay keyed to client IP so callers cannot spoof a
        # victim server/client bucket before relay.py validates the request.
        if _relay_server_token_boundary_has_configured_token():
            data = request.get_json(silent=True)
            identity_kind, identity_value = _control_plane_identity_for_request(
                route, data
            )
            if identity_kind != "client_ip" or identity_value != remote_address:
                checks.append((identity_kind, identity_value, route_limit["identity"]))

        allowed, retry_after, bucket_kind, bucket_key, limit_item = (
            _check_control_plane_limits(
                control_plane_rate_limiter,
                checks,
                route=route,
            )
        )
        if allowed:
            return None

        bucket_fingerprint = _fingerprint(bucket_key)
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

    _install_public_api_v1_cors(app)

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

    _install_control_plane_rate_limiter(app, limiter_storage_uri)

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
