from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlparse

from release_metadata import get_release_metadata

from flask import Flask, Response, g, jsonify, request, send_from_directory
from prometheus_client import Counter, REGISTRY
from werkzeug.serving import make_server

# Logging --------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class JsonFormatter(logging.Formatter):
    """Render log records as structured JSON."""

    _RESERVED = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 - logging API
        payload: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value

        return json.dumps(payload, default=_json_default)


def setup_logging() -> logging.Logger:
    """Configure application logging with JSON formatting."""

    logger = logging.getLogger("tokenplace.relay")
    if logger.handlers:
        return logger

    log_level = os.environ.get("TOKENPLACE_LOG_LEVEL", "INFO").upper()
    logger.setLevel(log_level)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.captureWarnings(True)

    return logger


LOGGER = setup_logging()


DRAINING = threading.Event()
_ORIGINAL_SIGNAL_HANDLERS: dict[int, Any] = {}
STATIC_DIR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX_HTML_PATH = os.path.join(STATIC_DIR_PATH, "index.html")
VUE_SCRIPT_PLACEHOLDER = "__TOKENPLACE_VUE_SCRIPT_SRC__"
RELEASE_METADATA_PLACEHOLDER = '{"environment":"dev","label":"dev dev","version":"dev"}'
RELEASE_BADGE_TEXT_PLACEHOLDER = "dev dev"
VUE_DEV_SCRIPT_SRC = "https://cdn.jsdelivr.net/npm/vue@2.6.14/dist/vue.js"
VUE_PROD_SCRIPT_SRC = "https://cdn.jsdelivr.net/npm/vue@2.6.14/dist/vue.min.js"


def _frontend_mode() -> str:
    """Resolve frontend asset mode for relay-served static HTML."""

    mode = os.environ.get("TOKENPLACE_FRONTEND_MODE", "production").strip().lower()
    if mode in {"dev", "development"}:
        return "development"
    return "production"


def _vue_script_src_for_mode(mode: str) -> str:
    return VUE_DEV_SCRIPT_SRC if mode == "development" else VUE_PROD_SCRIPT_SRC


def _render_index_html(host: str | None = None) -> str:
    with open(INDEX_HTML_PATH, encoding="utf-8") as index_file:
        html = index_file.read()
    metadata = get_release_metadata(host)
    return (
        html.replace(VUE_SCRIPT_PLACEHOLDER, _vue_script_src_for_mode(_frontend_mode()))
        .replace(
            RELEASE_METADATA_PLACEHOLDER,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )
        .replace(RELEASE_BADGE_TEXT_PLACEHOLDER, metadata["label"])
    )


def _handle_shutdown_signal(signum: int, frame: Any) -> None:
    """Mark the process as draining and defer to the original handler."""

    if not DRAINING.is_set():
        LOGGER.info("relay.shutdown.signal", extra={"signal": signum})
        DRAINING.set()

    original = _ORIGINAL_SIGNAL_HANDLERS.get(signum)
    if callable(original) and original not in (signal.SIG_DFL, signal.SIG_IGN, _handle_shutdown_signal):
        original(signum, frame)
        return

    if original in (signal.SIG_DFL, None):
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _install_shutdown_handlers() -> None:
    """Install signal handlers that record draining state for readiness probes."""

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)
            _ORIGINAL_SIGNAL_HANDLERS[sig] = previous
            signal.signal(sig, _handle_shutdown_signal)
        except (OSError, RuntimeError, ValueError, AttributeError):
            LOGGER.debug("relay.signal_handler.install_failed", extra={"signal": sig})


_install_shutdown_handlers()


def configure_app_logging(flask_app: Flask) -> None:
    """Ensure Flask's logger shares the JSON formatter."""

    flask_app.logger.handlers = []
    for handler in LOGGER.handlers:
        flask_app.logger.addHandler(handler)
    flask_app.logger.setLevel(LOGGER.level)
    flask_app.logger.propagate = False


def _configure_mock_mode(enable_mock: bool) -> None:
    should_enable = enable_mock or os.environ.get("USE_MOCK_LLM") == "1"
    if not should_enable:
        return

    if os.environ.get("USE_MOCK_LLM") != "1":
        os.environ["USE_MOCK_LLM"] = "1"
        LOGGER.info("mock.llm.enabled", extra={"use_mock_llm": True})


def _enforce_api_v1_distributed_guardrail() -> None:
    """Optionally force API v1 distributed routing for guardrail runs."""

    enforce = os.environ.get("TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED", "0").strip().lower()
    if enforce not in {"1", "true", "yes", "on"}:
        return

    if not os.environ.get("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "").strip():
        os.environ["TOKENPLACE_API_V1_COMPUTE_PROVIDER"] = "distributed"
    if not os.environ.get("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "").strip():
        os.environ["TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK"] = "0"

    LOGGER.info(
        "relay.api_v1_distributed_guardrail.enabled",
        extra={
            "provider_mode": os.environ.get("TOKENPLACE_API_V1_COMPUTE_PROVIDER"),
            "fallback": os.environ.get("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK"),
            "has_distributed_url": bool(os.environ.get("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "").strip()),
        },
    )


def _build_cli_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="token.place relay server", add_help=add_help)
    parser.add_argument(
        "--port",
        type=int,
        default=5010,
        help="Port to run the relay server on",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind the relay server",
    )
    parser.add_argument(
        "--use_mock_llm",
        action="store_true",
        help="Use mock LLM for testing",
    )
    return parser


def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments when running the relay directly."""

    parser = _build_cli_parser()
    return parser.parse_args(argv)


def _detect_mock_flag(argv: list[str]) -> bool:
    parser = _build_cli_parser(add_help=False)
    try:
        args, _ = parser.parse_known_args(argv)
    except SystemExit:
        return False
    return bool(getattr(args, "use_mock_llm", False))


_configure_mock_mode(_detect_mock_flag(sys.argv[1:]))
_enforce_api_v1_distributed_guardrail()


GPU_HOST_ENV = "TOKENPLACE_GPU_HOST"
GPU_PORT_ENV = "TOKENPLACE_GPU_PORT"
UPSTREAM_URL_ENV = "TOKENPLACE_RELAY_UPSTREAM_URL"
PUBLIC_BASE_URL_ENV = "TOKENPLACE_RELAY_PUBLIC_URL"
PUBLIC_BASE_URL_COMPAT_ENV = "TOKEN_PLACE_RELAY_PUBLIC_URL"
PUBLIC_BASE_URL_FALLBACK_ENV = "RELAY_PUBLIC_URL"
REQUIRE_UPSTREAM_HEALTH_ENV = "TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH"
RELAY_UPSTREAMS_ENV = "TOKEN_PLACE_RELAY_UPSTREAMS"
RELAY_UPSTREAM_COMPAT_ENV = "PERSONAL_GAMING_PC_URL"


def _env_truthy(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _normalise_upstream_server_pool(servers: List[str]) -> List[str]:
    """Return a normalised upstream server pool for source comparisons."""

    normalised: List[str] = []
    for raw_server in servers:
        if not isinstance(raw_server, str):
            continue
        value = raw_server.strip().rstrip("/")
        if value:
            normalised.append(value.lower())
    return normalised


def _has_explicit_relay_upstream_config(configured_servers: List[str] | None = None) -> bool:
    """Return whether relay upstream URLs were explicitly configured by env or config."""

    for env_name in (RELAY_UPSTREAMS_ENV, RELAY_UPSTREAM_COMPAT_ENV, UPSTREAM_URL_ENV):
        raw_value = os.environ.get(env_name, "")
        if not raw_value.strip():
            continue
        if env_name == UPSTREAM_URL_ENV:
            return True
        try:
            from config import Config

            parsed_upstreams = Config()._parse_relay_upstreams(raw_value)
        except Exception:
            parsed_upstreams = []
        if parsed_upstreams:
            return True
    if configured_servers is not None:
        default_legacy_pool = _normalise_upstream_server_pool(["https://token.place"])
        current_pool = _normalise_upstream_server_pool(configured_servers)
        if current_pool and current_pool != default_legacy_pool:
            return True
    return False


def _load_upstream_config() -> Dict[str, Any]:
    upstream_override = os.environ.get(UPSTREAM_URL_ENV)
    parsed_host = None
    parsed_port: int | None = None

    if upstream_override:
        try:
            parsed = urlparse(upstream_override)
        except ValueError:
            parsed = None
        if parsed and parsed.hostname:
            parsed_host = parsed.hostname
            parsed_port = parsed.port

    host = (
        os.environ.get(GPU_HOST_ENV)
        or os.environ.get("GPU_SERVER_HOST")
        or parsed_host
        or "gpu-server"
    )
    port_source = (
        os.environ.get(GPU_PORT_ENV)
        or os.environ.get("GPU_SERVER_PORT")
        or (str(parsed_port) if parsed_port is not None else None)
        or "3000"
    )
    port = int(port_source)
    upstream_url = upstream_override or f"http://{host}:{port}"
    return {
        "gpu_host": host,
        "gpu_port": port,
        "upstream_url": upstream_url,
    }


UPSTREAM_CONFIG = _load_upstream_config()


def _load_public_base_url() -> str | None:
    """Return the externally reachable relay URL when configured."""

    for env_var in (PUBLIC_BASE_URL_ENV, PUBLIC_BASE_URL_COMPAT_ENV, PUBLIC_BASE_URL_FALLBACK_ENV):
        candidate = os.environ.get(env_var, "")
        if not candidate:
            continue

        trimmed = candidate.strip().rstrip("/")
        if trimmed:
            return trimmed

    return None


def create_app() -> Flask:
    """Instantiate and configure the Flask application."""

    flask_app = Flask(__name__, static_folder=None)
    configure_app_logging(flask_app)
    flask_app.config.update(UPSTREAM_CONFIG)
    try:
        from config import get_config

        configured_servers = get_config().get("relay.server_pool", []) or []
    except (ImportError, AttributeError, KeyError, TypeError) as exc:
        LOGGER.debug("relay.config.load_failed", exc_info=exc)
        configured_servers = []
    flask_app.config["relay_configured_servers"] = list(configured_servers)
    public_base_url = _load_public_base_url()
    if public_base_url:
        flask_app.config["public_base_url"] = public_base_url

    from api import init_app  # Imported lazily to honor mock-mode configuration

    init_app(flask_app)
    LOGGER.info(
        "relay.app.initialized",
        extra={
            "upstream": flask_app.config.get("upstream_url"),
            "public_base_url": public_base_url,
        },
    )
    return flask_app


app = create_app()


def _get_request_counter() -> Counter:
    metric_name = "tokenplace_relay_requests_total"
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(metric_name)
    if existing is not None:
        return existing  # type: ignore[return-value]
    return Counter(
        metric_name,
        "Total HTTP requests processed by token.place relay",
        ["method", "endpoint", "status"],
    )


REQUEST_COUNTER = _get_request_counter()


def _load_server_registration_tokens():
    """Return configured relay server registration tokens."""

    tokens: list[str] = []
    try:
        from config import get_config

        configured = get_config().get('relay.server_registration_token')
        if isinstance(configured, str):
            tokens.extend(configured.split(','))
    except (ImportError, AttributeError, KeyError, TypeError):
        tokens = []

    plural_tokens = os.environ.get('TOKEN_PLACE_RELAY_SERVER_TOKENS', '')
    if plural_tokens:
        tokens.extend(plural_tokens.replace("\n", ",").split(','))
    singular_token = os.environ.get('TOKEN_PLACE_RELAY_SERVER_TOKEN', '')
    if singular_token:
        tokens.append(singular_token)

    normalized = [candidate.strip() for candidate in tokens if isinstance(candidate, str)]
    return [token for token in normalized if token]


SERVER_REGISTRATION_TOKENS = _load_server_registration_tokens()


def _validate_server_registration():
    """Ensure relay compute nodes present the expected token when configured."""

    if not SERVER_REGISTRATION_TOKENS:
        return None

    provided = request.headers.get('X-Relay-Server-Token', '')
    candidate = provided.strip()
    if candidate:
        matched = False
        for token in SERVER_REGISTRATION_TOKENS:
            if secrets.compare_digest(candidate, token):
                matched = True
        if matched:
            return None

    return jsonify({
        'error': {
            'message': 'Missing or invalid relay server token',
            'code': 401,
        }
    }), 401


known_servers = {}
server_round_robin_lock = threading.RLock()
server_round_robin_next_index = 0
API_V1_SERVER_MARKER = "api_v1_registered"
client_inference_requests = {}
client_responses = {}
client_responses_lock = threading.Lock()
client_pending_request_ids = {}
client_pending_request_ids_lock = threading.Lock()
client_terminal_request_ids = {}
client_terminal_request_ids_lock = threading.Lock()
TERMINAL_REQUEST_TTL_SECONDS = float(os.getenv("TOKENPLACE_TERMINAL_REQUEST_TTL_SECONDS", "300"))
PENDING_REQUEST_TTL_SECONDS = float(os.getenv("TOKENPLACE_PENDING_REQUEST_TTL_SECONDS", "300"))
client_inference_requests_lock = threading.Lock()
client_inference_requests_changed = threading.Condition(client_inference_requests_lock)
api_v1_in_flight_requests_lock = threading.Lock()
streaming_sessions = {}
streaming_sessions_by_client = {}
stream_lock = threading.Lock()

IGNORED_LOG_ENDPOINTS = {"livez", "healthz", "metrics"}
SERVER_STALE_SECONDS_ENV = "TOKEN_PLACE_RELAY_SERVER_TTL_SECONDS"
DEFAULT_SERVER_STALE_SECONDS = 30
API_V1_POLL_WAIT_SECONDS_ENV = "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS"
DEFAULT_API_V1_POLL_WAIT_SECONDS = 10
API_V1_LEASE_SECONDS_ENV = "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS"
DEFAULT_API_V1_LEASE_SECONDS = 30
API_V1_IN_FLIGHT_TTL_SECONDS_ENV = "TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS"


def _server_ping_age_seconds(last_ping: Any) -> float:
    if isinstance(last_ping, datetime):
        age = (datetime.now() - last_ping).total_seconds()
        return max(age, 0.0)
    if isinstance(last_ping, (int, float)):
        return max(time.time() - float(last_ping), 0.0)
    return float("inf")


def _server_stale_seconds() -> int:
    raw = os.environ.get(SERVER_STALE_SECONDS_ENV, str(DEFAULT_SERVER_STALE_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SERVER_STALE_SECONDS
    return max(value, 1)


def _api_v1_poll_wait_seconds() -> float:
    raw = os.environ.get(API_V1_POLL_WAIT_SECONDS_ENV, str(DEFAULT_API_V1_POLL_WAIT_SECONDS))
    try:
        wait_seconds = float(raw)
    except ValueError:
        return float(DEFAULT_API_V1_POLL_WAIT_SECONDS)
    if wait_seconds < 0:
        return 0.0
    return wait_seconds


def _api_v1_lease_seconds() -> int:
    raw = os.environ.get(API_V1_LEASE_SECONDS_ENV, str(DEFAULT_API_V1_LEASE_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_API_V1_LEASE_SECONDS
    return max(value, 1)




def _api_v1_in_flight_ttl_seconds() -> float:
    raw = os.environ.get(API_V1_IN_FLIGHT_TTL_SECONDS_ENV)
    if raw is None:
        return max(float(_api_v1_lease_seconds()), 1.0)
    try:
        value = float(raw)
    except ValueError:
        return max(float(_api_v1_lease_seconds()), 1.0)
    return max(value, 1.0)

def _pop_next_api_v1_request(public_key: str):
    queued_requests = client_inference_requests.get(public_key, [])
    if not queued_requests:
        return None

    first_request = None

    def _is_legacy_ciphertext_payload(payload):
        return all(key in payload for key in ('client_public_key', 'chat_history', 'cipherkey', 'iv'))

    for idx, candidate in enumerate(queued_requests):
        if bool(candidate.get('e2ee_v1')):
            first_request = queued_requests.pop(idx)
            break

    if first_request is None:
        while queued_requests:
            candidate = queued_requests[0]
            if _is_legacy_ciphertext_payload(candidate):
                first_request = queued_requests.pop(0)
                break
            queued_requests.pop(0)

    if first_request is not None and bool(first_request.get('e2ee_v1')):
        queued_requests[:] = [item for item in queued_requests if bool(item.get('e2ee_v1'))]

    if not queued_requests:
        client_inference_requests.pop(public_key, None)

    return first_request


def _evict_stale_servers() -> list[str]:
    _prune_terminal_requests()
    _expire_stale_pending_requests()
    default_stale_after = _server_stale_seconds()
    now_monotonic = time.monotonic()
    evicted: list[str] = []
    with server_round_robin_lock:
        server_items = list(known_servers.items())
        for server_public_key, payload in server_items:
            polling_until = payload.get("polling_until_monotonic")
            if isinstance(polling_until, (int, float)) and polling_until > now_monotonic:
                continue
            with api_v1_in_flight_requests_lock:
                in_flight_requests = payload.get("api_v1_in_flight_requests")
                if isinstance(in_flight_requests, dict):
                    for request_id, entry in list(in_flight_requests.items()):
                        if not isinstance(request_id, str) or not request_id:
                            continue
                        expires_at = entry.get("expires_at") if isinstance(entry, dict) else entry
                        if isinstance(expires_at, (int, float)) and expires_at > now_monotonic:
                            continue
                        if in_flight_requests.get(request_id) == entry:
                            in_flight_requests.pop(request_id, None)

                    has_active_in_flight_requests = any(
                        isinstance((entry.get("expires_at") if isinstance(entry, dict) else entry), (int, float))
                        and (entry.get("expires_at") if isinstance(entry, dict) else entry) > now_monotonic
                        for entry in in_flight_requests.values()
                    )
                    if has_active_in_flight_requests:
                        continue
                    payload.pop("api_v1_in_flight_requests", None)

            in_flight_until = payload.get("api_v1_in_flight_until_monotonic")
            if isinstance(in_flight_until, (int, float)) and in_flight_until > now_monotonic:
                continue
            payload.pop("api_v1_in_flight_until_monotonic", None)
            payload.pop("api_v1_in_flight_request_id", None)
            stale_after = payload.get("last_ping_duration", default_stale_after)
            if not isinstance(stale_after, (int, float)):
                stale_after = default_stale_after
            stale_after = max(float(stale_after), 1.0)
            if _server_ping_age_seconds(payload.get("last_ping")) <= stale_after:
                continue
            if _unregister_server(server_public_key):
                evicted.append(server_public_key)
    return evicted


def _live_server_diagnostics(*, api_v1_only: bool = False) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for server_public_key, payload in list(known_servers.items()):
        if api_v1_only and not bool(payload.get(API_V1_SERVER_MARKER)):
            continue
        diagnostics.append({
            "server_public_key": server_public_key,
            "age_seconds": round(_server_ping_age_seconds(payload.get("last_ping")), 3),
            "next_ping_in_x_seconds": payload.get("last_ping_duration"),
            "queue_depth": len(client_inference_requests.get(server_public_key, [])),
        })
    diagnostics.sort(key=lambda node: node["server_public_key"])
    return diagnostics


def _api_v1_round_robin_keys() -> list[str]:
    """Return API v1-capable compute node keys in registration order."""

    return [
        server_public_key
        for server_public_key, payload in known_servers.items()
        if bool(payload.get(API_V1_SERVER_MARKER))
    ]


def _remove_known_server(server_public_key: str) -> bool:
    """Remove a known server while preserving the API v1 round-robin cursor."""

    global server_round_robin_next_index
    with server_round_robin_lock:
        api_v1_ordered_keys = _api_v1_round_robin_keys()
        if server_public_key not in known_servers:
            return False

        is_api_v1_candidate = server_public_key in api_v1_ordered_keys
        if is_api_v1_candidate:
            removed_position = api_v1_ordered_keys.index(server_public_key)
            current_position = server_round_robin_next_index % len(api_v1_ordered_keys)

        known_servers.pop(server_public_key, None)

        if is_api_v1_candidate:
            remaining_count = len(api_v1_ordered_keys) - 1
            if remaining_count <= 0:
                server_round_robin_next_index = 0
            elif removed_position < current_position:
                server_round_robin_next_index = (current_position - 1) % remaining_count
            else:
                server_round_robin_next_index = current_position % remaining_count

        return True


def _unregister_server(server_public_key: str) -> bool:
    """Remove a compute node and associated per-server queue/session state."""

    _record_api_v1_server_unregistered(server_public_key)
    in_flight_requests = {}
    with server_round_robin_lock:
        server_payload = known_servers.get(server_public_key)
        if isinstance(server_payload, dict):
            with api_v1_in_flight_requests_lock:
                raw_in_flight = server_payload.get("api_v1_in_flight_requests")
                if isinstance(raw_in_flight, dict):
                    in_flight_requests = dict(raw_in_flight)

    removed = _remove_known_server(server_public_key)
    dropped_requests = []
    with client_inference_requests_changed:
        dropped_requests = list(client_inference_requests.pop(server_public_key, []) or [])
        client_inference_requests_changed.notify_all()

    cancelled_queue_depth = 0
    for item in dropped_requests:
        if not isinstance(item, dict) or not bool(item.get("e2ee_v1")):
            continue
        _cancel_api_v1_request(
            item.get("client_public_key"),
            item.get("request_id"),
            status="cancelled",
            reason="server_unregistered",
        )
        cancelled_queue_depth += 1

    for request_id, entry in in_flight_requests.items():
        if not isinstance(request_id, str) or not request_id:
            continue
        if not isinstance(entry, dict):
            continue
        _cancel_api_v1_request(
            entry.get("client_public_key"),
            request_id,
            status="cancelled",
            reason="server_unregistered",
        )
        cancelled_queue_depth += 1

    with stream_lock:
        stale_session_ids = [
            session_id
            for session_id, session in streaming_sessions.items()
            if session.get("server_public_key") == server_public_key
        ]
        for session_id in stale_session_ids:
            session = streaming_sessions.pop(session_id, None)
            if not session:
                continue
            client_public_key = session.get("client_public_key")
            if client_public_key:
                mapped_session_id = streaming_sessions_by_client.get(client_public_key)
                if mapped_session_id == session_id:
                    streaming_sessions_by_client.pop(client_public_key, None)

    LOGGER.info(
        "server.unregistered",
        extra={
            "server_fingerprint": _safe_key_fingerprint(server_public_key),
            "removed": removed,
            "cancelled_queue_depth": cancelled_queue_depth,
        },
    )
    return removed


def _can_resolve_gpu_host(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except socket.gaierror:
        return False


@app.before_request
def _record_request_start():
    g.request_start_time = time.time()
    g.request_id = request.headers.get("X-Request-Id") or secrets.token_hex(8)


@app.after_request
def _log_request(response: Response):
    endpoint = request.endpoint or "unknown"
    status_code = str(response.status_code)

    try:
        REQUEST_COUNTER.labels(request.method, endpoint, status_code).inc()
    except Exception:  # pragma: no cover - defensive metric increment
        LOGGER.debug(
            "metrics.increment_failed",
            extra={"endpoint": endpoint, "status": status_code},
        )

    duration = None
    if hasattr(g, "request_start_time"):
        duration = max(time.time() - g.request_start_time, 0)

    if endpoint not in IGNORED_LOG_ENDPOINTS:
        LOGGER.info(
            "http.request",
            extra={
                "http_method": request.method,
                "http_path": request.path,
                "http_status": int(status_code),
                "duration_ms": round((duration or 0) * 1000, 2),
                "request_id": getattr(g, "request_id", None),
                "user_agent": request.headers.get("User-Agent"),
            },
        )

    if getattr(g, "request_id", None):
        response.headers.setdefault("X-Request-Id", g.request_id)

    if endpoint == "metrics":
        response.headers.setdefault("Cache-Control", "no-store")

    return response


@app.route("/healthz", methods=["GET"])
def healthz():
    _evict_stale_servers()
    gpu_host = app.config.get("gpu_host")
    configured_servers = app.config.get("relay_configured_servers", [])
    require_upstream_health = _env_truthy(REQUIRE_UPSTREAM_HEALTH_ENV, default=False)
    explicit_upstream_config = _has_explicit_relay_upstream_config(configured_servers)
    relay_only_mode = (not require_upstream_health) and (not explicit_upstream_config)
    status = {
        "status": "ok",
        "upstream": app.config.get("upstream_url"),
        "upstreamHealthRequired": require_upstream_health,
        "relayOnly": relay_only_mode,
        "gpuHost": gpu_host,
        "knownServers": len(known_servers),
        "registeredServers": _live_server_diagnostics(),
    }
    status["configuredUpstreamServers"] = configured_servers
    status["legacyConfiguredUpstreamServers"] = [] if explicit_upstream_config else configured_servers
    if app.config.get("public_base_url"):
        status["publicBaseUrl"] = app.config["public_base_url"]

    if DRAINING.is_set():
        status["status"] = "draining"
        status.setdefault("details", {})["shutdown"] = True
        response = jsonify(status)
        response.status_code = 503
        response.headers["Retry-After"] = "0"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    if require_upstream_health and gpu_host and not _can_resolve_gpu_host(gpu_host):
        status["status"] = "degraded"
        status.setdefault("details", {})["gpuHostResolution"] = "failed"
        LOGGER.warning(
            "healthz.resolution_failed",
            extra={"gpu_host": gpu_host, "require_upstream_health": require_upstream_health},
        )
        return jsonify(status), 503

    if not known_servers:
        status.setdefault("details", {})["knownServers"] = "empty"

    return jsonify(status)


@app.route("/livez", methods=["GET"])
def livez():
    return jsonify({"status": "alive"})
def _register_stream_session(server_public_key, client_public_key):
    """Create or replace the streaming session for a client/server pair."""

    if not client_public_key:
        return None

    session_id = secrets.token_urlsafe(16)
    now = time.time()
    session = {
        'session_id': session_id,
        'server_public_key': server_public_key,
        'client_public_key': client_public_key,
        'chunks': [],
        'status': 'open',
        'created_at': now,
        'updated_at': now,
    }

    with stream_lock:
        existing_session_id = streaming_sessions_by_client.get(client_public_key)
        if existing_session_id:
            streaming_sessions.pop(existing_session_id, None)
        streaming_sessions[session_id] = session
        streaming_sessions_by_client[client_public_key] = session_id

    return session


def _append_stream_chunk(session_id, chunk, final=False):
    """Append a streaming chunk to the active session."""

    with stream_lock:
        session = streaming_sessions.get(session_id)
        if not session:
            return False

        session['chunks'].append(chunk)
        session['updated_at'] = time.time()
        if final:
            session['status'] = 'closed'

    return True


def _pop_stream_chunks_for_client(client_public_key):
    """Retrieve queued streaming chunks for a client."""

    with stream_lock:
        session_id = streaming_sessions_by_client.get(client_public_key)
        if not session_id:
            return None

        session = streaming_sessions.get(session_id)
        if not session:
            streaming_sessions_by_client.pop(client_public_key, None)
            return None

        chunks = list(session['chunks'])
        session['chunks'].clear()
        session['updated_at'] = time.time()
        final = session['status'] == 'closed'

        if final:
            streaming_sessions.pop(session_id, None)
            streaming_sessions_by_client.pop(client_public_key, None)

    return session_id, chunks, final

@app.route('/')
def index():
    response = Response(_render_index_html(request.host), mimetype='text/html')
    response.last_modified = os.path.getmtime(INDEX_HTML_PATH)
    response.add_etag()
    response.make_conditional(request)
    return response

@app.route('/api/v1/meta', methods=['GET'])
def api_v1_meta():
    return jsonify(get_release_metadata(request.host))


@app.route('/api/v1/version', methods=['GET'])
def api_v1_version():
    return jsonify(get_release_metadata(request.host))


# Generic route for serving static files
@app.route('/static/<path:path>')
def serve_static(path):
    if path == 'index.html':
        return index()
    return send_from_directory(STATIC_DIR_PATH, path)



def _legacy_routes_enabled() -> bool:
    return str(os.getenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _legacy_route_deprecated_response(route_name: str):
    return jsonify({
        "error": {
            "message": f"Legacy relay endpoint '{route_name}' is deprecated. Use API v1 relay E2EE routes.",
            "code": "legacy_relay_endpoint_deprecated",
            "deprecated": True,
        }
    }), 410


def _select_round_robin_server_key() -> tuple[str | None, dict[str, Any], dict[str, Any] | None]:
    """Select and snapshot the next API v1 compute node in registration-order round-robin order."""

    global server_round_robin_next_index
    with server_round_robin_lock:
        ordered_keys = _api_v1_round_robin_keys()
        if not ordered_keys:
            server_round_robin_next_index = 0
            return None, {
                "eligible_count": 0,
                "round_robin_index": 0,
                "round_robin_position": None,
            }, None

        eligible_count = len(ordered_keys)
        round_robin_index = server_round_robin_next_index % eligible_count
        server_public_key = ordered_keys[round_robin_index]
        server_payload = dict(known_servers[server_public_key])
        server_round_robin_next_index = (round_robin_index + 1) % eligible_count
        return server_public_key, {
            "eligible_count": eligible_count,
            "round_robin_index": server_round_robin_next_index,
            "round_robin_position": round_robin_index,
        }, server_payload


def _select_next_server_payload(*, api_v1: bool = False):
    global server_round_robin_next_index
    evicted = _evict_stale_servers()
    with server_round_robin_lock:
        if not known_servers:
            server_round_robin_next_index = 0
            if api_v1:
                return jsonify({
                    'error': {
                        'message': 'No registered compute nodes are available on this relay.',
                        'code': 'no_registered_compute_nodes',
                    }
                }), 503
            return jsonify({'error': {'message': 'No servers available','code': 503}}), 503
        if not api_v1:
            server_public_key = secrets.choice(list(known_servers.keys()))
            server_payload = dict(known_servers[server_public_key])
            return jsonify({'server_public_key': server_payload['public_key']})

    server_public_key, selection, server_payload = _select_round_robin_server_key()
    if not server_public_key or server_payload is None:
        return jsonify({
            'error': {
                'message': 'No registered compute nodes are available on this relay.',
                'code': 'no_registered_compute_nodes',
            }
        }), 503

    LOGGER.info(
        "relay.server_selected",
        extra={
            "server_fingerprint": _safe_key_fingerprint(server_public_key),
            "selection_policy": "registration_order_round_robin",
            "round_robin_index": selection.get("round_robin_index"),
            "round_robin_position": selection.get("round_robin_position"),
            "eligible_count": selection.get("eligible_count"),
            "evicted_stale_count": len(evicted),
            "api_v1": api_v1,
        },
    )
    return jsonify({'server_public_key': server_payload['public_key']})

@app.route('/next_server', methods=['GET'])
def next_server():
    """
    Endpoint for clients to get the next server to send a request to.
    This allows the relay to load-balance requests across heterogeneous servers and clients in a random manner.

    Returns: a json response with the following keys:
        - server_public_key: the RSA-2048 public key of the selected server to send a request to
        - error: an error message with a message and a code
    """
    if not _legacy_routes_enabled():
        return _legacy_route_deprecated_response('/next_server')
    return _select_next_server_payload()


@app.route('/api/v1/relay/servers/next', methods=['GET'])
def api_v1_relay_servers_next():
    """Get a registered compute node public key for API v1 encrypted relay requests."""
    return _select_next_server_payload(api_v1=True)


@app.route('/relay/diagnostics', methods=['GET'])
def relay_diagnostics():
    """Live diagnostics for legacy and API v1 relay registered compute nodes."""
    _evict_stale_servers()
    live_nodes = _live_server_diagnostics()
    api_v1_live_nodes = _live_server_diagnostics(api_v1_only=True)
    configured_servers = app.config.get("relay_configured_servers", [])
    require_upstream_health = _env_truthy(REQUIRE_UPSTREAM_HEALTH_ENV, default=False)
    explicit_upstream_config = _has_explicit_relay_upstream_config(configured_servers)
    diagnostics = {
        "relay_only": (not require_upstream_health) and (not explicit_upstream_config),
        "upstream_health_required": require_upstream_health,
        "registered_compute_nodes": live_nodes,
        "total_registered_compute_nodes": len(live_nodes),
        "api_v1_registered_compute_nodes": api_v1_live_nodes,
        "total_api_v1_registered_compute_nodes": len(api_v1_live_nodes),
        "configured_upstream_servers": configured_servers,
        "legacy_configured_upstream_servers": [] if explicit_upstream_config else configured_servers,
    }
    response = jsonify(diagnostics)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/relay/api/v1/chat/completions', methods=['POST'])
def relay_api_v1_chat_completions():
    """Fail closed for relay-dispatched API v1 plaintext chat payloads."""

    return jsonify(
        {
            'error': {
                'type': 'service_unavailable_error',
                'code': 'distributed_api_v1_relay_disabled',
                'message': (
                    'Distributed relay API v1 chat completions are disabled pending '
                    'an end-to-end encrypted relay design.'
                ),
            }
        }
    ), 503


@app.route('/relay/api/v1/source', methods=['POST'])
def relay_api_v1_source():
    """Fail closed for relay-dispatched API v1 plaintext completion responses."""

    return jsonify(
        {
            'error': {
                'type': 'service_unavailable_error',
                'code': 'distributed_api_v1_relay_disabled',
                'message': (
                    'Distributed relay API v1 source dispatch is disabled pending '
                    'an end-to-end encrypted relay design.'
                ),
            }
        }
    ), 503

def _extract_ciphertext_envelope(payload, *, require_server_key=False):
    if not isinstance(payload, dict):
        return None, ('Invalid request data', 400)

    required = ['cipherkey', 'iv']
    has_ciphertext = 'ciphertext' in payload
    has_chat_history = 'chat_history' in payload
    if not has_ciphertext and not has_chat_history:
        return None, ('Invalid request data', 400)
    if require_server_key:
        required.insert(0, 'server_public_key')
    missing = [field for field in required if field not in payload]
    if missing:
        return None, ('Invalid request data', 400)

    envelope = {
        'client_public_key': payload.get('client_public_key'),
        'chat_history': payload.get('ciphertext', payload.get('chat_history')),
        'ciphertext': payload.get('ciphertext', payload.get('chat_history')),
        'cipherkey': payload['cipherkey'],
        'iv': payload['iv'],
    }
    if require_server_key:
        envelope['server_public_key'] = payload['server_public_key']
    if 'request_id' in payload:
        envelope['request_id'] = payload['request_id']
    if 'protocol' in payload:
        envelope['protocol'] = payload['protocol']
    if 'version' in payload:
        envelope['version'] = payload['version']
    if 'cancel_token' in payload:
        envelope['cancel_token'] = payload['cancel_token']
    return envelope, None




def _payload_has_plaintext_fields(payload):
    if not isinstance(payload, dict):
        return False
    forbidden_plaintext_fields = {"messages", "prompt", "input", "content", "response", "text"}
    return any(field in payload for field in forbidden_plaintext_fields)


def _payload_has_unexpected_relay_fields(payload, *, allow_server_public_key):
    """Reject unknown top-level keys so relay envelopes stay ciphertext-only by schema."""
    if not isinstance(payload, dict):
        return False
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
    if allow_server_public_key:
        allowed_fields.add("server_public_key")
    return any(field not in allowed_fields for field in payload)


def _payload_has_unexpected_faucet_fields(payload):
    """Reject unknown top-level keys on the legacy faucet envelopes."""
    if not isinstance(payload, dict):
        return False
    allowed_fields = {
        "client_public_key",
        "server_public_key",
        "chat_history",
        "cipherkey",
        "iv",
        "stream",
    }
    return any(field not in allowed_fields for field in payload)


def _queue_client_response(client_public_key, envelope):
    """Queue an encrypted response while preserving per-request retrieval."""
    with client_responses_lock:
        existing = client_responses.get(client_public_key)
        if existing is None:
            client_responses[client_public_key] = envelope
            return
        if isinstance(existing, list):
            existing.append(envelope)
            return
        client_responses[client_public_key] = [existing, envelope]


def _safe_key_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    return f"{value[:8]}...{value[-4:]}" if len(value) > 12 else "short-key"


_ALLOWED_API_V1_TERMINAL_STATUSES = {"cancelled", "expired"}
_API_V1_UNREGISTER_TOMBSTONE_TTL_SECONDS = 300.0
api_v1_recently_unregistered_servers: dict[str, float] = {}
api_v1_recently_unregistered_servers_lock = threading.Lock()


def _record_api_v1_server_unregistered(server_public_key: str) -> None:
    if isinstance(server_public_key, str) and server_public_key:
        with api_v1_recently_unregistered_servers_lock:
            api_v1_recently_unregistered_servers[server_public_key] = time.monotonic()


def _api_v1_server_was_recently_unregistered(server_public_key: str) -> bool:
    if not isinstance(server_public_key, str) or not server_public_key:
        return False
    now = time.monotonic()
    with api_v1_recently_unregistered_servers_lock:
        expired_keys = [
            key
            for key, removed_at in api_v1_recently_unregistered_servers.items()
            if now - removed_at > _API_V1_UNREGISTER_TOMBSTONE_TTL_SECONDS
        ]
        for key in expired_keys:
            api_v1_recently_unregistered_servers.pop(key, None)
        return server_public_key in api_v1_recently_unregistered_servers


_ALLOWED_API_V1_TERMINAL_REASONS = {
    "cancelled",
    "expired",
    "requester_cancelled",
    "requester_gave_up",
    "provider_timeout",
    "pending_request_ttl_exceeded",
    "server_unregistered",
}


def _sanitize_terminal_status(value):
    return value if isinstance(value, str) and value in _ALLOWED_API_V1_TERMINAL_STATUSES else "cancelled"


def _sanitize_terminal_reason(value, status):
    return value if isinstance(value, str) and value in _ALLOWED_API_V1_TERMINAL_REASONS else status


def _mark_request_terminal(client_public_key, request_id, *, status="cancelled", reason=None):
    if not client_public_key or not request_id:
        return
    status = _sanitize_terminal_status(status)
    reason = _sanitize_terminal_reason(reason, status)
    expires_at = time.time() + max(TERMINAL_REQUEST_TTL_SECONDS, 1.0)
    _prune_terminal_requests(now=time.time())
    with client_terminal_request_ids_lock:
        terminal_ids = client_terminal_request_ids.setdefault(client_public_key, {})
        terminal_ids[request_id] = {"status": status, "reason": reason, "expires_at": expires_at}


def _prune_terminal_requests(*, now=None):
    now = time.time() if now is None else now
    with client_terminal_request_ids_lock:
        for client_public_key, terminal_ids in list(client_terminal_request_ids.items()):
            if not isinstance(terminal_ids, dict):
                client_terminal_request_ids.pop(client_public_key, None)
                continue
            for request_id, terminal in list(terminal_ids.items()):
                expires_at = terminal.get("expires_at") if isinstance(terminal, dict) else None
                if not isinstance(expires_at, (int, float)) or expires_at <= now:
                    terminal_ids.pop(request_id, None)
            if not terminal_ids:
                client_terminal_request_ids.pop(client_public_key, None)


def _get_terminal_request(client_public_key, request_id):
    if not client_public_key or not request_id:
        return None
    now = time.time()
    with client_terminal_request_ids_lock:
        terminal_ids = client_terminal_request_ids.get(client_public_key)
        if not terminal_ids:
            return None
        terminal = terminal_ids.get(request_id)
        if not isinstance(terminal, dict):
            terminal_ids.pop(request_id, None)
            return None
        expires_at = terminal.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at <= now:
            terminal_ids.pop(request_id, None)
            if not terminal_ids:
                client_terminal_request_ids.pop(client_public_key, None)
            return None
        return terminal


def _remove_request_from_server_queues(client_public_key, request_id):
    removed = 0
    with client_inference_requests_changed:
        for server_public_key, queued_requests in list(client_inference_requests.items()):
            if not isinstance(queued_requests, list):
                continue
            kept = []
            removed_for_server = 0
            for item in queued_requests:
                if (
                    isinstance(item, dict)
                    and item.get("client_public_key") == client_public_key
                    and item.get("request_id") == request_id
                ):
                    removed += 1
                    removed_for_server += 1
                    LOGGER.info(
                        "relay.api_v1.request_removed_from_queue",
                        extra={
                            "server_fingerprint": _safe_key_fingerprint(server_public_key),
                            "request_id": request_id,
                        },
                    )
                    continue
                kept.append(item)
            if kept:
                client_inference_requests[server_public_key] = kept
            elif removed_for_server:
                client_inference_requests.pop(server_public_key, None)
        if removed:
            client_inference_requests_changed.notify_all()
    return removed


def _remove_client_responses_for_request(client_public_key, request_id):
    if not client_public_key or not request_id:
        return 0
    removed = 0
    with client_responses_lock:
        queued = client_responses.get(client_public_key)
        if queued is None:
            return 0
        if isinstance(queued, list):
            kept = []
            for candidate in queued:
                if isinstance(candidate, dict) and candidate.get("request_id") == request_id:
                    removed += 1
                    continue
                kept.append(candidate)
            if not kept:
                client_responses.pop(client_public_key, None)
            elif len(kept) == 1:
                client_responses[client_public_key] = kept[0]
            else:
                client_responses[client_public_key] = kept
            return removed
        if isinstance(queued, dict) and queued.get("request_id") == request_id:
            client_responses.pop(client_public_key, None)
            return 1
    return 0


def _has_client_response_for_request(client_public_key, request_id):
    if not client_public_key or not request_id:
        return False
    with client_responses_lock:
        queued = client_responses.get(client_public_key)
        if isinstance(queued, list):
            return any(
                isinstance(candidate, dict) and candidate.get("request_id") == request_id
                for candidate in queued
            )
        return isinstance(queued, dict) and queued.get("request_id") == request_id


def _in_flight_entry_matches_client(entry, client_public_key):
    if isinstance(entry, dict):
        return entry.get("client_public_key") == client_public_key
    return False


def _cancel_api_v1_request(client_public_key, request_id, *, status="cancelled", reason=None):
    if not client_public_key or not request_id:
        return 0
    status = _sanitize_terminal_status(status)
    reason = _sanitize_terminal_reason(reason, status)
    removed = _remove_request_from_server_queues(client_public_key, request_id)
    _remove_client_responses_for_request(client_public_key, request_id)
    _clear_pending_request(client_public_key, request_id)
    _mark_request_terminal(client_public_key, request_id, status=status, reason=reason)
    with server_round_robin_lock:
        with api_v1_in_flight_requests_lock:
            for server_payload in known_servers.values():
                in_flight_requests = server_payload.get("api_v1_in_flight_requests")
                if not isinstance(in_flight_requests, dict) or request_id not in in_flight_requests:
                    continue
                if _in_flight_entry_matches_client(in_flight_requests.get(request_id), client_public_key):
                    in_flight_requests.pop(request_id, None)
                    if not in_flight_requests:
                        server_payload.pop("api_v1_in_flight_requests", None)
    LOGGER.info(
        "relay.api_v1.request_cancelled",
        extra={
            "client_fingerprint": _safe_key_fingerprint(client_public_key),
            "request_id": request_id,
            "status": status,
            "reason": reason or status,
            "removed_from_queue": removed,
        },
    )
    return removed


def _mark_request_pending(client_public_key, request_id, *, cancel_token=None):
    if not client_public_key or not request_id:
        return
    with client_pending_request_ids_lock:
        pending_ids = client_pending_request_ids.setdefault(client_public_key, {})
        if isinstance(cancel_token, str) and cancel_token:
            pending_ids[request_id] = {
                "queued_at": time.time(),
                "cancel_token": cancel_token,
            }
        else:
            pending_ids[request_id] = time.time()


def _clear_pending_request(client_public_key, request_id):
    if not client_public_key or not request_id:
        return
    with client_pending_request_ids_lock:
        pending_ids = client_pending_request_ids.get(client_public_key)
        if not pending_ids:
            return
        pending_ids.pop(request_id, None)
        if not pending_ids:
            client_pending_request_ids.pop(client_public_key, None)


def _clear_pending_requests_for_queued_items(queued_items):
    """Clear pending markers for queued API v1 envelopes that are being dropped."""
    for item in queued_items or []:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("e2ee_v1")):
            continue
        _clear_pending_request(item.get("client_public_key"), item.get("request_id"))


def _pending_request_entry_is_expired(pending_entry, *, now=None):
    if PENDING_REQUEST_TTL_SECONDS <= 0:
        return False
    now = time.time() if now is None else now
    queued_at = pending_entry.get("queued_at") if isinstance(pending_entry, dict) else pending_entry
    try:
        return (now - float(queued_at)) > PENDING_REQUEST_TTL_SECONDS
    except (TypeError, ValueError):
        return True


def _expire_pending_request_if_stale(client_public_key, request_id):
    if not client_public_key or not request_id:
        return False
    with client_pending_request_ids_lock:
        pending_entry = client_pending_request_ids.get(client_public_key, {}).get(request_id)
    if pending_entry is None or not _pending_request_entry_is_expired(pending_entry):
        return False
    _cancel_api_v1_request(
        client_public_key,
        request_id,
        status="expired",
        reason="pending_request_ttl_exceeded",
    )
    return True


def _is_request_pending(client_public_key, request_id):
    if not client_public_key or not request_id:
        return False
    with client_pending_request_ids_lock:
        pending_entry = client_pending_request_ids.get(client_public_key, {}).get(request_id)
    if pending_entry is None:
        return False
    if _pending_request_entry_is_expired(pending_entry):
        _expire_pending_request_if_stale(client_public_key, request_id)
        return False
    return True


def _get_pending_cancel_token(client_public_key, request_id):
    if not client_public_key or not request_id:
        return None
    with client_pending_request_ids_lock:
        pending_entry = client_pending_request_ids.get(client_public_key, {}).get(request_id)
    if isinstance(pending_entry, dict):
        token = pending_entry.get("cancel_token")
        return token if isinstance(token, str) and token else None
    return None


def _cancel_token_for_queued_or_in_flight_request(client_public_key, request_id):
    token = _get_pending_cancel_token(client_public_key, request_id)
    if token:
        return token
    with client_inference_requests_changed:
        for queued_requests in client_inference_requests.values():
            if not isinstance(queued_requests, list):
                continue
            for item in queued_requests:
                if (
                    isinstance(item, dict)
                    and item.get("client_public_key") == client_public_key
                    and item.get("request_id") == request_id
                ):
                    token = item.get("cancel_token")
                    return token if isinstance(token, str) and token else None
    with server_round_robin_lock:
        with api_v1_in_flight_requests_lock:
            for server_payload in known_servers.values():
                in_flight_requests = server_payload.get("api_v1_in_flight_requests")
                if not isinstance(in_flight_requests, dict):
                    continue
                entry = in_flight_requests.get(request_id)
                if isinstance(entry, dict) and entry.get("client_public_key") == client_public_key:
                    token = entry.get("cancel_token")
                    return token if isinstance(token, str) and token else None
    return None


def _expire_stale_pending_requests():
    if PENDING_REQUEST_TTL_SECONDS <= 0:
        return
    now = time.time()
    expired = []
    with client_pending_request_ids_lock:
        for client_public_key, pending_ids in list(client_pending_request_ids.items()):
            if not isinstance(pending_ids, dict):
                client_pending_request_ids.pop(client_public_key, None)
                continue
            for request_id, pending_entry in list(pending_ids.items()):
                if _pending_request_entry_is_expired(pending_entry, now=now):
                    expired.append((client_public_key, request_id))
    for client_public_key, request_id in expired:
        if _has_client_response_for_request(client_public_key, request_id):
            continue
        _cancel_api_v1_request(
            client_public_key,
            request_id,
            status="expired",
            reason="pending_request_ttl_exceeded",
        )


def _pop_client_response(client_public_key, request_id=None):
    """Pop a queued encrypted response, optionally matching API v1 request id."""
    with client_responses_lock:
        if client_public_key not in client_responses:
            return None

        queued = client_responses[client_public_key]
        if isinstance(queued, list):
            if request_id:
                for idx, candidate in enumerate(queued):
                    if candidate.get('request_id') == request_id:
                        response = queued.pop(idx)
                        if not queued:
                            client_responses.pop(client_public_key, None)
                        elif len(queued) == 1:
                            client_responses[client_public_key] = queued[0]
                        _clear_pending_request(client_public_key, response.get('request_id'))
                        return response
                return None
            response = queued.pop(0)
            if not queued:
                client_responses.pop(client_public_key, None)
            elif len(queued) == 1:
                client_responses[client_public_key] = queued[0]
            _clear_pending_request(client_public_key, response.get('request_id'))
            return response

        if request_id and queued.get('request_id') != request_id:
            return None
        response = client_responses.pop(client_public_key)
        _clear_pending_request(client_public_key, response.get('request_id'))
        return response


@app.route('/api/v1/relay/servers/register', methods=['POST'])
def api_v1_relay_servers_register():
    """Register or heartbeat a compute node for API v1 encrypted relay workloads."""
    global server_round_robin_next_index
    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    public_key = data.get('server_public_key')
    if not public_key:
        return jsonify({'error': {'message': 'Missing server public key', 'code': 400}}), 400

    lease_seconds = _api_v1_lease_seconds()
    with server_round_robin_lock:
        existing_payload = known_servers.get(public_key)
        existing_api_v1_count = len(_api_v1_round_robin_keys())
        if existing_payload and existing_payload.get(API_V1_SERVER_MARKER):
            existing_payload['last_ping'] = datetime.now()
            log_event = "server.reregister"
        else:
            if existing_api_v1_count == 0:
                server_round_robin_next_index = 0
            if existing_payload:
                existing_payload['last_ping'] = datetime.now()
                known_servers.pop(public_key, None)
                known_servers[public_key] = existing_payload
            else:
                known_servers[public_key] = {
                    'public_key': public_key,
                    'last_ping': datetime.now(),
                    'last_ping_duration': lease_seconds,
                }
            known_servers[public_key][API_V1_SERVER_MARKER] = True
            log_event = "server.registered"
        known_servers[public_key]['last_ping_duration'] = lease_seconds
        known_servers[public_key][API_V1_SERVER_MARKER] = True
    LOGGER.info(log_event, extra={"server_public_key": public_key})

    return jsonify({
        'next_ping_in_x_seconds': lease_seconds,
        'poll_wait_seconds': _api_v1_poll_wait_seconds(),
    }), 200


def _handle_server_unregister_request(*, invalid_error_shape: str = "api_v1"):
    """Handle idempotent compute-node unregister requests for relay routes."""

    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        if invalid_error_shape == "legacy":
            return jsonify({'error': 'Invalid request data'}), 400
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    public_key = data.get('server_public_key')
    if not isinstance(public_key, str) or not public_key.strip():
        if invalid_error_shape == "legacy":
            return jsonify({'error': 'Invalid public key'}), 400
        return jsonify({'error': {'message': 'Invalid public key', 'code': 400}}), 400

    removed = _unregister_server(public_key)
    return jsonify({'message': 'Server unregistered', 'removed': removed}), 200


@app.route('/api/v1/relay/servers/unregister', methods=['POST'])
def api_v1_relay_servers_unregister():
    """Explicitly unregister an API v1 compute node and cancel its queued work."""

    return _handle_server_unregister_request()


@app.route('/api/v1/relay/servers/poll', methods=['POST'])
def api_v1_relay_servers_poll():
    """Claim the next queued encrypted workload for a registered compute node."""
    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    _evict_stale_servers()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    public_key = data.get('server_public_key')
    if not public_key:
        return jsonify({'error': {'message': 'Missing server public key', 'code': 400}}), 400

    poll_wait_seconds = _api_v1_poll_wait_seconds()
    lease_seconds = _api_v1_lease_seconds()
    with server_round_robin_lock:
        server_payload = known_servers.get(public_key)
        if server_payload is None:
            return jsonify({'error': {'message': 'Server with the specified public key not found', 'code': 404}}), 404
        server_payload['last_ping'] = datetime.now()
        server_payload['last_ping_duration'] = lease_seconds
        server_payload['polling_until_monotonic'] = time.monotonic() + max(poll_wait_seconds, 0.0)
    LOGGER.info("server.heartbeat", extra={"server_public_key": public_key})

    def _mark_claimed_request_terminal(claimed_request):
        if not isinstance(claimed_request, dict):
            return
        if bool(claimed_request.get('e2ee_v1')):
            was_unregistered = _api_v1_server_was_recently_unregistered(public_key)
            _cancel_api_v1_request(
                claimed_request.get('client_public_key'),
                claimed_request.get('request_id'),
                status='cancelled' if was_unregistered else 'expired',
                reason='server_unregistered' if was_unregistered else 'provider_timeout',
            )

    def _server_not_found_response(claimed_request=None):
        _mark_claimed_request_terminal(claimed_request)
        return jsonify({'error': {'message': 'Server with the specified public key not found', 'code': 404}}), 404

    first_request = None
    deadline = time.monotonic() + poll_wait_seconds
    while True:
        server_missing = False
        with server_round_robin_lock:
            server_missing = public_key not in known_servers
        if server_missing:
            return _server_not_found_response(first_request)
        with client_inference_requests_changed:
            first_request = _pop_next_api_v1_request(public_key)
            if first_request is not None:
                break
            remaining = deadline - time.monotonic()
            if poll_wait_seconds <= 0 or remaining <= 0:
                break
            client_inference_requests_changed.wait(timeout=remaining)

    server_missing = False
    with server_round_robin_lock:
        server_payload = known_servers.get(public_key)
        if server_payload is None:
            server_missing = True
        else:
            server_payload.pop('polling_until_monotonic', None)

            if first_request is None:
                server_payload['last_ping'] = datetime.now()
                return jsonify({
                    'message': 'No requests available',
                    'next_ping_in_x_seconds': 0 if poll_wait_seconds > 0 else max(server_payload['last_ping_duration'], 1),
                    'poll_wait_seconds': poll_wait_seconds,
                }), 200
    if server_missing:
        return _server_not_found_response(first_request)

    queue_wait_ms = None
    queued_at = first_request.pop('_queued_at', None)
    if isinstance(queued_at, (int, float)):
        queue_wait_ms = round(max((time.time() - float(queued_at)) * 1000.0, 0.0), 3)
    request_id = first_request.get('request_id')
    if isinstance(request_id, str) and request_id:
        server_missing = False
        with server_round_robin_lock:
            server_payload = known_servers.get(public_key)
            if server_payload is None:
                server_missing = True
            else:
                with api_v1_in_flight_requests_lock:
                    in_flight_requests = server_payload.setdefault('api_v1_in_flight_requests', {})
                    if isinstance(in_flight_requests, dict):
                        in_flight_requests[request_id] = {
                            'expires_at': time.monotonic() + _api_v1_in_flight_ttl_seconds(),
                            'client_public_key': first_request.get('client_public_key'),
                            'cancel_token': first_request.get('cancel_token'),
                        }
        if server_missing:
            return _server_not_found_response(first_request)

    LOGGER.info(
        "relay.api_v1.request_dispatched",
        extra={
            "server_public_key": public_key,
            "request_id": first_request.get("request_id"),
            "queued_at_unix": queued_at,
            "dispatched_at_unix": time.time(),
            "queue_wait_ms": queue_wait_ms,
        },
    )
    return jsonify(first_request), 200

@app.route('/api/v1/relay/requests', methods=['POST'])
def api_v1_relay_requests():
    """Queue an encrypted API v1 relay request envelope for a target compute node."""
    _evict_stale_servers()
    data = request.get_json()
    envelope, error = _extract_ciphertext_envelope(data, require_server_key=True)
    if _payload_has_plaintext_fields(data):
        return jsonify({'error': {'message': 'Plaintext relay payload fields are forbidden; send ciphertext envelope only', 'code': 400}}), 400
    if _payload_has_unexpected_relay_fields(data, allow_server_public_key=True):
        return jsonify({'error': {'message': 'Unexpected relay payload fields are forbidden; send ciphertext envelope only', 'code': 400}}), 400
    if error:
        msg, code = error
        return jsonify({'error': {'message': msg, 'code': code}}), code

    server_public_key = envelope.pop('server_public_key')

    if not envelope.get('client_public_key'):
        return jsonify({'error': {'message': 'Missing client public key', 'code': 400}}), 400

    envelope['e2ee_v1'] = True
    queued_at = time.time()
    envelope['_queued_at'] = queued_at
    with server_round_robin_lock:
        server_payload = known_servers.get(server_public_key)
        if server_payload is None or not bool(server_payload.get(API_V1_SERVER_MARKER)):
            return jsonify({'error': {'message': 'Server with the specified public key not found', 'code': 404}}), 404
        _mark_request_pending(
            envelope.get('client_public_key'),
            envelope.get('request_id'),
            cancel_token=envelope.get('cancel_token'),
        )
        with client_inference_requests_changed:
            client_inference_requests.setdefault(server_public_key, []).append(envelope)
            queue_depth = len(client_inference_requests.get(server_public_key, []))
            client_inference_requests_changed.notify_all()
    LOGGER.info(
        "relay.api_v1.request_queued",
        extra={
            "server_public_key": server_public_key,
            "request_id": envelope.get("request_id"),
            "queued_at_unix": queued_at,
            "queue_depth": queue_depth,
        },
    )
    return jsonify({'message': 'Request received'}), 200



@app.route('/api/v1/relay/requests/cancel', methods=['POST'])
def api_v1_relay_requests_cancel():
    """Cancel or expire an API v1 relay request with its requester proof token."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    client_public_key = data.get('client_public_key')
    request_id = data.get('request_id')
    if not client_public_key or not request_id:
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    expected_cancel_token = _cancel_token_for_queued_or_in_flight_request(client_public_key, request_id)
    provided_cancel_token = data.get('cancel_token')
    if not (
        expected_cancel_token
        and isinstance(provided_cancel_token, str)
        and secrets.compare_digest(provided_cancel_token, expected_cancel_token)
    ):
        return jsonify({'error': {'message': 'Missing or invalid cancel proof', 'code': 403}}), 403

    status = _sanitize_terminal_status(data.get('status'))
    reason = _sanitize_terminal_reason(data.get('reason'), status)
    removed = _cancel_api_v1_request(
        client_public_key,
        request_id,
        status=status,
        reason=reason,
    )
    return jsonify({'status': status, 'request_id': request_id, 'removed_from_queue': removed}), 200


@app.route('/api/v1/relay/responses', methods=['POST'])
def api_v1_relay_responses():
    """Store an encrypted API v1 response envelope for client retrieval."""
    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json()
    envelope, error = _extract_ciphertext_envelope(data, require_server_key=False)
    if _payload_has_plaintext_fields(data):
        return jsonify({'error': {'message': 'Plaintext relay payload fields are forbidden; send ciphertext envelope only', 'code': 400}}), 400
    if _payload_has_unexpected_relay_fields(data, allow_server_public_key=False):
        return jsonify({'error': {'message': 'Unexpected relay payload fields are forbidden; send ciphertext envelope only', 'code': 400}}), 400
    if error:
        msg, code = error
        return jsonify({'error': {'message': msg, 'code': code}}), code

    client_public_key = envelope.get('client_public_key')
    if not client_public_key:
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    request_id = envelope.get('request_id')
    if isinstance(request_id, str) and request_id:
        terminal = _get_terminal_request(client_public_key, request_id)
        if terminal is not None:
            status = terminal.get('status', 'cancelled')
            return jsonify({'error': {'message': 'Request is no longer waiting for a response', 'code': status, 'status': status}}), 410
        if _has_client_response_for_request(client_public_key, request_id):
            LOGGER.info(
                "relay.api_v1.duplicate_response_ignored",
                extra={
                    "client_fingerprint": _safe_key_fingerprint(client_public_key),
                    "request_id": request_id,
                },
            )
            return jsonify({'message': 'Response already queued for client'}), 200
        _expire_pending_request_if_stale(client_public_key, request_id)
        terminal = _get_terminal_request(client_public_key, request_id)
        if terminal is not None:
            status = terminal.get('status', 'cancelled')
            return jsonify({'error': {'message': 'Request is no longer waiting for a response', 'code': status, 'status': status}}), 410
        with server_round_robin_lock:
            with api_v1_in_flight_requests_lock:
                for server_payload in known_servers.values():
                    in_flight_requests = server_payload.get('api_v1_in_flight_requests')
                    if not isinstance(in_flight_requests, dict) or request_id not in in_flight_requests:
                        continue
                    if _in_flight_entry_matches_client(in_flight_requests.get(request_id), client_public_key):
                        in_flight_requests.pop(request_id, None)
                        if not in_flight_requests:
                            server_payload.pop('api_v1_in_flight_requests', None)
                        break

    _queue_client_response(client_public_key, envelope)
    LOGGER.info(
        "relay.api_v1.response_received",
        extra={
            "client_fingerprint": _safe_key_fingerprint(client_public_key),
            "request_id": request_id,
        },
    )
    return jsonify({'message': 'Response received and queued for client'}), 200


@app.route('/api/v1/relay/responses/retrieve', methods=['POST'])
def api_v1_relay_responses_retrieve():
    """Retrieve an encrypted API v1 response envelope by client public key."""
    data = request.get_json()
    if not data or 'client_public_key' not in data:
        return jsonify({'error': {'message': 'Invalid request data', 'code': 400}}), 400

    client_public_key = data['client_public_key']
    request_id = data.get('request_id')
    terminal = _get_terminal_request(client_public_key, request_id)
    if terminal is not None:
        _remove_client_responses_for_request(client_public_key, request_id)
        status = terminal.get('status', 'cancelled')
        return jsonify({'error': {'message': f'Request {status}', 'code': status, 'status': status, 'reason': terminal.get('reason', status)}}), 410

    response = _pop_client_response(client_public_key, request_id)
    if response is None:
        _expire_pending_request_if_stale(client_public_key, request_id)
        terminal = _get_terminal_request(client_public_key, request_id)
        if terminal is not None:
            status = terminal.get('status', 'cancelled')
            return jsonify({'error': {'message': f'Request {status}', 'code': status, 'status': status, 'reason': terminal.get('reason', status)}}), 410
    if response is None:
        if _is_request_pending(client_public_key, request_id):
            LOGGER.debug(
                "relay.api_v1.response_pending",
                extra={"client_fingerprint": _safe_key_fingerprint(client_public_key), "request_id": request_id},
            )
            return jsonify({"status": "pending"}), 202
        terminal = _get_terminal_request(client_public_key, request_id)
        if terminal is not None:
            status = terminal.get('status', 'cancelled')
            return jsonify({'error': {'message': f'Request {status}', 'code': status, 'status': status, 'reason': terminal.get('reason', status)}}), 410
        if request_id:
            return jsonify({'error': {'message': f'Unknown request_id: {request_id}', 'code': 404}}), 404
        return jsonify({'error': {'message': 'No response available for the given public key', 'code': 404}}), 404

    LOGGER.info(
        "relay.api_v1.response_retrieved",
        extra={
            "client_fingerprint": _safe_key_fingerprint(client_public_key),
            "request_id": request_id,
        },
    )
    return jsonify(response), 200

@app.route('/faucet', methods=['POST'])
def faucet():
    """
    Endpoint for clients to request inference given a public key.
    The public key uniquely identifies the server to send the request to,
    mitigating the need to expose server URLs and force the servers to expose certain ports (as these
    servers interact in a RESTful way to maximize ease of use on consumer devices).

    The request body should be application/json and contain the following keys:
        - client_public_key: a unique identifier for the client (RSA-2048 public key) requesting inference
        - server_public_key: a unique identifier for the server (RSA-2048 public key) from which to request
            inference
        - chat_history: a string of ciphertext, encrypted with the server_public_key, which when decrypted,
            conforms to a list of objects with the the following JSON format:
            - role: the role of the message sender (either 'user' or 'assistant')
            - content: the message content
        - cipherkey: the AES key used to encrypt the chat_history, encrypted with the server_public_key
        - iv: the initialization vector used in the AES encryption of the chat_history

    Returns: a json response with the following keys:
        - chat_history: a string of ciphertext encrypted with the client_public_key, which when decrypted,
            conforms to a list of objects with the the following JSON format:
            - role: the role of the message sender (either 'user' or 'assistant')
            - content: the message content

    The request can either start a conversation with a single user message, or continue a conversation
    with a list of chat messages between the user and the assistant. Clients and servers may implement
    diffing mechanisms to determine if conversations were altered, but this is intentionally built in a
    stateless manner to minimize complexity and maximize scalability. It's trivially easy to identify
    tampering from either party, so enforcing it on the relay is a non-goal, especially since communication
    between the client and the server uses mandatory end-to-end encryption. This relay path must remain
    ciphertext-only (+ safe routing metadata) and fail closed for plaintext payloads or bypass attempts.

    Example request:

    {
        "client_public_key": "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQklqQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FROEFNSUlCQ2dLQ0FRRUF3ZFpidTljcGwvclk4dFVrM3BoQwoxNTVnRm02OTRJOTd5YUJURkZSZ25PQjhlbXlZWWJCbDdlTFNTcVJOUTg2cDQzK1hldXdYcHpTcnc4SXJRdTZaCjA2cWJ0SlJmcy93bC84Y1BJZzVWdWtVRjBPSEJ2MFFnRkxwdFBSZUVUOXlKMFNEbUcxQlhwazJieXE2YUI3bG4KbFBNSytZb1VxQ0dLSzVRMXlHVFUzNC9YOHE0Q1VlYWJjL0RVRFRsNEUxdlkwK3EzaTZIMEZrd1Z3TGQ1bWpoegpzeHlqNjZxRU5kblF5RkEyVTZlU0tORHhaOGdLMC84YzVHbGhDV3ZTUmF1ZE10R2ZVNkZTTzJoSmMyb0NKYW5vCmtFNWNGeEFLQjY1eHRWRXdJYUY1UTVYUm0zajg5Ym1tWGFSYzBjcGZlMFhJYW9qQ3YvdTcxWi9wRjU4clJKOGsKQndJREFRQUIKLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg==",
        "server_public_key": "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQklqQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FROEFNSUlCQ2dLQ0FRRUF4M0VLUGkvNVNGc3JsaUZVQnMvagphcW8xY2RUKzl4cUNoZUt2bHl1dVpGNG5JVFVLbW5ZSmtUVE9GL3JNME9nMTM3b1d6RzhwOWdBREtOMWxoYWtVCjBwdkNZeVh3c3dEV3JMU0ZOTVc0d1B0cWpjaUxIbGhrQ3REQ3N3WjhMazd4NE1IOExHYTVTVzkzdHc4eWQrSGIKNTd0N1NaL0pneEtIZE5QUmh4Tjh2Q1pOOXQ4OWIxaklxaHZyNVBIZk9LSC9pc0hxWXIwdUxsVW9XaTdzenVpVApJZEcrS1UyNFFqQkxCK1RZaDdpVy9XTWF2VEhzRSt6dUxlVkJKSmdYTmZuNk15K3ZxaEJyY2RDeWZ2VG1vQVZ1CkRjckFkZ1NoQ1plL01GT3RRdDlEb3loNUx3ck03U3NmQVoxa0x1Rm43VjloNGw4V3JVOFdWaHdGYmE4TlNmUFMKalFJREFRQUIKLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg==",
        "chat_history": "v61G8y7z1WYGLnGJ27f+A0daaxNWexT9Tm6uN/yibmvZTuQQGuQPaoczVXigZayK",
        "cipherkey": "B+ewTtXyl0dezVTQ1gTXxASj4PqKKfqdfcBrSV5yyKQnIz8voK2+dFUnJx6EXxEIpyXZ/BymXCs9YLOJceCsaYyQCRvWYEzLWrKDJpGpkKWZNkpKigqsGBwD+qZlW7Vxjj91eqVunLBxpUTB3rcsw7zuuW/jTtWnRe8UW/y0c8ZDw8rYbIHmDs3IykNfThWhE2K0olMLkUTOhr6+yfRh4fb3WHvTdUtCzIrjOSwaA7OgpdlaqiZ/qbLsdfaSmNCKNh6AL4eJN0ifYq89ETeTA77IDyww2YIvJqWm4DdlgV4I14Ker5RCmdTBabPLJjFuXm7YaI57IfSsTAghLYX+Ww==",
        "iv": "yUR11oNkM/ZQeGuRF6JHAw=="
    }
    """
    if not _legacy_routes_enabled():
        return _legacy_route_deprecated_response('/faucet')

    _evict_stale_servers()
    # Parse the request data
    data = request.get_json()
    if _payload_has_plaintext_fields(data):
        return jsonify({
            'error': {
                'message': 'Plaintext relay payload fields are forbidden; send ciphertext envelope only',
                'code': 400
            }
        }), 400
    if _payload_has_unexpected_faucet_fields(data):
        return jsonify({
            'error': {
                'message': 'Unexpected relay payload fields are forbidden; send ciphertext envelope only',
                'code': 400,
            }
        }), 400

    if not data or 'server_public_key' not in data or 'chat_history' not in data or 'cipherkey' not in data or 'iv' not in data:
        return jsonify({
            'error': {
                'message': 'Invalid request data',
                'code': 400
            }
        }), 400

    server_public_key = data['server_public_key']
    chat_history_ciphertext = data['chat_history']
    cipherkey = data['cipherkey']
    iv = data['iv']  # Extract the IV from the request data
    stream_requested = bool(data.get('stream', False))
    client_public_key = data.get('client_public_key', None)

    if stream_requested and not client_public_key:
        return jsonify({
            'error': {
                'message': 'Streaming requests require a client public key',
                'code': 400,
            }
        }), 400

    # Check if the server with the specified public key is known
    if server_public_key not in known_servers:
        return jsonify({'error': {'message': 'Server with the specified public key not found', 'code': 404}}), 404

    # Append the client's request to the list of requests for the server
    with client_inference_requests_changed:
        client_inference_requests.setdefault(server_public_key, []).append({
            'chat_history': chat_history_ciphertext,
            'client_public_key': client_public_key,
            'cipherkey': cipherkey,
            'iv': iv,  # Include the IV in the saved client's request
            'stream': stream_requested,
        })
        client_inference_requests_changed.notify_all()
    return jsonify({'message': 'Request received'}), 200

@app.route('/sink', methods=['POST'])
def sink():
    """
    Endpoint for server instances to announce their availability (offering a compute sink).
    The request body should be application/json and contain the following keys:
        - server_public_key: a unique identifier for the server

    Returns: a json response with the following keys:
        - client_public_key: if present, chat_history will be present as well, and the client_public_key
            will be the public key used by the server when it returns the chat_history in ciphertext.
        - chat_history: a string of ciphertext, encrypted with the server's public key.
            Conforms to the same JSON format as the request body.
        - next_ping_in_x_seconds: the number of seconds after which the server should send the next ping
    """
    if not _legacy_routes_enabled():
        return _legacy_route_deprecated_response('/sink')

    _evict_stale_servers()
    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid request data'}), 400
    public_key = data.get('server_public_key', None)

    raw_batch_size = data.get('max_batch_size') if isinstance(data, dict) else None
    max_batch_size = 1
    if raw_batch_size is not None:
        try:
            max_batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid max_batch_size'}), 400
        if max_batch_size < 1:
            return jsonify({'error': 'Invalid max_batch_size'}), 400

    if public_key is None:
        return jsonify({'error': 'Invalid public key'}), 400

    # Update or add the server to known_servers
    with server_round_robin_lock:
        if public_key in known_servers:
            known_servers[public_key]['last_ping'] = datetime.now()
        else:
            known_servers[public_key] = {
                'public_key': public_key,
                'last_ping': datetime.now(),
                'last_ping_duration': 10
            }
        next_ping_duration = known_servers[public_key]['last_ping_duration']

    response_data = {
        'next_ping_in_x_seconds': next_ping_duration
    }

    # Check if there are any client requests for this server
    with client_inference_requests_changed:
        queued_requests = client_inference_requests.get(public_key, [])
        if queued_requests:
            batch = []
            while queued_requests and len(batch) < max_batch_size:
                request_payload = queued_requests[0]
                if 'api_v1_request' in request_payload:
                    queued_requests.pop(0)
                    LOGGER.warning(
                        "relay.api_v1_plaintext_payload_dropped",
                        extra={"server_public_key": public_key},
                    )
                    continue
                if request_payload.get('e2ee_v1'):
                    LOGGER.warning(
                        "relay.api_v1_ciphertext_payload_skipped",
                        extra={"server_public_key": public_key},
                    )
                    break
                request_payload = queued_requests.pop(0)
                if request_payload.get('stream'):
                    session = _register_stream_session(
                        public_key,
                        request_payload.get('client_public_key'),
                    )
                    if session is not None:
                        request_payload['stream_session_id'] = session['session_id']
                batch.append(request_payload)
            if batch:
                first_request = batch[0]
                response_data['client_public_key'] = first_request.get('client_public_key')
                response_data['chat_history'] = first_request.get('chat_history')
                response_data['cipherkey'] = first_request.get('cipherkey')
                response_data['iv'] = first_request.get('iv')

                if first_request.get('stream') and first_request.get('stream_session_id'):
                    response_data['stream'] = True
                    response_data['stream_session_id'] = first_request['stream_session_id']

                if max_batch_size > 1:
                    response_data['batch'] = batch

    return jsonify(response_data)


@app.route('/unregister', methods=['POST'])
def unregister():
    """Explicitly unregister a compute node and clear relay queue/session state."""
    return _handle_server_unregister_request(invalid_error_shape="legacy")

@app.route('/source', methods=['POST'])
def source():
    """
    Receives encrypted responses from the server and queues them for the client to retrieve.
    """
    if not _legacy_routes_enabled():
        return _legacy_route_deprecated_response('/source')

    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json()
    if not data or 'client_public_key' not in data or 'chat_history' not in data or 'cipherkey' not in data or 'iv' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    client_public_key = data['client_public_key']
    encrypted_chat_history = data['chat_history']
    encrypted_cipherkey = data['cipherkey']
    iv = data['iv']

    # Store the response in the client_responses dictionary
    with client_responses_lock:
        client_responses[client_public_key] = {
            'chat_history': encrypted_chat_history,
            'cipherkey': encrypted_cipherkey,
            'iv': iv
        }
    return jsonify({'message': 'Response received and queued for client'}), 200


@app.route('/stream/source', methods=['POST'])
def stream_source():
    """Accept streaming chunks emitted by compute nodes."""

    auth_error = _validate_server_registration()
    if auth_error:
        return auth_error

    data = request.get_json()
    if not data or 'session_id' not in data or 'chunk' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    session_id = data['session_id']
    chunk = data['chunk']
    final = bool(data.get('final', False))

    if not _append_stream_chunk(session_id, chunk, final=final):
        return jsonify({'error': 'Unknown stream session'}), 404

    return jsonify({'message': 'Chunk stored', 'final': final}), 200


@app.route('/retrieve', methods=['POST'])
def retrieve():
    """
    Endpoint for clients to retrieve responses queued by the /source endpoint.
    """
    if not _legacy_routes_enabled():
        return _legacy_route_deprecated_response('/retrieve')

    data = request.get_json()
    if not data or 'client_public_key' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    client_public_key = data['client_public_key']

    # Check if there's a response for the given client public key
    with client_responses_lock:
        response_data = client_responses.pop(client_public_key, None)
    if response_data is not None:
        return jsonify(response_data), 200
    return jsonify({'error': 'No response available for the given public key'}), 200


@app.route('/stream/retrieve', methods=['POST'])
def stream_retrieve():
    """Return queued streaming chunks for the requesting client."""

    data = request.get_json()
    if not data or 'client_public_key' not in data:
        return jsonify({'error': 'Invalid request data'}), 400

    client_public_key = data['client_public_key']
    popped = _pop_stream_chunks_for_client(client_public_key)
    if popped is None:
        return jsonify({'error': 'No active stream for the given public key'}), 200

    session_id, chunks, final = popped
    response_payload = {
        'stream': True,
        'session_id': session_id,
        'chunks': chunks,
    }
    if final:
        response_payload['final'] = True

    return jsonify(response_payload), 200


def serve(host: str, port: int) -> None:
    """Run the relay application using Werkzeug's production server."""

    server = make_server(host, port, app, threaded=True)
    ctx = app.app_context()
    ctx.push()

    shutdown_requested = threading.Event()
    shutdown_thread: threading.Thread | None = None

    def _shutdown_server_async() -> None:
        nonlocal shutdown_thread

        def _shutdown_server() -> None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - defensive logging path
                LOGGER.exception("relay.shutdown.error")

        shutdown_thread = threading.Thread(
            target=_shutdown_server,
            name="relay-server-shutdown",
            daemon=False,
        )
        shutdown_thread.start()

    def _handle_signal(signum, _frame):
        LOGGER.info("relay.shutdown.signal", extra={"signal": signum})
        DRAINING.set()
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        _shutdown_server_async()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    LOGGER.info(
        "relay.startup",
        extra={
            "host": host,
            "port": port,
            "upstream": app.config.get("upstream_url"),
        },
    )

    try:
        server.serve_forever()
    finally:
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=1.0)
        ctx.pop()
        LOGGER.info(
            "relay.shutdown",
            extra={"requested": shutdown_requested.is_set()},
        )


def main(argv: list[str] | None = None) -> None:
    args = parse_cli_args(argv)
    host = os.environ.get("RELAY_HOST") or args.host
    port_value = os.environ.get("RELAY_PORT") or str(args.port)
    try:
        port = int(port_value)
    except ValueError:
        LOGGER.warning("relay.invalid_port", extra={"port": port_value})
        port = args.port

    _configure_mock_mode(args.use_mock_llm)
    serve(host, port)


if __name__ == '__main__':  # pragma: no cover
    main()
