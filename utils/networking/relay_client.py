"""Relay client module for managing communication with relay servers."""
from __future__ import annotations

import base64
import binascii
import importlib
import ipaddress
import json
import logging
import math
import os
import hashlib
import re
import sys
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple, Union

from utils.processing_result import RelayProcessingResult
from urllib.parse import urlparse, urlunparse

from utils.networking.http_requests_compat import requests
from utils.context_profiles import DEFAULT_CONTEXT_TIER, get_context_profile, normalize_context_tier
from utils.llm.model_profiles import build_model_aliases

# Configure logging
logger = logging.getLogger('relay_client')
DEFAULT_API_V1_LEASE_SECONDS = 30.0


class _ApiV1ChatValidationResult(NamedTuple):
    valid: bool
    code: Optional[str] = None
    reason: Optional[str] = None
    message_count: int = 0
    message_index: Optional[int] = None
    message_content_chars: Optional[int] = None
    total_content_chars: int = 0


def _is_llama_cpp_inference_request_error(exc: BaseException) -> bool:
    """Return True for request-scoped llama.cpp inference validation failures."""

    return exc.__class__.__name__ == "LlamaCppInferenceRequestError"


def _is_llama_cpp_restartable_worker_error(exc: BaseException) -> bool:
    """Return True for restartable llama.cpp worker failures without importing runtime internals."""

    return exc.__class__.__name__.startswith("LlamaCpp") and "Worker" in exc.__class__.__name__


def _load_jsonschema():
    """Lazy-load jsonschema; return ``None`` when unavailable in packaged runtimes."""
    try:
        return importlib.import_module("jsonschema")
    except ModuleNotFoundError:
        return None
    except ImportError:
        return None


def _validate_with_fallback(instance: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Validate JSON payloads even when jsonschema is unavailable in packaged runtimes."""
    try:
        jsonschema = _load_jsonschema()
    except RuntimeError as exc:
        if "jsonschema is required" not in str(exc):
            raise
        jsonschema = None
    except AssertionError:
        jsonschema = None

    if jsonschema is not None:
        try:
            jsonschema.validate(instance=instance, schema=schema)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return

    if not isinstance(instance, dict):
        raise ValueError("Payload must be an object")

    required_fields = schema.get("required", [])
    properties = schema.get("properties", {})
    type_by_name = {
        "string": str,
        "number": (int, float),
        "object": dict,
    }

    for field in required_fields:
        if field not in instance:
            raise ValueError(f"Missing required field: {field}")

    for field, value in instance.items():
        field_schema = properties.get(field)
        if not field_schema:
            continue
        expected = field_schema.get("type")
        expected_types = type_by_name.get(expected)
        if expected_types is not None and not isinstance(value, expected_types):
            raise ValueError(f"Invalid type for field '{field}': expected {expected}")


def get_config_lazy():
    """Lazy import of config to avoid circular imports"""
    from config import get_config
    return get_config()

# Define JSON schema for messages
MESSAGE_SCHEMA = {
    "type": "object",
    "required": ["client_public_key", "chat_history", "cipherkey", "iv"],
    "properties": {
        "client_public_key": {"type": "string"},
        "chat_history": {"type": "string"},
        "cipherkey": {"type": "string"},
        "iv": {"type": "string"}
    }
}

# Define relay response schema
RELAY_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["next_ping_in_x_seconds"],
    "properties": {
        "next_ping_in_x_seconds": {"type": "number"},
        "client_public_key": {"type": "string"},
        "chat_history": {"type": "string"},
        "cipherkey": {"type": "string"},
        "iv": {"type": "string"},
        "api_v1_request": {"type": "object"},
        "error": {"type": "string"}
    }
}


_API_V1_DIAGNOSTIC_HEADER_NAMES = (
    "server",
    "cf-ray",
    "cf-cache-status",
    "content-type",
    "x-request-id",
    "retry-after",
)
_API_V1_BODY_SNIPPET_LIMIT = 512
_API_V1_REDACTED = "[redacted]"
_API_V1_SENSITIVE_BODY_KEYS = {
    "x_relay_server_token",
    "relay_server_token",
    "server_registration_token",
    "registration_token",
    "token",
    "authorization",
    "private_key",
    "privatekey",
    "public_key",
    "publickey",
    "client_public_key",
    "server_public_key",
    "ciphertext",
    "cipherkey",
    "chat_history",
    "iv",
    "messages",
    "prompt",
    "content",
    "api_v1_request",
    "api_v1_response",
}


def _redact_sensitive_text(text: Any, *, secrets: Tuple[str, ...] = ()) -> str:
    """Return a compact single-line text representation with sensitive values removed."""

    rendered = text if isinstance(text, str) else str(text)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            rendered = rendered.replace(secret, _API_V1_REDACTED)

    replacements = [
        (
            r"(?i)(x-relay-server-token\s*[:=]\s*)([^\s<>'\"]+)",
            r"\1[redacted]",
        ),
        (
            r"(?i)((?:private[_-]?key|public[_-]?key|server[_-]?public[_-]?key|"
            r"client[_-]?public[_-]?key|ciphertext|cipherkey|chat_history|iv|"
            r"prompt|content)\s*[=:]\s*)([^\s,;}<]+)",
            r"\1[redacted]",
        ),
    ]
    for pattern, replacement in replacements:
        rendered = re.sub(pattern, replacement, rendered)

    rendered = re.sub(r"[\r\n\x00]+", " ", rendered)

    if len(rendered) > _API_V1_BODY_SNIPPET_LIMIT:
        return rendered[:_API_V1_BODY_SNIPPET_LIMIT] + "..."
    return rendered


def _sanitize_api_v1_json_body(value: Any, *, secrets: Tuple[str, ...] = ()) -> Any:
    """Sanitize a relay error JSON body before it is logged or returned."""

    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower().replace("-", "_")
            if normalized_key in _API_V1_SENSITIVE_BODY_KEYS:
                sanitized[key_text] = _API_V1_REDACTED
            else:
                sanitized[key_text] = _sanitize_api_v1_json_body(item, secrets=secrets)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_api_v1_json_body(item, secrets=secrets) for item in value[:10]]
    if isinstance(value, str):
        return _redact_sensitive_text(value, secrets=secrets)
    return value


def _safe_api_v1_response_body_snippet(
    response: Any, *, secrets: Tuple[str, ...] = ()
) -> Tuple[str, Optional[Any]]:
    """Return a redacted, capped non-200 response-body snippet and parsed JSON when available."""

    parsed_json: Optional[Any] = None
    try:
        parsed_json = response.json()
    except Exception:
        parsed_json = None

    if parsed_json is not None:
        sanitized = _sanitize_api_v1_json_body(parsed_json, secrets=secrets)
        try:
            rendered = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = str(sanitized)
        return _redact_sensitive_text(rendered, secrets=secrets), sanitized

    body_text = getattr(response, "text", "")
    return _redact_sensitive_text(body_text or "", secrets=secrets), None


def _api_v1_response_headers(response: Any) -> Dict[str, str]:
    """Extract infrastructure-oriented response headers safe for diagnostics."""

    headers = getattr(response, "headers", {}) or {}
    diagnostic_headers: Dict[str, str] = {}
    for header_name in _API_V1_DIAGNOSTIC_HEADER_NAMES:
        value: Optional[Any] = None
        try:
            value = headers.get(header_name)
        except AttributeError:
            value = None
        if value is None:
            try:
                value = headers.get(header_name.title())
            except AttributeError:
                value = None
        if value is not None:
            diagnostic_headers[header_name] = _redact_sensitive_text(str(value))
    return diagnostic_headers


def _sanitize_relay_target(relay_url: Any) -> str:
    """Return a relay target safe for diagnostics without userinfo, query, or fragment."""

    if not isinstance(relay_url, str):
        return "unknown"
    try:
        parsed = urlparse(relay_url.strip() if relay_url.strip() else "")
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError:
        return "unknown"

    if not parsed.scheme or not hostname:
        return "unknown"
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parsed_port}" if parsed_port is not None else ""
    return urlunparse((parsed.scheme, f"{host}{port}", "", "", "", ""))


def _normalise_registration_token(value: Optional[str]) -> Optional[str]:
    """Normalise optional registration tokens from config or environment."""

    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _coerce_optional_bool(value: Optional[Any]) -> Optional[bool]:
    """Interpret truthy values from config/environment settings."""

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        lowered = value.strip().lower()
        if not lowered:
            return None
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False

    return None

def _log(level: str, message: str, *args, exc_info: Optional[bool] = None) -> None:
    """Log a message with environment-aware behaviour.

    Info-level messages are suppressed in production, while errors always emit.
    When logging in production, stack traces are hidden even if ``exc_info`` is
    requested so sensitive details are not leaked.
    """

    try:
        config = get_config_lazy()
        is_production = bool(getattr(config, "is_production", False))
    except Exception:
        is_production = False

    if is_production and level != "error":
        return

    log_func = getattr(logger, level)
    formatted = message.format(*args) if args else message

    kwargs: Dict[str, Any] = {}
    if exc_info is not None:
        kwargs["exc_info"] = exc_info if not is_production else False

    log_func(formatted, **kwargs)


def log_info(message, *args) -> None:
    """Log info only in non-production environments using consistent formatting"""
    _log("info", message, *args)


def log_error(message, *args, exc_info: bool = False) -> None:
    """Log errors only in non-production environments using consistent formatting"""
    _log("error", message, *args, exc_info=exc_info)


def _max_poll_failures_before_stop() -> Optional[int]:
    """Return max consecutive polling failures before stopping.

    This keeps CI from spending tens of minutes in retry loops when relay
    endpoints are unreachable, while preserving infinite polling by default
    outside CI unless explicitly overridden.
    """
    raw_value = os.environ.get("TOKENPLACE_MAX_POLL_FAILURES")
    if raw_value is not None:
        try:
            parsed = int(raw_value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    if os.environ.get("CI", "").strip().lower() == "true":
        return 18
    return None


def _normalize_client_public_key_b64(client_public_key_b64: Any) -> Optional[str]:
    """Normalize relay metadata key format for consistent decode/binding checks."""
    if not isinstance(client_public_key_b64, str):
        return None
    normalized = client_public_key_b64.strip()
    if not normalized:
        return None
    return normalized


def _extract_chat_history_and_validate_key_binding(
    decrypted_payload: Any,
    expected_client_public_key_b64: str,
) -> Optional[Any]:
    """Validate optional encrypted key-binding metadata and return chat history."""

    def _is_valid_chat_history(chat_history: Any) -> bool:
        if not isinstance(chat_history, list):
            return False

        for message in chat_history:
            if not isinstance(message, dict):
                return False
            if not isinstance(message.get("role"), str):
                return False
            if not isinstance(message.get("content"), str):
                return False

        return True

    if isinstance(decrypted_payload, dict):
        bound_client_key = decrypted_payload.get("client_public_key")
        if bound_client_key is not None:
            if not isinstance(bound_client_key, str):
                log_error("Invalid encrypted payload: client_public_key binding must be a string")
                return None
            if bound_client_key != expected_client_public_key_b64:
                log_error("Rejected request: relay client key does not match encrypted key binding")
                return None
        else:
            # Legacy clients may still send unbound payloads; continue to accept for compatibility.
            pass

        if "chat_history" not in decrypted_payload:
            log_error("Invalid encrypted payload: missing chat_history")
            return None

        chat_history = decrypted_payload.get("chat_history")
        if not _is_valid_chat_history(chat_history):
            log_error("Invalid encrypted payload: chat_history must be a list of role/content message objects")
            return None

        return chat_history

    if _is_valid_chat_history(decrypted_payload):
        return decrypted_payload

    log_error("Invalid encrypted payload: expected chat_history list or payload containing chat_history")
    return None


def _extract_api_v1_request_payload(
    decrypted_payload: Any,
    expected_client_public_key_b64: str,
) -> Optional[Dict[str, Any]]:
    """Validate API v1 relay E2EE envelope and return request payload."""

    if not isinstance(decrypted_payload, dict):
        return None

    if decrypted_payload.get("protocol") != "tokenplace_api_v1_relay_e2ee":
        return None

    bound_client_key = decrypted_payload.get("client_public_key")
    if bound_client_key != expected_client_public_key_b64:
        log_error("Rejected API v1 relay payload: encrypted client key binding mismatch")
        return None

    request_id = decrypted_payload.get("request_id")
    api_v1_request = decrypted_payload.get("api_v1_request")
    if not isinstance(request_id, str) or not request_id.strip():
        log_error("Rejected API v1 relay payload: missing request_id")
        return None
    if not isinstance(api_v1_request, dict):
        log_error("Rejected API v1 relay payload: missing api_v1_request object")
        return None

    model = api_v1_request.get("model")
    messages = api_v1_request.get("messages")
    options = api_v1_request.get("options", {})
    routing = api_v1_request.get("routing", {})
    if not isinstance(model, str) or not model.strip():
        log_error("Rejected API v1 relay payload: model must be a non-empty string")
        return None
    if not isinstance(messages, list):
        log_error("Rejected API v1 relay payload: messages must be a list")
        return None
    if not isinstance(options, dict):
        log_error("Rejected API v1 relay payload: options must be an object")
        return None
    if routing is None:
        routing = {}
    if not isinstance(routing, dict):
        log_error("Rejected API v1 relay payload: routing must be an object")
        return None
    raw_context_tier = routing.get("context_tier")
    normalized_raw_context_tier = raw_context_tier.strip() if isinstance(raw_context_tier, str) else raw_context_tier
    context_tier = normalize_context_tier(normalized_raw_context_tier)
    if normalized_raw_context_tier is not None and context_tier != normalized_raw_context_tier:
        log_error("Rejected API v1 relay payload: routing.context_tier is unsupported")
        return None

    return {
        "request_id": request_id,
        "model": model,
        "messages": messages,
        "options": options,
        "routing": {"context_tier": context_tier},
    }


class RelayClient:
    """
    Client for communicating with relay servers.
    Handles registration, polling, sending and receiving encrypted messages.

    Example:
        ```python
        # Create a relay client
        relay = RelayClient(
            base_url="http://localhost",
            port=8080,
            crypto_manager=crypto_manager_instance,
            model_manager=model_manager_instance
        )

        # Start polling in a separate thread
        import threading
        polling_thread = threading.Thread(target=relay.poll_relay_continuously)
        polling_thread.daemon = True
        polling_thread.start()

        # Later, to stop polling cleanly:
        relay.stop()
        polling_thread.join(timeout=15)  # Wait for thread to finish
        ```
    """

    _API_V1_LOCAL_MODEL_ALIASES = build_model_aliases()
    _API_V1_LOCAL_ADAPTER_BASE_MODELS = {}
    _API_V1_ALLOWED_MESSAGE_ROLES = {"system", "user", "assistant"}
    _API_V1_MAX_MESSAGES = 64
    _API_V1_MAX_TEXT_BLOCKS = 32
    # Aggregate plaintext abuse/transport safety ceiling, not a token estimate.
    _API_V1_MAX_TOTAL_REQUEST_CHARS = 131072
    _API_V1_MAX_STOP_SEQUENCES = 16
    _API_V1_MAX_STOP_CHARS = 256
    _API_V1_MAX_TOKENS_LIMIT = 8192
    _API_V1_MAX_SEED = 2**32 - 1
    # Single source of truth for API v1 Qwen behavior. API v1 is a
    # non-reasoning chat-completions surface: Qwen thinking is disabled via the
    # documented message-level /no_think control because the packaged
    # llama-cpp-python create_chat_completion path used here does not reliably
    # expose chat-template kwargs such as enable_thinking/template_kwargs.
    _API_V1_QWEN_NON_THINKING_POLICY = {
        "thinking_mode": "disabled",
        "message_control": "/no_think",
        "visible_think_output_forbidden": True,
        "reasoning_content_forbidden": True,
    }

    def __init__(
        self,
        base_url: str,
        port: Optional[int],
        crypto_manager,
        model_manager,
        *,
        include_configured_servers: bool = True,
        explicit_relay_urls: Optional[Sequence[str]] = None,
    ):
        """
        Initialize the RelayClient.

        Args:
            base_url: The base URL of the relay server
                (e.g., 'https://token.place' or 'http://localhost:5000')
            port: Optional relay port injected only for non-HTTPS URLs without an explicit port
            crypto_manager: Instance of CryptoManager for encryption/decryption
            model_manager: Instance of ModelManager for LLM interaction
            include_configured_servers: When True, include configured/env relay fallbacks
                and relay cluster-only mode. When False, use only the explicit base relay.
            explicit_relay_urls: Additional explicit relay URLs supplied by the desktop
                start request. These are included even when configured fallbacks are disabled.
        """
        self.base_url = base_url
        self.port = port
        self.crypto_manager = crypto_manager
        self.model_manager = model_manager
        self.stop_polling = True  # Flag to control polling loop - starts as True so loop won't run until explicitly started
        self._polling_stopped_by_request = False
        self._registration_token: Optional[str] = None
        configured_servers: List[Any] = []
        self._cluster_only = False

        try:
            config = get_config_lazy()
            self._request_timeout = config.get('relay.request_timeout', 10)

            if include_configured_servers:
                configured_servers = list(config.get('relay.additional_servers', []) or [])

                cf_fallbacks = config.get('relay.cloudflare_fallback_urls', []) or []
                for entry in cf_fallbacks:
                    if entry not in configured_servers:
                        configured_servers.append(entry)

                pool_secondary = config.get('relay.server_pool_secondary', []) or []
                for entry in pool_secondary:
                    if entry not in configured_servers:
                        configured_servers.append(entry)

                primary_config_url = config.get('relay.server_url', '')
                if primary_config_url and primary_config_url not in configured_servers:
                    configured_servers.insert(0, primary_config_url)

            if include_configured_servers:
                cluster_only_value = config.get('relay.cluster_only', False)
                parsed_cluster_only = _coerce_optional_bool(cluster_only_value)
                if parsed_cluster_only is not None:
                    self._cluster_only = parsed_cluster_only
                elif isinstance(cluster_only_value, bool):
                    self._cluster_only = cluster_only_value

            token_value = config.get('relay.server_registration_token', None)
            if not token_value:
                token_value = os.environ.get('TOKEN_PLACE_RELAY_SERVER_TOKEN')
            self._registration_token = _normalise_registration_token(token_value)

        except Exception:
            self._request_timeout = 10  # Fallback default
            if include_configured_servers:
                cluster_env = _coerce_optional_bool(os.environ.get('TOKEN_PLACE_RELAY_CLUSTER_ONLY'))
                self._cluster_only = cluster_env if cluster_env is not None else False

            if include_configured_servers:
                upstreams_raw = os.environ.get('TOKEN_PLACE_RELAY_UPSTREAMS', '')
                if upstreams_raw:
                    normalised = upstreams_raw.replace('\n', ',')
                    configured_servers.extend(
                        entry.strip()
                        for entry in normalised.split(',')
                        if entry and entry.strip()
                    )

                cf_raw = os.environ.get('TOKEN_PLACE_RELAY_CLOUDFLARE_URLS', '')
                cf_single = os.environ.get('TOKEN_PLACE_RELAY_CLOUDFLARE_URL', '')
                combined = ','.join(part for part in (cf_raw, cf_single) if part)
                if combined:
                    entries: List[str] = []
                    try:
                        loaded = json.loads(combined)
                    except json.JSONDecodeError:
                        normalised_cf = combined.replace('\n', ',')
                        entries.extend(
                            segment.strip()
                            for segment in normalised_cf.split(',')
                            if segment.strip()
                        )
                    else:
                        if isinstance(loaded, str):
                            entries.append(loaded.strip())
                        elif isinstance(loaded, (list, tuple)):
                            for item in loaded:
                                if isinstance(item, str) and item.strip():
                                    entries.append(item.strip())
                    for entry in entries:
                        if entry and entry not in configured_servers:
                            configured_servers.append(entry)

            self._registration_token = _normalise_registration_token(
                os.environ.get('TOKEN_PLACE_RELAY_SERVER_TOKEN')
            )

        if explicit_relay_urls:
            for entry in explicit_relay_urls:
                if isinstance(entry, str) and entry.strip() and entry not in configured_servers:
                    configured_servers.append(entry)

        self._relay_urls = self._build_relay_targets(
            base_url,
            port,
            configured_servers,
            cluster_only=self._cluster_only,
        )
        self._active_relay_index = 0
        self._sink_start_index = 0
        self._last_api_v1_work_relay_url: Optional[str] = None
        self._api_v1_registered_relays: Set[str] = set()
        self._api_v1_last_heartbeat_at: Dict[str, float] = {}
        self._unregister_attempted = False
        self._unregister_complete = False
        self._api_v1_heartbeat_lock = threading.Lock()
        self._api_v1_heartbeat_stop = threading.Event()
        self._api_v1_heartbeat_thread: Optional[threading.Thread] = None
        self._api_v1_heartbeat_stopping = False


    def _api_v1_start_heartbeat_worker(self) -> None:
        """Start an independent API v1 lease refresher for long inferences."""

        lock = getattr(self, "_api_v1_heartbeat_lock", None)
        if lock is None:
            return
        with lock:
            if getattr(self, "_api_v1_heartbeat_stopping", False):
                return
            thread = getattr(self, "_api_v1_heartbeat_thread", None)
            if thread is not None and thread.is_alive():
                return
            self._api_v1_heartbeat_stop.clear()
            thread = threading.Thread(
                target=self._api_v1_heartbeat_worker,
                name="tokenplace-api-v1-heartbeat",
                daemon=True,
            )
            self._api_v1_heartbeat_thread = thread
            thread.start()

    def _api_v1_stop_heartbeat_worker(self) -> None:
        """Stop the API v1 heartbeat worker without leaving shutdown heartbeats behind."""

        stop_event = getattr(self, "_api_v1_heartbeat_stop", None)
        if stop_event is None:
            return
        lock = getattr(self, "_api_v1_heartbeat_lock", None)
        if lock is None:
            stop_event.set()
            return
        with lock:
            self._api_v1_heartbeat_stopping = True
            stop_event.set()
            thread = getattr(self, "_api_v1_heartbeat_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            join_timeout = max(float(getattr(self, "_request_timeout", 10) or 10) + 1.0, 2.0)
            thread.join(timeout=join_timeout)
        with lock:
            if getattr(self, "_api_v1_heartbeat_thread", None) is thread and (
                thread is None or not thread.is_alive()
            ):
                self._api_v1_heartbeat_thread = None
            self._api_v1_heartbeat_stopping = False

    def _api_v1_heartbeat_worker(self) -> None:
        """Refresh relay leases independently from polling/inference work."""

        while not self._api_v1_heartbeat_stop.wait(0.25):
            if getattr(self, "_polling_stopped_by_request", False):
                break
            relay_wait_hints = getattr(self, "_api_v1_relay_wait_hints", None)
            if not isinstance(relay_wait_hints, dict):
                relay_wait_hints = {}
                self._api_v1_relay_wait_hints = relay_wait_hints
            for candidate_url in list(getattr(self, "_api_v1_registered_relays", set())):
                if self._api_v1_heartbeat_stop.is_set():
                    break
                hints = relay_wait_hints.get(candidate_url, {})
                lease = self._normalise_positive_seconds(
                    hints.get("next_ping_in_x_seconds"), DEFAULT_API_V1_LEASE_SECONDS
                )
                threshold = self._api_v1_refresh_threshold_seconds(lease)
                last = self._api_v1_last_heartbeat_at.get(candidate_url, 0.0)
                if threshold > 0 and time.monotonic() - float(last or 0.0) < threshold:
                    continue
                try:
                    response = self.register_api_v1_compute_node(candidate_url)
                except Exception as exc:
                    log_error(
                        "server.heartbeat.background_failed relay={} error={}",
                        _sanitize_relay_target(candidate_url),
                        str(exc),
                    )
                    continue
                if isinstance(response, dict) and not response.get("error"):
                    refreshed_lease = self._normalise_positive_seconds(
                        response.get("next_ping_in_x_seconds"), lease
                    )
                    relay_wait_hints[candidate_url] = {
                        "next_ping_in_x_seconds": refreshed_lease,
                        "poll_wait_seconds": self._normalise_poll_wait_seconds(
                            response.get("poll_wait_seconds", refreshed_lease)
                        ),
                        "server_public_key": self.crypto_manager.public_key_b64,
                    }
                    self._api_v1_registered_relays.add(candidate_url)
                    self._api_v1_last_heartbeat_at[candidate_url] = time.monotonic()
                    log_info(
                        "server.heartbeat.background relay={} lease_seconds={} key_fingerprint={}",
                        _sanitize_relay_target(candidate_url),
                        refreshed_lease,
                        self._api_v1_public_key_fingerprint(self.crypto_manager.public_key_b64),
                    )
        with self._api_v1_heartbeat_lock:
            if self._api_v1_heartbeat_thread is threading.current_thread():
                self._api_v1_heartbeat_thread = None

    @staticmethod
    def _api_v1_public_key_fingerprint(public_key: Any) -> str:
        """Return a short stable fingerprint for diagnostics without logging raw keys."""

        if not isinstance(public_key, str) or not public_key:
            return "unknown"
        digest = hashlib.sha256(public_key.encode("utf-8", errors="ignore")).hexdigest()
        return digest[:12]

    @staticmethod
    def _api_v1_refresh_threshold_seconds(lease_seconds: Any) -> float:
        """Return the local age at which a relay lease should be proactively renewed."""

        if isinstance(lease_seconds, bool):
            return 0.0
        try:
            lease = float(lease_seconds)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(lease) or lease <= 0:
            return 0.0
        return max(min(lease * 0.8, lease - 1.0), lease * 0.5)

    def api_v1_registration_fresh(self, relay_url: Optional[str] = None) -> bool:
        """Return whether the selected API v1 relay registration was recently confirmed."""

        candidate_url = relay_url or self.relay_url
        if candidate_url not in self._api_v1_registered_relays:
            return False
        hints = getattr(self, "_api_v1_relay_wait_hints", {}).get(candidate_url, {})
        last_heartbeat_at = self._api_v1_last_heartbeat_at.get(candidate_url)
        if not isinstance(last_heartbeat_at, (int, float)):
            return False
        lease_seconds = hints.get("next_ping_in_x_seconds", DEFAULT_API_V1_LEASE_SECONDS)
        try:
            lease = float(lease_seconds)
        except (TypeError, ValueError):
            lease = DEFAULT_API_V1_LEASE_SECONDS
        if not math.isfinite(lease) or lease <= 0:
            lease = DEFAULT_API_V1_LEASE_SECONDS
        return (time.monotonic() - float(last_heartbeat_at)) < lease

    @staticmethod
    def _compose_relay_url(base_url: str, port: Optional[int]) -> str:
        """Normalise relay targets into canonical URLs."""

        base = (base_url or '').strip()
        if not base:
            return ''
        base = base.rstrip('/')

        parsed = urlparse(base if '://' in base else f'http://{base}')
        scheme = parsed.scheme or 'http'
        netloc = parsed.netloc or parsed.path
        path = parsed.path if parsed.netloc else ''

        should_inject_port = parsed.port is None and port is not None

        if should_inject_port:
            hostname = parsed.hostname or ''
            host_for_netloc = hostname or (parsed.netloc or parsed.path or netloc)
            if host_for_netloc and ':' in host_for_netloc and not host_for_netloc.startswith('['):
                host_for_netloc = f"[{host_for_netloc}]"

            userinfo = ''
            if parsed.username:
                userinfo = parsed.username
                if parsed.password:
                    userinfo = f"{userinfo}:{parsed.password}"
                userinfo = f"{userinfo}@"

            netloc = f"{userinfo}{host_for_netloc}:{int(port)}"

        return urlunparse((scheme, netloc, path, '', '', '')).rstrip('/')

    @classmethod
    def _build_relay_targets(
        cls,
        primary_base: str,
        primary_port: Optional[int],
        additional: Union[List[Any], Tuple[Any, ...]],
        *,
        cluster_only: bool = False,
    ) -> List[str]:
        """Combine primary and additional relay endpoints into an ordered list."""

        targets: List[str] = []

        def _append(url: str, port: Optional[int] = None) -> None:
            normalised = cls._compose_relay_url(url, port)
            if not normalised:
                return
            if cluster_only and cls._is_local_url(normalised):
                return
            if normalised not in targets:
                targets.append(normalised)

        additional_entries: Union[List[Any], Tuple[Any, ...]] = additional or []

        if not cluster_only:
            _append(primary_base, primary_port)
        elif not additional_entries:
            raise ValueError("Cluster-only mode requires at least one relay target")

        for entry in additional_entries:
            if isinstance(entry, str):
                _append(entry)
            elif isinstance(entry, dict):
                base = entry.get('base_url') or entry.get('url') or entry.get('host')
                port = entry.get('port')
                if base:
                    _append(base, port)

        if not targets:
            raise ValueError("At least one relay target must be provided")

        return targets

    @staticmethod
    def _is_local_url(url: str) -> bool:
        """Determine whether the given URL resolves to a localhost target."""

        parsed = urlparse(url if '://' in url else f'http://{url}')
        hostname = (parsed.hostname or '').strip().lower()

        if not hostname:
            return True

        if hostname in {'localhost', '::1', '::'}:
            return True

        normalised = hostname.strip('[]')

        try:
            candidate_ip = ipaddress.ip_address(normalised)
        except ValueError:
            candidate_ip = None

        if candidate_ip and (candidate_ip.is_loopback or candidate_ip.is_unspecified):
            return True

        return False

    @property
    def relay_url(self) -> str:
        """Return the currently active relay endpoint."""

        return self._relay_urls[self._active_relay_index]

    @property
    def relay_urls(self) -> Tuple[str, ...]:
        """Expose configured relay endpoints for diagnostics."""

        return tuple(self._relay_urls)

    def _auth_headers(self) -> Dict[str, str]:
        """Return authentication headers when a registration token is configured."""

        if not self._registration_token:
            return {}
        return {"X-Relay-Server-Token": self._registration_token}

    def reset_api_v1_polling_session(self, *, clear_registration: bool = False) -> None:
        """Reset stop/unregister state before a fresh API v1 polling session.

        ``clear_registration`` forces the next poll to re-register with the relay, which is
        required after an explicit Stop because the relay-side lease may have been removed.
        """

        self.stop_polling = False
        self._polling_stopped_by_request = False
        self._unregister_attempted = False
        self._unregister_complete = False
        if clear_registration:
            self._api_v1_registered_relays.clear()
            self._api_v1_last_heartbeat_at.clear()
            getattr(self, "_api_v1_relay_wait_hints", {}).clear()

    def start(self):
        """Start the polling loop by setting stop_polling to False."""

        clear_registration = bool(
            getattr(self, "stop_polling", True)
            or getattr(self, "_polling_stopped_by_request", False)
            or getattr(self, "_unregister_complete", False)
        )
        self.reset_api_v1_polling_session(clear_registration=clear_registration)
        log_info(
            "Starting relay polling relay={} key_fingerprint={} registration_reset={}",
            _sanitize_relay_target(self.relay_url),
            self._api_v1_public_key_fingerprint(getattr(self.crypto_manager, "public_key_b64", None)),
            clear_registration,
        )

    def stop(self):
        """Stop the polling loop by setting stop_polling to True"""
        log_info("Stopping relay polling")
        self.stop_polling = True
        self._polling_stopped_by_request = True
        self._api_v1_stop_heartbeat_worker()

    def unregister_from_relay(self) -> bool:
        """Best-effort unregister call for graceful compute-node shutdown."""

        self._api_v1_stop_heartbeat_worker()

        registered_relays = getattr(self, "_api_v1_registered_relays", set())
        if not isinstance(registered_relays, set):
            registered_relays = set()
            self._api_v1_registered_relays = registered_relays

        if (
            getattr(self, "_unregister_attempted", False)
            and getattr(self, "_unregister_complete", False)
            and not registered_relays
        ):
            log_info("Compute node unregister already completed; skipping duplicate request")
            return True

        self._unregister_attempted = True

        last_error: Optional[str] = None
        failed_relays: Set[str] = set()
        unregistered_relays: Set[str] = set()

        relay_wait_hints = getattr(self, "_api_v1_relay_wait_hints", {})
        self._api_v1_relay_wait_hints = relay_wait_hints
        if not registered_relays:
            log_info("Compute node was not registered with an API v1 relay; skipping unregister")
            self._unregister_complete = True
            return True

        ordered_relay_urls = [
            self._relay_urls[(self._active_relay_index + offset) % len(self._relay_urls)]
            for offset in range(len(self._relay_urls))
        ]
        target_urls = [url for url in ordered_relay_urls if url in registered_relays]
        target_urls.extend(sorted(url for url in registered_relays if url not in set(target_urls)))

        relay_index_by_url = {url: index for index, url in enumerate(self._relay_urls)}

        for candidate_url in target_urls:
            try:
                request_kwargs = {
                    'json': {'server_public_key': self.crypto_manager.public_key_b64},
                }
                headers = self._auth_headers()
                if headers:
                    request_kwargs['headers'] = headers

                unregister_url = self._build_api_v1_url(candidate_url, "/relay/servers/unregister")
                response = requests.post(
                    unregister_url,
                    timeout=self._request_timeout,
                    **request_kwargs,
                )
                if response.status_code == 404:
                    legacy_base_url = candidate_url.rstrip('/')
                    if legacy_base_url.endswith('/api/v1'):
                        legacy_base_url = legacy_base_url[: -len('/api/v1')]
                    legacy_url = f"{legacy_base_url}/unregister"
                    response = requests.post(
                        legacy_url,
                        timeout=self._request_timeout,
                        **request_kwargs,
                    )
                if response.status_code == 200:
                    if candidate_url in relay_index_by_url:
                        self._active_relay_index = relay_index_by_url[candidate_url]
                    log_info("Unregistered compute node from relay {}", candidate_url)
                    unregistered_relays.add(candidate_url)
                    self._api_v1_registered_relays.discard(candidate_url)
                    self._api_v1_last_heartbeat_at.pop(candidate_url, None)
                    relay_wait_hints.pop(candidate_url, None)
                    continue

                failed_relays.add(candidate_url)
                diagnostic = self._api_v1_non_200_diagnostic(
                    response,
                    method="POST",
                    url=unregister_url,
                    token_sent=bool(headers),
                )
                last_error = f"HTTP {diagnostic['status_code']}"
                log_error(
                    "Failed to unregister compute node from {}: {}",
                    candidate_url,
                    last_error,
                )
            except requests.RequestException as exc:
                failed_relays.add(candidate_url)
                last_error = str(exc)
                log_error(
                    "Error unregistering compute node from {}: {}",
                    candidate_url,
                    last_error,
                    exc_info=True,
                )
            except Exception as exc:  # pragma: no cover - unexpected edge cases
                failed_relays.add(candidate_url)
                last_error = str(exc)
                log_error(
                    "Unexpected error unregistering compute node from {}: {}",
                    candidate_url,
                    last_error,
                    exc_info=True,
                )

        if failed_relays:
            self._unregister_complete = False
            if last_error:
                log_error("Unable to unregister compute node from relay: {}", last_error)
            return False

        self._unregister_complete = True
        if len(unregistered_relays) == len(target_urls):
            self._api_v1_registered_relays.clear()
            self._api_v1_last_heartbeat_at.clear()
            relay_wait_hints.clear()
        return True

    def ping_relay(self) -> Dict[str, Any]:
        """
        Send a ping to the relay server to register this server and check for client requests.

        Returns:
            Dict containing relay server response

        Raises:
            requests.ConnectionError: If connection to relay fails
            requests.Timeout: If the request times out
            requests.RequestException: For other request-related errors
            ValueError: If the server response is not valid JSON or fails schema validation
        """
        last_error: Optional[Dict[str, Any]] = None
        encountered_error = False

        relay_wait_hints = getattr(self, "_api_v1_relay_wait_hints", {})
        self._api_v1_relay_wait_hints = relay_wait_hints

        for offset in range(len(self._relay_urls)):
            index = (self._sink_start_index + offset) % len(self._relay_urls)
            candidate_url = self._relay_urls[index]

            try:
                log_info(
                    "Pinging relay {}/sink with key {}...",
                    candidate_url,
                    self.crypto_manager.public_key_b64[:10],
                )

                request_kwargs = {
                    'json': {'server_public_key': self.crypto_manager.public_key_b64},
                    'timeout': self._request_timeout,
                }
                headers = self._auth_headers()
                if headers:
                    request_kwargs['headers'] = headers

                timeout = request_kwargs.pop('timeout', self._request_timeout)
                response = requests.post(
                    f'{candidate_url}/sink',
                    timeout=timeout,
                    **request_kwargs,
                )

                if response.status_code != 200:
                    log_error(
                        "Error from relay /sink: status {} ({} bytes)",
                        response.status_code,
                        len(response.text),
                    )
                    last_error = {
                        'error': f"HTTP {response.status_code}",
                        'next_ping_in_x_seconds': self._request_timeout,
                    }
                    encountered_error = True
                    continue

                relay_response = response.json()
                try:
                    _validate_with_fallback(relay_response, RELAY_RESPONSE_SCHEMA)
                except ValueError as exc:
                    log_error("Invalid relay response format: {}", str(exc))
                    last_error = {
                        'error': f"Invalid response format: {str(exc)}",
                        'next_ping_in_x_seconds': self._request_timeout,
                    }
                    encountered_error = True
                    continue

                self._active_relay_index = index
                if encountered_error:
                    self._sink_start_index = index
                else:
                    self._sink_start_index = (index + 1) % len(self._relay_urls)
                return relay_response

            except requests.ConnectionError as exc:
                # Connection failures are expected during relay failover/startup probing.
                # Keep this log concise to avoid runaway traceback noise in long-running loops.
                log_error("Connection error when pinging relay: {}", str(exc))
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True
            except requests.Timeout as exc:
                log_error("Request timeout when pinging relay: {}", str(exc))
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True
            except requests.RequestException as exc:
                log_error("Request exception when pinging relay: {}", str(exc))
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True
            except json.JSONDecodeError as exc:
                log_error("Invalid JSON response from relay: {}", str(exc), exc_info=True)
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True
            except Exception as exc:  # pragma: no cover - unexpected edge cases
                log_error("Unexpected error when pinging relay: {}", str(exc), exc_info=True)
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True

        return last_error or {
            'error': 'No relay targets responded',
            'next_ping_in_x_seconds': self._request_timeout,
        }

    def _api_v1_non_200_diagnostic(
        self,
        response: Any,
        *,
        method: str,
        url: str,
        token_sent: bool,
    ) -> Dict[str, Any]:
        """Build and emit safe diagnostics for API v1 register/poll non-200 responses."""

        parsed_url = urlparse(url)
        path = parsed_url.path or "/"
        headers = _api_v1_response_headers(response)
        secrets = (
            self._registration_token or "",
            getattr(self.crypto_manager, "public_key_b64", ""),
        )
        body_snippet, parsed_json = _safe_api_v1_response_body_snippet(
            response,
            secrets=secrets,
        )
        relay_error = None
        if isinstance(parsed_json, dict):
            raw_error = parsed_json.get("error")
            if isinstance(raw_error, str):
                relay_error = _redact_sensitive_text(raw_error, secrets=secrets)
            elif isinstance(raw_error, dict):
                sanitized_error = _sanitize_api_v1_json_body(raw_error, secrets=secrets)
                relay_error = _redact_sensitive_text(
                    json.dumps(sanitized_error, sort_keys=True, separators=(",", ":")),
                    secrets=secrets,
                )

        server_header = headers.get("server", "")
        cf_ray = headers.get("cf-ray")
        status_code = int(getattr(response, "status_code", 0) or 0)
        probable_pre_app_rejection = (
            status_code == 403
            and (server_header.lower() == "cloudflare" or bool(cf_ray))
            and relay_error is None
        )
        if relay_error is not None:
            error_kind = "relay_json_error"
        elif probable_pre_app_rejection:
            error_kind = "cloudflare_pre_app_rejection"
        else:
            error_kind = "http_status_no_json_body"

        retry_after = headers.get("retry-after")
        control_plane_paths = {
            "/api/v1/relay/servers/register",
            "/api/v1/relay/servers/unregister",
            "/api/v1/relay/servers/poll",
            "/api/v1/relay/responses",
        }
        route_class = (
            "compute_node_control_plane" if path in control_plane_paths else "api_v1_relay"
        )
        diagnostic = {
            "method": method.upper(),
            "path": path,
            "status_code": status_code,
            "headers": headers,
            "body_snippet": body_snippet,
            "token_sent": token_sent,
            "relay_error": relay_error,
            "error_kind": error_kind,
            "probable_pre_app_rejection": probable_pre_app_rejection,
            "route_class": route_class,
            "retry_after": retry_after,
        }

        log_error(
            "api_v1.relay_http_error method={} path={} status={} token_sent={} headers={} body_snippet={}",
            diagnostic["method"],
            diagnostic["path"],
            diagnostic["status_code"],
            diagnostic["token_sent"],
            diagnostic["headers"],
            diagnostic["body_snippet"],
        )
        if status_code == 429 and route_class == "compute_node_control_plane":
            log_error(
                "relay_control_plane_rate_limited method={} path={} status={} retry_after={} token_sent={}",
                diagnostic["method"],
                diagnostic["path"],
                diagnostic["status_code"],
                retry_after or "unknown",
                diagnostic["token_sent"],
            )
        if probable_pre_app_rejection:
            log_error(
                "api_v1.relay_pre_app_rejection method={} path={} status={} cf_ray={} server={} token_sent={}",
                diagnostic["method"],
                diagnostic["path"],
                diagnostic["status_code"],
                headers.get("cf-ray", "none"),
                headers.get("server", "none"),
                diagnostic["token_sent"],
            )
        return diagnostic

    def _api_v1_http_error_result(
        self,
        response: Any,
        *,
        method: str,
        url: str,
        token_sent: bool,
        next_ping_in_x_seconds: Any,
    ) -> Dict[str, Any]:
        diagnostic = self._api_v1_non_200_diagnostic(
            response,
            method=method,
            url=url,
            token_sent=token_sent,
        )
        return {
            'error': f'HTTP {diagnostic["status_code"]}',
            'next_ping_in_x_seconds': next_ping_in_x_seconds,
            'http_status': diagnostic["status_code"],
            'relay_error_kind': diagnostic["error_kind"],
            'relay_error': diagnostic["relay_error"],
            'relay_http_diagnostic': diagnostic,
        }

    def register_api_v1_compute_node(self, relay_url: Optional[str] = None) -> Dict[str, Any]:
        target_url = relay_url or self.relay_url
        payload = {
            'server_public_key': self.crypto_manager.public_key_b64,
            'capabilities': self._api_v1_compute_node_capabilities(),
        }
        request_kwargs: Dict[str, Any] = {'json': payload, 'timeout': self._request_timeout}
        headers = self._auth_headers()
        if headers:
            request_kwargs['headers'] = headers
        register_url = self._build_api_v1_url(target_url, "/relay/servers/register")
        token_sent = bool(headers)
        response = requests.post(
            register_url,
            timeout=request_kwargs.pop('timeout'),
            **request_kwargs,
        )
        if response.status_code != 200:
            return self._api_v1_http_error_result(
                response,
                method="POST",
                url=register_url,
                token_sent=token_sent,
                next_ping_in_x_seconds=self._request_timeout,
            )
        return response.json()

    @staticmethod
    def _api_v1_model_path_basename(model_path: Any) -> Optional[str]:
        """Return a safe basename only for concrete path-like model paths."""

        if isinstance(model_path, (str, bytes, os.PathLike)):
            basename = os.path.basename(os.fspath(model_path))
            return basename if basename else None
        return None

    def _active_context_tier_can_satisfy(self, requested_context_tier: str) -> bool:
        """Return whether the active runtime profile can satisfy a requested tier."""

        active_context_tier = normalize_context_tier(
            getattr(self.model_manager, "context_tier", DEFAULT_CONTEXT_TIER)
        )
        try:
            active_profile = get_context_profile(active_context_tier)
            requested_profile = get_context_profile(requested_context_tier)
        except Exception:
            return False
        return active_profile.total_context_tokens >= requested_profile.total_context_tokens

    def _api_v1_supported_model_ids(self) -> List[str]:
        configured = [
            getattr(self.model_manager, "api_model_id", None),
            getattr(self.model_manager, "model_id", None),
            getattr(self.model_manager, "file_name", None),
            self._api_v1_model_path_basename(getattr(self.model_manager, "model_path", None)),
        ]
        model_ids = {
            str(value).strip().lower()
            for value in configured
            if isinstance(value, str) and value.strip()
        }
        supports_api_v1_model = getattr(self.model_manager, "supports_api_v1_model", None)
        manager_defines_supports_model = callable(
            getattr(type(self.model_manager), "supports_api_v1_model", None)
        )
        if callable(supports_api_v1_model) and manager_defines_supports_model:
            candidate_ids = set(model_ids)
            candidate_ids.update(self._API_V1_LOCAL_MODEL_ALIASES)
            candidate_ids.update(self._API_V1_LOCAL_MODEL_ALIASES.values())
            model_ids = {
                model_id
                for model_id in candidate_ids
                if supports_api_v1_model(model_id) is True
            }
        else:
            model_ids.update(self._api_v1_catalogue_ids_for_configured_runtime(model_ids))
        return sorted(model_id for model_id in model_ids if not model_id.endswith(".gguf"))

    def _api_v1_compute_node_capabilities(self) -> Dict[str, Any]:
        context_tier = normalize_context_tier(getattr(self.model_manager, "context_tier", DEFAULT_CONTEXT_TIER))
        profile = get_context_profile(context_tier)
        diagnostics = getattr(self.model_manager, "last_compute_diagnostics", None)
        backend_class = "unknown"
        if isinstance(diagnostics, dict):
            backend_class = str(
                diagnostics.get("backend_used")
                or diagnostics.get("backend_selected")
                or diagnostics.get("backend_available")
                or "unknown"
            ).strip().lower()
        if backend_class not in {"cpu", "cuda", "metal", "vulkan", "gpu", "unknown"}:
            backend_class = "unknown"
        return {
            "api_version": "v1",
            "supported_model_ids": self._api_v1_supported_model_ids(),
            "active_context_tier": profile.profile_id,
            "maximum_total_context_tokens": profile.total_context_tokens,
            "default_output_token_reservation": profile.default_output_reservation_tokens,
            "maximum_output_tokens": max(
                profile.default_output_reservation_tokens,
                self._API_V1_MAX_TOKENS_LIMIT,
            ),
            "max_concurrency": 1,
            "backend_class": backend_class,
        }

    @staticmethod
    def _build_api_v1_url(relay_url: str, route: str) -> str:
        """Build API v1 URLs without duplicating a pre-existing /api/v1 suffix."""
        base = relay_url.rstrip("/")
        normalized_route = route if route.startswith("/") else f"/{route}"
        if base.endswith("/api/v1"):
            return f"{base}{normalized_route}"
        return f"{base}/api/v1{normalized_route}"

    def _api_v1_poll_timeout_seconds(self, expected_wait_seconds: Any) -> float:
        """Return a safe poll timeout that exceeds the server-side long-poll wait."""
        base_timeout = float(self._request_timeout)
        if isinstance(expected_wait_seconds, bool):
            return base_timeout
        try:
            wait_seconds = float(expected_wait_seconds)
        except (TypeError, ValueError):
            return base_timeout
        if not math.isfinite(wait_seconds) or wait_seconds < 0:
            return base_timeout
        return max(base_timeout, wait_seconds + 5.0, wait_seconds * 1.25)

    def _normalise_positive_seconds(self, seconds: Any, fallback: Any) -> float:
        """Return a finite positive numeric hint, accepting numeric strings."""

        if isinstance(seconds, bool):
            seconds = None
        try:
            normalised = float(seconds)
        except (TypeError, ValueError):
            try:
                normalised = float(fallback)
            except (TypeError, ValueError):
                normalised = float(self._request_timeout)
        if not math.isfinite(normalised) or normalised <= 0:
            try:
                normalised = float(fallback)
            except (TypeError, ValueError):
                normalised = float(self._request_timeout)
        if not math.isfinite(normalised) or normalised <= 0:
            normalised = float(self._request_timeout)
        return normalised

    def poll_api_v1_encrypted_work(self) -> Dict[str, Any]:
        """Poll API v1 relay routes for encrypted work with lease-aware registration."""

        if getattr(self, "_polling_stopped_by_request", False):
            return {
                'error': 'Relay polling stopped',
                'next_ping_in_x_seconds': 0,
                'poll_wait_seconds': 0,
            }

        last_error: Optional[Dict[str, Any]] = None
        relay_wait_hints = getattr(self, "_api_v1_relay_wait_hints", {})
        self._api_v1_relay_wait_hints = relay_wait_hints

        for offset in range(len(self._relay_urls)):
            index = (self._active_relay_index + offset) % len(self._relay_urls)
            candidate_url = self._relay_urls[index]

            try:
                cached_hints = relay_wait_hints.get(candidate_url, {})
                register_wait = self._normalise_positive_seconds(
                    cached_hints.get('next_ping_in_x_seconds'),
                    self._request_timeout,
                )
                poll_wait = cached_hints.get('poll_wait_seconds', register_wait)
                poll_wait = self._normalise_poll_wait_seconds(poll_wait)
                current_public_key = self.crypto_manager.public_key_b64
                registered_public_key = cached_hints.get('server_public_key')
                requires_register = candidate_url not in self._api_v1_registered_relays
                reregister_reason: Optional[str] = None
                if requires_register:
                    reregister_reason = "not_registered"
                if (
                    not requires_register
                    and isinstance(registered_public_key, str)
                    and registered_public_key != current_public_key
                ):
                    requires_register = True
                    reregister_reason = "public_key_changed"
                if not requires_register:
                    last_heartbeat_at = self._api_v1_last_heartbeat_at.get(candidate_url)
                    refresh_threshold = self._api_v1_refresh_threshold_seconds(register_wait)
                    if (
                        not isinstance(last_heartbeat_at, (int, float))
                        or refresh_threshold <= 0
                        or time.monotonic() - float(last_heartbeat_at) >= refresh_threshold
                    ):
                        requires_register = True
                        reregister_reason = "lease_expiry_risk"

                if getattr(self, "_polling_stopped_by_request", False):
                    return {
                        'error': 'Relay polling stopped',
                        'next_ping_in_x_seconds': 0,
                        'poll_wait_seconds': 0,
                    }

                if requires_register:
                    if getattr(self, "_polling_stopped_by_request", False):
                        return {
                            'error': 'Relay polling stopped',
                            'next_ping_in_x_seconds': 0,
                            'poll_wait_seconds': 0,
                        }
                    if reregister_reason and reregister_reason != "not_registered":
                        log_info(
                            "server.reregister reason={} relay={} key_fingerprint={}",
                            reregister_reason,
                            candidate_url,
                            self._api_v1_public_key_fingerprint(current_public_key),
                        )
                    register_response = self.register_api_v1_compute_node(candidate_url)
                    if not isinstance(register_response, dict):
                        last_error = {
                            'error': 'Invalid register response format: expected object payload',
                            'next_ping_in_x_seconds': self._request_timeout,
                        }
                        continue
                    register_wait = self._normalise_positive_seconds(
                        register_response.get('next_ping_in_x_seconds'),
                        self._request_timeout,
                    )
                    poll_wait = register_response.get('poll_wait_seconds', register_wait)
                    poll_wait = self._normalise_poll_wait_seconds(poll_wait)
                    if register_response.get('error'):
                        last_error = dict(register_response)
                        last_error.setdefault('next_ping_in_x_seconds', register_wait)
                        continue
                    relay_wait_hints[candidate_url] = {
                        'next_ping_in_x_seconds': register_wait,
                        'poll_wait_seconds': poll_wait,
                        'server_public_key': current_public_key,
                    }
                    self._api_v1_registered_relays.add(candidate_url)
                    self._api_v1_last_heartbeat_at[candidate_url] = time.monotonic()
                    self._unregister_complete = False
                    if getattr(self, "_polling_stopped_by_request", False):
                        self.unregister_from_relay()
                        return {
                            'error': 'Relay polling stopped',
                            'next_ping_in_x_seconds': 0,
                            'poll_wait_seconds': 0,
                        }
                    next_refresh = self._api_v1_refresh_threshold_seconds(register_wait)
                    log_info(
                        "server.registered relay={} lease_seconds={} next_refresh_seconds={} key_fingerprint={}",
                        candidate_url,
                        register_wait,
                        round(next_refresh, 3),
                        self._api_v1_public_key_fingerprint(current_public_key),
                    )

                request_kwargs: Dict[str, Any] = {
                    'json': {
                        'server_public_key': self.crypto_manager.public_key_b64,
                        'capabilities': self._api_v1_compute_node_capabilities(),
                    },
                    'timeout': self._api_v1_poll_timeout_seconds(poll_wait),
                }
                log_info(
                    "api_v1.poll_timeout relay={} poll_wait_seconds={} timeout_seconds={}",
                    candidate_url,
                    poll_wait,
                    request_kwargs['timeout'],
                )
                headers = self._auth_headers()
                if headers:
                    request_kwargs['headers'] = headers

                if getattr(self, "_polling_stopped_by_request", False):
                    return {
                        'error': 'Relay polling stopped',
                        'next_ping_in_x_seconds': 0,
                        'poll_wait_seconds': 0,
                    }

                poll_timeout_seconds = float(request_kwargs.pop('timeout'))
                poll_url = self._build_api_v1_url(candidate_url, "/relay/servers/poll")
                token_sent = bool(headers)
                poll_started = time.monotonic()
                try:
                    response = requests.post(
                        poll_url,
                        timeout=poll_timeout_seconds,
                        **request_kwargs,
                    )
                except Exception as exc:
                    elapsed_seconds = time.monotonic() - poll_started
                    timeout_exception = (
                        isinstance(exc, requests.Timeout)
                        or "Read timed out" in str(exc)
                    )
                    can_try_another_relay = len(self._relay_urls) > 1
                    reached_server_long_poll = (
                        timeout_exception
                        and not can_try_another_relay
                        and math.isfinite(float(poll_wait))
                        and float(poll_wait) > 0
                        and elapsed_seconds >= max(0.0, float(poll_wait) - 0.5)
                    )
                    if reached_server_long_poll and candidate_url in self._api_v1_registered_relays:
                        self._api_v1_last_heartbeat_at[candidate_url] = time.monotonic()
                        relay_wait_hints[candidate_url] = {
                            'next_ping_in_x_seconds': register_wait,
                            'poll_wait_seconds': poll_wait,
                            'server_public_key': current_public_key,
                        }
                        next_refresh = self._api_v1_refresh_threshold_seconds(register_wait)
                        log_info(
                            "api_v1.poll_timeout_no_work relay={} poll_wait_seconds={} timeout_seconds={} "
                            "lease_seconds={} next_refresh_seconds={} key_fingerprint={} error={}",
                            candidate_url,
                            poll_wait,
                            poll_timeout_seconds,
                            register_wait,
                            round(next_refresh, 3),
                            self._api_v1_public_key_fingerprint(current_public_key),
                            str(exc),
                        )
                        return {
                            'message': 'No requests available',
                            'next_ping_in_x_seconds': 0 if poll_wait > 0 else register_wait,
                            'poll_wait_seconds': poll_wait,
                        }
                    raise
                if response.status_code != 200:
                    if response.status_code == 404:
                        self._api_v1_registered_relays.discard(candidate_url)
                        self._api_v1_last_heartbeat_at.pop(candidate_url, None)
                        relay_wait_hints.pop(candidate_url, None)
                        log_info(
                            "server.reregister reason=unknown_node relay={} key_fingerprint={}",
                            candidate_url,
                            self._api_v1_public_key_fingerprint(current_public_key),
                        )
                    last_error = self._api_v1_http_error_result(
                        response,
                        method="POST",
                        url=poll_url,
                        token_sent=token_sent,
                        next_ping_in_x_seconds=0 if response.status_code == 404 else register_wait,
                    )
                    continue
                payload = response.json()
                if not isinstance(payload, dict):
                    last_error = {
                        'error': 'Invalid response format: expected object payload',
                        'next_ping_in_x_seconds': register_wait,
                    }
                    continue
                payload_wait = payload.get('next_ping_in_x_seconds')
                normalised_payload_wait: Optional[float] = None
                if not isinstance(payload_wait, bool):
                    try:
                        candidate_payload_wait = float(payload_wait)
                    except (TypeError, ValueError):
                        candidate_payload_wait = math.nan
                    if math.isfinite(candidate_payload_wait) and candidate_payload_wait > 0:
                        normalised_payload_wait = candidate_payload_wait
                if normalised_payload_wait is None:
                    payload.setdefault('next_ping_in_x_seconds', register_wait)
                else:
                    register_wait = normalised_payload_wait
                    payload['next_ping_in_x_seconds'] = normalised_payload_wait
                payload_poll_wait = payload.get('poll_wait_seconds', poll_wait)
                poll_wait = self._normalise_poll_wait_seconds(payload_poll_wait)
                relay_wait_hints[candidate_url] = {
                    'next_ping_in_x_seconds': register_wait,
                    'poll_wait_seconds': poll_wait,
                    'server_public_key': current_public_key,
                }
                self._api_v1_last_heartbeat_at[candidate_url] = time.monotonic()
                self._active_relay_index = index
                self._last_api_v1_work_relay_url = candidate_url
                next_refresh = self._api_v1_refresh_threshold_seconds(register_wait)
                log_info(
                    "server.heartbeat relay={} lease_seconds={} next_refresh_seconds={} key_fingerprint={}",
                    candidate_url,
                    register_wait,
                    round(next_refresh, 3),
                    self._api_v1_public_key_fingerprint(current_public_key),
                )
                log_info(
                    "API v1 relay poll route=/api/v1/relay/servers/poll api_v1_payload={} request_id={}",
                    payload.get('protocol') == 'tokenplace_api_v1_relay_e2ee',
                    payload.get('request_id', 'none'),
                )
                return payload
            except Exception as exc:
                log_error("API v1 relay poll failed for {}: {}", candidate_url, str(exc), exc_info=True)
                self._api_v1_registered_relays.discard(candidate_url)
                self._api_v1_last_heartbeat_at.pop(candidate_url, None)
                relay_wait_hints.pop(candidate_url, None)
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}

        return last_error or {
            'error': 'No relay targets responded',
            'next_ping_in_x_seconds': self._request_timeout,
        }

    def _api_v1_response_relay_url(self) -> str:
        """Return the relay URL that supplied the current API v1 work item."""

        return self._last_api_v1_work_relay_url or self.relay_url

    @staticmethod
    def _api_v1_response_envelope(
        request_id: str,
        *,
        message: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build an encrypted API v1 relay response envelope body."""

        api_v1_response: Dict[str, Any]
        if error is not None:
            api_v1_response = {"error": error}
        else:
            api_v1_response = {"message": message}
        return {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": request_id,
            "api_v1_response": api_v1_response,
        }

    def _post_api_v1_response(
        self,
        response_envelope: Dict[str, Any],
        *,
        client_pub_key_b64: str,
        client_pub_key: bytes,
    ) -> bool:
        """Encrypt and submit an API v1 response to the relay that supplied work."""

        try:
            bound_response_envelope = {
                **response_envelope,
                "client_public_key": client_pub_key_b64,
            }
            encrypted_response = self.crypto_manager.encrypt_message(
                bound_response_envelope,
                client_pub_key,
            )
            source_payload = {
                "client_public_key": client_pub_key_b64,
                "request_id": response_envelope["request_id"],
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                **encrypted_response,
            }
            request_kwargs = {
                "json": source_payload,
            }
            headers = self._auth_headers()
            if headers:
                request_kwargs["headers"] = headers

            response_relay_url = self._api_v1_response_relay_url()
            response_url = self._build_api_v1_url(
                response_relay_url,
                "/relay/responses",
            )
            relay_target = _sanitize_relay_target(response_relay_url)
            source_response = requests.post(
                response_url,
                timeout=self._request_timeout,
                **request_kwargs,
            )
            submitted = source_response.status_code == 200
            route = "/api/v1/relay/responses"
            protocol = "tokenplace_api_v1_relay_e2ee"
            if submitted:
                log_info(
                    "API v1 E2EE response submission request_id={} protocol={} route={} relay={} submitted={}",
                    response_envelope["request_id"],
                    protocol,
                    route,
                    relay_target,
                    submitted,
                )
            else:
                log_error(
                    "API v1 E2EE response submission failed request_id={} protocol={} route={} relay={} http_status={}",
                    response_envelope["request_id"],
                    protocol,
                    route,
                    relay_target,
                    source_response.status_code,
                )
            return submitted
        except Exception:
            log_error(
                "Failed to encrypt or post API v1 response request_id={} protocol={} route={}",
                response_envelope.get("request_id"),
                response_envelope.get("protocol", "tokenplace_api_v1_relay_e2ee"),
                "/api/v1/relay/responses",
                exc_info=True,
            )
            return False


    def submit_api_v1_error_response(
        self,
        request_data: Dict[str, Any],
        *,
        code: str,
        message: str,
    ) -> bool:
        """Submit a structured encrypted API v1 error response for an unprocessed work item."""

        required_string_fields = ("request_id", "client_public_key", "chat_history", "cipherkey", "iv")
        if (
            not isinstance(request_data, dict)
            or request_data.get("protocol") != "tokenplace_api_v1_relay_e2ee"
            or request_data.get("version") != 1
            or not all(isinstance(request_data.get(field), str) for field in required_string_fields)
        ):
            log_error("Cannot submit API v1 error response for invalid relay payload")
            return False

        client_pub_key_b64 = _normalize_client_public_key_b64(request_data.get("client_public_key"))
        if client_pub_key_b64 is None:
            log_error("Cannot submit API v1 error response: invalid client_public_key metadata")
            return False

        try:
            client_pub_key = base64.b64decode(client_pub_key_b64, validate=True)
        except (AttributeError, binascii.Error, ValueError):
            log_error("Cannot submit API v1 error response: invalid client_public_key encoding")
            return False

        response_envelope = self._api_v1_response_envelope(
            request_data["request_id"],
            error={"code": code, "message": message},
        )
        return self._post_api_v1_response(
            response_envelope,
            client_pub_key_b64=client_pub_key_b64,
            client_pub_key=client_pub_key,
        )

    @staticmethod
    def _valid_api_v1_assistant_message(message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return None
        if message.get("role") != "assistant":
            return None
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        if isinstance(content, str) and content.strip():
            return dict(message)
        if isinstance(tool_calls, list) and tool_calls:
            return dict(message)
        return None

    @classmethod
    def _messages_are_valid_api_v1_chat(cls, messages: Any) -> bool:
        return cls._validate_api_v1_chat_messages(messages).valid

    @classmethod
    def _validate_api_v1_chat_messages(cls, messages: Any) -> _ApiV1ChatValidationResult:
        if (
            not isinstance(messages, list)
            or not messages
            or len(messages) > cls._API_V1_MAX_MESSAGES
        ):
            message_count = len(messages) if isinstance(messages, list) else 0
            return _ApiV1ChatValidationResult(
                False,
                "compute_node_invalid_request",
                "invalid_message_list",
                message_count,
            )
        total_content_chars = 0
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_invalid_request",
                    "message_not_object",
                    len(messages),
                    index,
                    total_content_chars=total_content_chars,
                )
            allowed_keys = {"role", "content", "name"}
            if set(message) - allowed_keys:
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_invalid_request",
                    "unknown_message_keys",
                    len(messages),
                    index,
                    total_content_chars=total_content_chars,
                )
            role = message.get("role")
            if (
                not isinstance(role, str)
                or role not in cls._API_V1_ALLOWED_MESSAGE_ROLES
            ):
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_invalid_request",
                    "invalid_role",
                    len(messages),
                    index,
                    total_content_chars=total_content_chars,
                )
            name = message.get("name")
            if name is not None and (not isinstance(name, str) or len(name) > 128):
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_invalid_request",
                    "invalid_name",
                    len(messages),
                    index,
                    total_content_chars=total_content_chars,
                )
            if "content" not in message:
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_invalid_request",
                    "missing_content",
                    len(messages),
                    index,
                    total_content_chars=total_content_chars,
                )
            content_size = cls._api_v1_content_validation_size(message.get("content"))
            if content_size is None:
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_invalid_request",
                    "invalid_content",
                    len(messages),
                    index,
                    total_content_chars=total_content_chars,
                )
            total_content_chars += content_size
            if total_content_chars > cls._API_V1_MAX_TOTAL_REQUEST_CHARS:
                return _ApiV1ChatValidationResult(
                    False,
                    "compute_node_request_too_large",
                    "aggregate_content_too_large",
                    len(messages),
                    index,
                    content_size,
                    total_content_chars,
                )
        return _ApiV1ChatValidationResult(
            True,
            message_count=len(messages),
            total_content_chars=total_content_chars,
        )

    @classmethod
    def _log_api_v1_chat_validation_rejection(
        cls, result: _ApiV1ChatValidationResult
    ) -> None:
        log_error(
            (
                "api_v1.chat_validation_rejected safe_error_code={} reason={} "
                "message_count={} message_index={} message_content_chars={} "
                "total_content_chars={} maximum_total_content_chars={}"
            ),
            result.code or "compute_node_invalid_request",
            result.reason or "unknown",
            result.message_count,
            result.message_index if result.message_index is not None else "none",
            (
                result.message_content_chars
                if result.message_content_chars is not None
                else "unknown"
            ),
            result.total_content_chars,
            cls._API_V1_MAX_TOTAL_REQUEST_CHARS,
        )

    @classmethod
    def _api_v1_chat_validation_error(
        cls, result: _ApiV1ChatValidationResult
    ) -> Dict[str, Any]:
        if result.code == "compute_node_request_too_large":
            return {
                "code": "compute_node_request_too_large",
                "type": "validation_error",
                "message": "API v1 request message content exceeds the aggregate safety limit",
                "message_count": result.message_count,
                "total_content_chars": result.total_content_chars,
                "maximum_total_content_chars": cls._API_V1_MAX_TOTAL_REQUEST_CHARS,
                "retryable": False,
            }
        return {
            "code": "compute_node_invalid_request",
            "message": "Invalid chat message format",
        }

    @staticmethod
    def _api_v1_stringify_content_blocks(content: Any) -> Any:
        """Collapse text-only OpenAI-style content blocks for llama.cpp."""

        if isinstance(content, str) or content is None:
            return content
        if not isinstance(content, list):
            return content

        segments: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")
            if block_type in {"input_text", "text"}:
                text_value = block.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    segments.append(text_value.strip())
                continue

        if not segments:
            return ""
        return "\n\n".join(segments)

    @classmethod
    def _normalise_api_v1_chat_messages(
        cls, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Return API v1 messages with text content blocks collapsed to text."""

        normalised: List[Dict[str, Any]] = []
        for message in messages:
            updated = dict(message)
            updated["content"] = cls._api_v1_stringify_content_blocks(
                message.get("content")
            )
            normalised.append(updated)
        return normalised

    @staticmethod
    def _api_v1_adapter_system_message(model_id: str) -> Optional[Dict[str, str]]:
        """Return local API v1 adapter instructions that do not require server imports."""

        adapter_instructions = {}
        instructions = adapter_instructions.get(model_id.strip().lower())
        if not instructions:
            return None
        return {
            "role": "system",
            "name": f"adapter:{model_id.strip().lower()}",
            "content": instructions,
        }

    @classmethod
    def _prepare_api_v1_runtime_messages(
        cls, model_id: str, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Apply API v1 server-compatible adapter and content normalization."""

        prepared = cls._normalise_api_v1_chat_messages(messages)
        adapter_message = cls._api_v1_adapter_system_message(model_id)
        if adapter_message is None:
            return prepared

        already_injected = any(
            message.get("role") == "system"
            and message.get("name") == adapter_message["name"]
            for message in prepared
        )
        if not already_injected:
            prepared.insert(0, adapter_message)
        return prepared

    @classmethod
    def _api_v1_qwen_no_think_messages(cls, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Inject Qwen's template-supported non-thinking control into user text."""

        message_control = cls._API_V1_QWEN_NON_THINKING_POLICY["message_control"]
        prepared = [dict(message) for message in messages]
        for index in range(len(prepared) - 1, -1, -1):
            if prepared[index].get("role") != "user":
                continue
            content = prepared[index].get("content")
            if isinstance(content, str):
                prepared[index]["content"] = f"{message_control}\n{content}"
                return prepared
            if isinstance(content, list):
                copied_blocks = [dict(block) if isinstance(block, dict) else block for block in content]
                for block in copied_blocks:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        block["text"] = f"{message_control}\n{block['text']}"
                        prepared[index]["content"] = copied_blocks
                        return prepared
                copied_blocks.insert(0, {"type": "text", "text": f"{message_control}\n"})
                prepared[index]["content"] = copied_blocks
                return prepared
        return prepared

    @classmethod
    def _api_v1_qwen_non_thinking_required(cls, model_profile: Dict[str, Any]) -> bool:
        return (
            isinstance(model_profile, dict)
            and model_profile.get("provider") == "qwen"
            and model_profile.get("thinking_mode")
            == cls._API_V1_QWEN_NON_THINKING_POLICY["thinking_mode"]
        )

    @classmethod
    def _api_v1_prepare_qwen_non_thinking_messages(
        cls, messages: List[Dict[str, Any]], model_profile: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if cls._api_v1_qwen_non_thinking_required(model_profile):
            return cls._api_v1_qwen_no_think_messages(messages)
        return messages

    @classmethod
    def _api_v1_qwen_thinking_leaked(
        cls, model_profile: Dict[str, Any], content: Any
    ) -> bool:
        return (
            cls._api_v1_qwen_non_thinking_required(model_profile)
            and isinstance(content, str)
            and bool(re.search(r"<\s*think", content, flags=re.IGNORECASE))
        )

    @classmethod
    def _api_v1_normalize_qwen_non_thinking_content(
        cls, model_profile: Dict[str, Any], content: Any
    ) -> Tuple[Optional[str], Optional[str]]:
        """Strip empty leading Qwen think wrappers while rejecting reasoning."""

        if not isinstance(content, str):
            return None, "unsupported_completion_shape"

        if not cls._api_v1_qwen_non_thinking_required(model_profile):
            cleaned = content.strip()
            return (cleaned, None) if cleaned else (None, "empty_content")

        remaining = content.lstrip()
        empty_wrapper_pattern = re.compile(
            r"^<\s*think\s*>\s*</\s*think\s*>",
            flags=re.IGNORECASE,
        )
        stripped_wrapper = False
        while True:
            match = empty_wrapper_pattern.match(remaining)
            if not match:
                break
            stripped_wrapper = True
            remaining = remaining[match.end():].lstrip()

        if re.search(r"<\s*/?\s*think\b|<\s*think", remaining, flags=re.IGNORECASE):
            return None, "qwen_thinking_output_leaked"

        cleaned = remaining.strip()
        if not cleaned:
            if stripped_wrapper:
                return None, "qwen_empty_after_think_wrapper_strip"
            return None, "empty_content"

        return cleaned, None

    @classmethod
    def _api_v1_qwen_reasoning_content_leaked(
        cls, model_profile: Dict[str, Any], payload: Any
    ) -> bool:
        if not (
            cls._api_v1_qwen_non_thinking_required(model_profile)
            and cls._API_V1_QWEN_NON_THINKING_POLICY["reasoning_content_forbidden"]
        ):
            return False
        forbidden_reasoning_fields = {"reasoning_content", "reasoning"}
        if isinstance(payload, dict):
            if any(payload.get(field) not in (None, "") for field in forbidden_reasoning_fields):
                return True
            return any(
                cls._api_v1_qwen_reasoning_content_leaked(model_profile, value)
                for value in payload.values()
            )
        if isinstance(payload, list):
            return any(
                cls._api_v1_qwen_reasoning_content_leaked(model_profile, item)
                for item in payload
            )
        return False

    @staticmethod
    def _api_v1_models_module() -> Optional[Any]:
        """Return the optional repo API v1 models module when it is importable."""

        try:
            return importlib.import_module("api.v1.models")
        except Exception:
            return None

    @staticmethod
    def _normalised_model_ids_from_api_v1_entry(entry: Dict[str, Any]) -> Set[str]:
        """Extract comparable model identifiers from one API v1 catalogue entry."""

        ids = {
            str(value).strip().lower()
            for value in (
                entry.get("id"),
                entry.get("base_model_id"),
                entry.get("file_name"),
                os.path.basename(str(entry.get("file_name", ""))),
            )
            if value
        }
        return {model_id for model_id in ids if model_id}

    @classmethod
    def _api_v1_local_model_ids_for_configured_runtime(
        cls, configured_ids: Set[str]
    ) -> Set[str]:
        """Return API v1 IDs supported by the active configured runtime profile."""

        return cls._api_v1_catalogue_ids_for_configured_runtime(configured_ids)

    @classmethod
    def _api_v1_requested_model_ids(cls, model_id: str) -> Set[str]:
        """Return normalized API v1 ids that are equivalent for local matching."""

        normalized_model = model_id.strip().lower()
        ids = {normalized_model}
        alias_target = cls._API_V1_LOCAL_MODEL_ALIASES.get(normalized_model)
        if alias_target:
            ids.add(alias_target)
        adapter_base = cls._API_V1_LOCAL_ADAPTER_BASE_MODELS.get(normalized_model)
        if adapter_base:
            ids.add(adapter_base)
        ids.add(cls._api_v1_catalogue_resolved_model_id(normalized_model))
        return {value for value in ids if value}

    @classmethod
    def _api_v1_catalogue_ids_for_configured_runtime(
        cls, configured_ids: Set[str]
    ) -> Set[str]:
        """Return API v1 catalogue IDs served by the configured local runtime."""

        models_module = cls._api_v1_models_module()
        if models_module is None:
            return set()

        get_models_info = getattr(models_module, "get_models_info", None)
        if not callable(get_models_info):
            return set()

        try:
            entries = get_models_info()
        except Exception:
            return set()

        runtime_catalogue_ids: Set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_ids = cls._normalised_model_ids_from_api_v1_entry(entry)
            if entry_ids & configured_ids:
                runtime_catalogue_ids.update(entry_ids)

        return runtime_catalogue_ids

    @classmethod
    def _api_v1_catalogue_resolved_model_id(cls, model_id: str) -> str:
        """Resolve API v1 model aliases when the catalogue module is available."""

        models_module = cls._api_v1_models_module()
        if models_module is None:
            return model_id

        resolve_model_alias = getattr(models_module, "resolve_model_alias", None)
        if callable(resolve_model_alias):
            try:
                resolved = resolve_model_alias(model_id)
            except Exception:
                resolved = None
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip().lower()

        aliases = getattr(models_module, "MODEL_ALIASES", {})
        if isinstance(aliases, dict):
            resolved = aliases.get(model_id)
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip().lower()

        return model_id

    def _runtime_model_can_satisfy(self, model_id: str) -> bool:
        """Return whether the attached desktop runtime can serve the requested model."""

        normalized_model = model_id.strip().lower()
        if not normalized_model:
            return False

        supports_api_v1_model = getattr(self.model_manager, "supports_api_v1_model", None)
        manager_defines_supports_model = callable(
            getattr(type(self.model_manager), "supports_api_v1_model", None)
        )
        if callable(supports_api_v1_model) and manager_defines_supports_model:
            requested_ids = self._api_v1_requested_model_ids(normalized_model)
            return any(
                supports_api_v1_model(requested_model_id) is True
                for requested_model_id in requested_ids
            )

        if getattr(self.model_manager, "use_mock_llm", False) is True:
            return True

        configured_ids = {
            str(value).strip().lower()
            for value in (
                getattr(self.model_manager, "api_model_id", None),
                getattr(self.model_manager, "model_id", None),
                getattr(self.model_manager, "file_name", None),
                self._api_v1_model_path_basename(getattr(self.model_manager, "model_path", None)),
            )
            if value
        }
        requested_ids = self._api_v1_requested_model_ids(normalized_model)
        if requested_ids & configured_ids:
            return True

        local_ids = self._api_v1_local_model_ids_for_configured_runtime(configured_ids)
        if requested_ids & local_ids:
            return True

        catalogue_ids = self._api_v1_catalogue_ids_for_configured_runtime(configured_ids)
        if requested_ids & catalogue_ids:
            return True

        return False

    @staticmethod
    def _api_v1_is_finite_number(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    @classmethod
    def _api_v1_normalise_numeric_option(
        cls,
        value: Any,
        *,
        minimum: Union[int, float],
        maximum: Union[int, float],
        integer: bool = False,
    ) -> Tuple[bool, Any]:
        if integer:
            if isinstance(value, bool) or not isinstance(value, int):
                return False, None
            normalised = value
        else:
            if not cls._api_v1_is_finite_number(value):
                return False, None
            normalised = float(value)
        if normalised < minimum or normalised > maximum:
            return False, None
        return True, normalised

    @classmethod
    def _api_v1_normalise_stop_option(cls, value: Any) -> Tuple[bool, Any]:
        if isinstance(value, str):
            if not value or len(value) > cls._API_V1_MAX_STOP_CHARS:
                return False, None
            return True, value
        if not isinstance(value, list) or len(value) > cls._API_V1_MAX_STOP_SEQUENCES:
            return False, None
        normalised = []
        for item in value:
            if not isinstance(item, str) or not item or len(item) > cls._API_V1_MAX_STOP_CHARS:
                return False, None
            normalised.append(item)
        return True, normalised

    @staticmethod
    def _api_v1_rejected_options_summary(option_names: List[str]) -> str:
        displayed_option_limit = 5
        displayed = option_names[:displayed_option_limit]
        remaining_count = len(option_names) - len(displayed)
        summary = ", ".join(displayed)
        if remaining_count > 0:
            summary = f"{summary}, and {remaining_count} more option(s)"
        return summary

    @classmethod
    def _api_v1_validate_and_normalise_options(
        cls, options: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        supported = {
            "frequency_penalty",
            "max_tokens",
            "presence_penalty",
            "seed",
            "stop",
            "stream",
            "temperature",
            "top_p",
        }
        intentionally_unsupported = {
            "function_call",
            "functions",
            "logit_bias",
            "logprobs",
            "n",
            "parallel_tool_calls",
            "response_format",
            "tool_choice",
            "tools",
            "top_logprobs",
        }
        if not all(isinstance(key, str) for key in options):
            return False, "compute_node_invalid_request", "option name", {}

        option_names = sorted(options)
        unsupported = [key for key in option_names if key in intentionally_unsupported]
        unknown = [
            key
            for key in option_names
            if key not in supported and key not in intentionally_unsupported
        ]
        if unsupported or unknown:
            return (
                False,
                "compute_node_options_unsupported",
                cls._api_v1_rejected_options_summary(unsupported + unknown),
                {},
            )

        normalised: Dict[str, Any] = {}
        for key, value in options.items():
            valid = True
            normalised_value = value
            if key == "stream":
                if value is not False:
                    return False, "compute_node_options_unsupported", "stream", {}
                continue
            if key == "max_tokens":
                valid, normalised_value = cls._api_v1_normalise_numeric_option(
                    value,
                    minimum=1,
                    maximum=cls._API_V1_MAX_TOKENS_LIMIT,
                    integer=True,
                )
            elif key == "seed":
                valid, normalised_value = cls._api_v1_normalise_numeric_option(
                    value,
                    minimum=0,
                    maximum=cls._API_V1_MAX_SEED,
                    integer=True,
                )
            elif key == "temperature":
                valid, normalised_value = cls._api_v1_normalise_numeric_option(
                    value, minimum=0.0, maximum=2.0
                )
            elif key == "top_p":
                valid, normalised_value = cls._api_v1_normalise_numeric_option(
                    value, minimum=0.0, maximum=1.0
                )
            elif key in {"frequency_penalty", "presence_penalty"}:
                valid, normalised_value = cls._api_v1_normalise_numeric_option(
                    value, minimum=-2.0, maximum=2.0
                )
            elif key == "stop":
                valid, normalised_value = cls._api_v1_normalise_stop_option(value)
            if not valid:
                return False, "compute_node_invalid_request", key, {}
            normalised[key] = normalised_value
        return True, None, None, normalised

    @classmethod
    def _api_v1_content_validation_size(cls, content: Any) -> Optional[int]:
        if isinstance(content, str):
            return len(content)
        if (
            not isinstance(content, list)
            or not content
            or len(content) > cls._API_V1_MAX_TEXT_BLOCKS
        ):
            return None
        total = 0
        for item in content:
            if not isinstance(item, dict) or set(item) - {"type", "text"}:
                return None
            item_type = item.get("type")
            if item_type not in {"input_text", "text"}:
                return None
            text = item.get("text")
            if not isinstance(text, str) or not text:
                return None
            total += len(text)
        return total


    @staticmethod
    def _api_v1_render_and_tokenize_chat_prompt(
        llm_instance: Any,
        messages: List[Dict[str, Any]],
        *,
        enable_thinking: Optional[bool] = None,
        model_profile: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Render and tokenize in one runtime bridge call when the facade exposes it."""

        render_and_tokenize = getattr(llm_instance, "render_and_tokenize_chat", None)
        if not callable(render_and_tokenize):
            return None
        kwargs = {"tokenize": False, "add_generation_prompt": True}
        model_manager = getattr(llm_instance, "model_manager", None)
        profile = model_profile or getattr(model_manager, "model_profile", None) or getattr(llm_instance, "model_profile", None) or {}
        if isinstance(profile, dict):
            if profile.get("provider"):
                kwargs["token_place_provider"] = profile.get("provider")
            if profile.get("chat_template_policy"):
                kwargs["token_place_template_policy"] = profile.get("chat_template_policy")
        if enable_thinking is not None:
            kwargs["enable_thinking"] = enable_thinking
        try:
            result = render_and_tokenize(messages, **kwargs)
        except TypeError:
            if enable_thinking is not None:
                logger.warning(
                    "api_v1.chat_template_render result=rejected "
                    "reason=enable_thinking_unsupported safe_error_code=%s",
                    "compute_node_context_admission_unavailable",
                )
            return None
        except Exception as exc:
            if _is_llama_cpp_inference_request_error(exc):
                diagnostics = getattr(exc, "diagnostics", None)
                if isinstance(diagnostics, dict):
                    safe_diagnostics = {}
                    for key, value in diagnostics.items():
                        if isinstance(key, str) and isinstance(value, (str, bool, int, float, type(None))):
                            safe_diagnostics[key] = value
                    llm_instance._token_place_last_render_tokenize_error = safe_diagnostics
                else:
                    llm_instance._token_place_last_render_tokenize_error = {}
            return None
        if isinstance(result, dict):
            prompt_tokens = result.get("prompt_tokens")
        else:
            prompt_tokens = result
        if isinstance(prompt_tokens, int) and prompt_tokens >= 0:
            if hasattr(llm_instance, "_token_place_last_render_tokenize_error"):
                delattr(llm_instance, "_token_place_last_render_tokenize_error")
            return prompt_tokens
        return None

    @staticmethod
    def _api_v1_tokenize_rendered_prompt(llm_instance: Any, rendered_prompt: str) -> Optional[int]:
        """Count prompt tokens with the active llama.cpp runtime tokenizer."""

        tokenize = getattr(llm_instance, "tokenize", None)
        if not callable(tokenize) or not isinstance(rendered_prompt, str):
            return None
        attempts = (
            lambda: tokenize(rendered_prompt.encode("utf-8"), add_bos=False),
            lambda: tokenize(rendered_prompt.encode("utf-8"), False),
            lambda: tokenize(rendered_prompt.encode("utf-8")),
            lambda: tokenize(rendered_prompt),
        )
        for tokenize_attempt in attempts:
            try:
                tokens = tokenize_attempt()
            except TypeError:
                continue
            except Exception:
                return None
            if isinstance(tokens, (list, tuple)):
                return len(tokens)
            return None
        return None

    @staticmethod
    def _api_v1_render_chat_prompt(
        llm_instance: Any,
        messages: List[Dict[str, Any]],
        *,
        enable_thinking: Optional[bool] = None,
        allow_chat_format_fallback: bool = True,
    ) -> Optional[str]:
        """Render chat messages with the active runtime chat template."""

        apply_chat_template = getattr(llm_instance, "apply_chat_template", None)
        if callable(apply_chat_template):
            try:
                kwargs = {"tokenize": False, "add_generation_prompt": True}
                if enable_thinking is not None:
                    kwargs["enable_thinking"] = enable_thinking
                rendered = apply_chat_template(messages, **kwargs)
            except TypeError:
                if enable_thinking is not None:
                    logger.warning(
                        "api_v1.chat_template_render result=rejected "
                        "reason=enable_thinking_unsupported safe_error_code=%s",
                        "compute_node_context_admission_unavailable",
                    )
                    return None
                try:
                    rendered = apply_chat_template(messages)
                except Exception:
                    rendered = None
            except Exception:
                rendered = None
            if isinstance(rendered, str):
                return rendered
        tokenizer = getattr(llm_instance, "tokenizer", None)
        if callable(tokenizer):
            try:
                tokenizer = tokenizer()
            except Exception:
                tokenizer = None
        if tokenizer is not None:
            tokenizer_template = getattr(tokenizer, "apply_chat_template", None)
            if callable(tokenizer_template):
                try:
                    kwargs = {"tokenize": False, "add_generation_prompt": True}
                    if enable_thinking is not None:
                        kwargs["enable_thinking"] = enable_thinking
                    rendered = tokenizer_template(messages, **kwargs)
                except Exception:
                    rendered = None
                if isinstance(rendered, str):
                    return rendered
        try:
            if not allow_chat_format_fallback:
                return None
            if not hasattr(llm_instance, "chat_format"):
                return None
            chat_format = getattr(llm_instance, "chat_format", None) or "llama-2"
            chat_format_module = RelayClient._api_v1_llama_chat_format_module()
            formatter_key = str(chat_format).replace("-", "_")
            formatter_name = (
                "format_llama2" if formatter_key == "llama_2" else "format_" + formatter_key
            )
            if formatter_name == "format_llama_3":
                formatter_name = "format_llama3"
            formatter = getattr(chat_format_module, formatter_name, None)
            if callable(formatter):
                rendered = formatter(
                    messages, tokenize=False, add_generation_prompt=True
                )
                prompt = getattr(rendered, "prompt", rendered)
                if isinstance(prompt, str):
                    return prompt
        except Exception:
            return None
        return None

    @staticmethod
    def _api_v1_llama_chat_format_module() -> Any:
        """Import llama.cpp chat formatting without the repo-local llama_cpp stub."""

        repo_root = os.path.abspath(os.getcwd())
        original_path = list(sys.path)
        original_parent_module = sys.modules.get("llama_cpp")
        parent_module_path = os.path.abspath(
            str(getattr(original_parent_module, "__file__", ""))
        )
        try:
            if (
                original_parent_module is not None
                and parent_module_path == os.path.join(repo_root, "llama_cpp.py")
            ):
                sys.modules.pop("llama_cpp", None)
            sys.path = [
                entry for entry in sys.path
                if entry and os.path.abspath(entry) != repo_root
            ]
            return importlib.import_module("llama_cpp.llama_chat_format")
        finally:
            sys.path = original_path
            if original_parent_module is not None:
                sys.modules["llama_cpp"] = original_parent_module

    def _api_v1_context_tier_unsupported_error(
        self,
        *,
        active_context_tier: str,
        configured_context_tokens: int,
        requested_context_tier: str,
        prompt_tokens: int,
        requested_output_tokens: int,
    ) -> Dict[str, Any]:
        return {
            "code": "compute_node_context_tier_unsupported",
            "type": "validation_error",
            "message": "Requested context tier is not active on this compute node",
            "active_context_tier": active_context_tier,
            "requested_context_tier": requested_context_tier,
            "configured_context_tokens": configured_context_tokens,
            "prompt_tokens": prompt_tokens,
            "requested_output_tokens": requested_output_tokens,
            "required_total_tokens": prompt_tokens + requested_output_tokens,
            "retryable": False,
        }

    def _api_v1_context_admission_unavailable_error(
        self,
        *,
        active_context_tier: str,
        configured_context_tokens: int,
        requested_context_tier: str,
        internal_reason: str = "runtime_template_tokenizer_bridge_unavailable",
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        error = {
            "code": "compute_node_context_admission_unavailable",
            "type": "validation_error",
            "message": (
                "Compute node cannot authoritatively render and tokenize the "
                "API v1 prompt for context admission"
            ),
            "active_context_tier": active_context_tier,
            "requested_context_tier": requested_context_tier,
            "configured_context_tokens": configured_context_tokens,
            "retryable": False,
            "internal_reason": internal_reason,
        }
        safe_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        for key in (
            "active_model_id",
            "active_profile_id",
            "context_tier",
            "template_policy",
            "non_thinking_mode",
            "runtime_facade_type",
            "direct_apply_chat_template_available",
            "metadata_template_available",
            "jinja_renderer_available",
        ):
            if key in safe_diagnostics:
                error[key] = safe_diagnostics[key]
        return error

    def _api_v1_context_admission_error(
        self,
        *,
        active_context_tier: str,
        configured_context_tokens: int,
        prompt_tokens: int,
        requested_output_tokens: int,
        requested_context_tier: str,
    ) -> Dict[str, Any]:
        required_total = prompt_tokens + requested_output_tokens
        recommended_context_tier = None
        retryable = False
        try:
            full_profile = get_context_profile("64k-full")
            requested_profile = get_context_profile(requested_context_tier)
            if (
                required_total <= full_profile.total_context_tokens
                and configured_context_tokens < full_profile.total_context_tokens
                and requested_profile.total_context_tokens <= full_profile.total_context_tokens
            ):
                recommended_context_tier = "64k-full"
                retryable = True
        except Exception:
            recommended_context_tier = None
        error = {
            "code": "compute_node_context_window_exceeded",
            "type": "validation_error",
            "message": "Requested prompt and output reservation exceed the active context window",
            "active_context_tier": active_context_tier,
            "configured_context_tokens": configured_context_tokens,
            "prompt_tokens": prompt_tokens,
            "requested_output_tokens": requested_output_tokens,
            "required_total_tokens": required_total,
            "retryable": retryable,
        }
        if recommended_context_tier is not None:
            error["recommended_context_tier"] = recommended_context_tier
        return error

    def _api_v1_authoritative_context_admission(
        self,
        *,
        llm_instance: Any,
        messages: List[Dict[str, Any]],
        requested_output_tokens: int,
        requested_context_tier: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[int]]:
        active_context_tier = normalize_context_tier(
            getattr(self.model_manager, "context_tier", DEFAULT_CONTEXT_TIER)
        )
        active_profile = get_context_profile(active_context_tier)
        configured_context_tokens = int(
            getattr(
                self.model_manager,
                "context_window_tokens",
                active_profile.total_context_tokens,
            )
            or active_profile.total_context_tokens
        )
        model_profile = getattr(self.model_manager, "model_profile", {}) or {}
        is_qwen_non_thinking = (
            self._api_v1_qwen_non_thinking_required(model_profile)
        )
        # Prefer a packaged-runtime bridge that renders and tokenizes inside the
        # loaded worker process. This keeps Qwen admission aligned with the same
        # GGUF/Jinja template surface used by generation and avoids returning or
        # logging rendered prompt text in the parent process.
        prompt_tokens = self._api_v1_render_and_tokenize_chat_prompt(
            llm_instance,
            messages,
            enable_thinking=None,
            model_profile=model_profile,
        )
        if prompt_tokens is None:
            rendered_prompt = self._api_v1_render_chat_prompt(
                llm_instance,
                messages,
                # Qwen generation below is controlled by the message-level
                # ``/no_think`` directive because llama-cpp-python's
                # ``create_chat_completion`` API does not expose template kwargs.
                # Admission must render the same message shape, rather than adding
                # an admission-only ``enable_thinking=False`` assistant prefix that
                # over-counts near-limit requests.
                enable_thinking=None,
                allow_chat_format_fallback=not is_qwen_non_thinking,
            )
            prompt_tokens = (
                self._api_v1_tokenize_rendered_prompt(llm_instance, rendered_prompt)
                if rendered_prompt is not None
                else None
            )
        if prompt_tokens is None:
            worker_diagnostics = getattr(llm_instance, "_token_place_last_render_tokenize_error", None)
            internal_reason = "runtime_template_tokenizer_bridge_unavailable"
            if isinstance(worker_diagnostics, dict) and isinstance(worker_diagnostics.get("reason"), str):
                internal_reason = worker_diagnostics["reason"]
            safe_diagnostics = {
                "active_model_id": getattr(self.model_manager, "api_model_id", None),
                "active_profile_id": model_profile.get("id") or model_profile.get("model_id"),
                "context_tier": active_context_tier,
                "template_policy": model_profile.get("chat_template_policy") or "llama-3",
                "non_thinking_mode": is_qwen_non_thinking,
                "runtime_facade_type": type(llm_instance).__name__,
                "direct_apply_chat_template_available": callable(getattr(llm_instance, "apply_chat_template", None)),
                "metadata_template_available": internal_reason != "runtime_chat_template_metadata_missing",
                "jinja_renderer_available": internal_reason != "runtime_chat_template_renderer_unavailable",
            }
            log_error(
                "api_v1.context_admission active_tier={} result=rejected reason={} safe_error_code={} model_id={} profile_id={} template_policy={} non_thinking={} runtime_facade={} direct_apply_chat_template_available={} metadata_template_available={} jinja_renderer_available={}",
                active_context_tier,
                internal_reason,
                "compute_node_context_admission_unavailable",
                safe_diagnostics["active_model_id"],
                safe_diagnostics["active_profile_id"],
                safe_diagnostics["template_policy"],
                safe_diagnostics["non_thinking_mode"],
                safe_diagnostics["runtime_facade_type"],
                safe_diagnostics["direct_apply_chat_template_available"],
                safe_diagnostics["metadata_template_available"],
                safe_diagnostics["jinja_renderer_available"],
            )
            return (
                False,
                self._api_v1_context_admission_unavailable_error(
                    active_context_tier=active_context_tier,
                    configured_context_tokens=configured_context_tokens,
                    requested_context_tier=requested_context_tier,
                    internal_reason=internal_reason,
                    diagnostics=safe_diagnostics,
                ),
                None,
            )
        required_total = prompt_tokens + requested_output_tokens
        tier_supported = self._active_context_tier_can_satisfy(requested_context_tier)
        admitted = tier_supported and required_total <= configured_context_tokens
        if admitted:
            safe_error_code = "none"
            admission_error = None
        elif not tier_supported and required_total <= configured_context_tokens:
            safe_error_code = "compute_node_context_tier_unsupported"
            admission_error = self._api_v1_context_tier_unsupported_error(
                active_context_tier=active_context_tier,
                configured_context_tokens=configured_context_tokens,
                requested_context_tier=requested_context_tier,
                prompt_tokens=prompt_tokens,
                requested_output_tokens=requested_output_tokens,
            )
        else:
            safe_error_code = "compute_node_context_window_exceeded"
            admission_error = self._api_v1_context_admission_error(
                active_context_tier=active_context_tier,
                configured_context_tokens=configured_context_tokens,
                prompt_tokens=prompt_tokens,
                requested_output_tokens=requested_output_tokens,
                requested_context_tier=requested_context_tier,
            )
        log_info(
            "api_v1.context_admission active_tier={} prompt_tokens={} output_reservation={} result={} duration_ms=0 safe_error_code={}",
            active_context_tier,
            prompt_tokens,
            requested_output_tokens,
            "admitted" if admitted else "rejected",
            safe_error_code,
        )
        if admitted:
            return True, None, prompt_tokens
        return False, admission_error, prompt_tokens

    def _api_v1_runtime_completion_kwargs(
        self, safe_options: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge API v1 options with model-manager defaults for direct completions."""

        config = getattr(self.model_manager, "config", None)
        config_get = getattr(config, "get", None)

        def configured(key: str, default: Any) -> Any:
            if callable(config_get):
                return config_get(key, default)
            return default

        completion_kwargs = {
            "max_tokens": configured("model.max_tokens", 512),
            "temperature": configured("model.temperature", 0.7),
            "top_p": configured("model.top_p", 0.9),
            "stop": configured("model.stop_tokens", []),
            "stream": False,
        }
        profile_defaults = (
            getattr(self.model_manager, "model_profile", {}) or {}
        ).get("generation_defaults") or {}
        for key in ("temperature", "top_p", "top_k"):
            if key in profile_defaults:
                completion_kwargs[key] = profile_defaults[key]
        completion_kwargs.update(safe_options)
        return completion_kwargs

    def _api_v1_enrich_safe_error(
        self,
        error: Dict[str, Any],
        *,
        request_id: str,
        requested_context_tier: str,
        prompt_tokens: Optional[int] = None,
        requested_output_tokens: Optional[int] = None,
        internal_reason: Optional[str] = None,
        rejected_option: Optional[str] = None,
        retryable: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Attach request-scoped, non-sensitive API v1 diagnostics to an error."""

        enriched = dict(error)
        enriched["request_id"] = request_id
        enriched.setdefault(
            "active_context_tier",
            normalize_context_tier(
                getattr(self.model_manager, "context_tier", DEFAULT_CONTEXT_TIER)
            ),
        )
        enriched.setdefault(
            "requested_context_tier", normalize_context_tier(requested_context_tier)
        )
        try:
            active_profile = get_context_profile(enriched["active_context_tier"])
        except Exception:
            active_profile = get_context_profile(DEFAULT_CONTEXT_TIER)
        configured_context_tokens = int(
            getattr(
                self.model_manager,
                "context_window_tokens",
                active_profile.total_context_tokens,
            )
            or active_profile.total_context_tokens
        )
        enriched.setdefault("configured_context_tokens", configured_context_tokens)
        if isinstance(prompt_tokens, int) and prompt_tokens >= 0:
            enriched.setdefault("prompt_tokens", prompt_tokens)
        if isinstance(requested_output_tokens, int) and requested_output_tokens >= 0:
            enriched.setdefault("requested_output_tokens", requested_output_tokens)
        if internal_reason:
            enriched.setdefault("internal_reason", internal_reason)
        if rejected_option:
            enriched.setdefault("rejected_option", rejected_option)
        if retryable is not None:
            enriched.setdefault("retryable", retryable)
        runtime_health = getattr(self, "_last_api_v1_runtime_health", {})
        if not isinstance(runtime_health, dict):
            runtime_health = {}
        enriched.setdefault(
            "runtime_healthy", bool(runtime_health.get("runtime_healthy", True))
        )
        enriched.setdefault(
            "recovery_attempted", bool(runtime_health.get("recovery_attempted", False))
        )
        enriched.setdefault(
            "recovery_succeeded", bool(runtime_health.get("recovery_succeeded", False))
        )
        return enriched

    def _assistant_message_from_runtime_completion(
        self, completion: Any
    ) -> Optional[Dict[str, Any]]:
        """Extract an API v1 assistant message from direct llama.cpp output."""

        self._last_api_v1_invalid_model_output_reason = None
        if (
            isinstance(completion, dict)
            and isinstance(completion.get("choices"), list)
            and completion["choices"]
            and isinstance(completion["choices"][0], dict)
        ):
            choice = completion["choices"][0]
            if self._api_v1_qwen_reasoning_content_leaked(
                getattr(self.model_manager, "model_profile", {}) or {}, choice
            ):
                # Fail closed before normalizing the runtime choice so hidden
                # reasoning fields are never forwarded or echoed.
                self._last_api_v1_invalid_model_output_reason = "qwen_reasoning_content_leaked"
                return None
            raw_message = choice.get("message")
            if isinstance(raw_message, dict) and "role" not in raw_message and "content" in raw_message:
                raw_message = {**raw_message, "role": "assistant"}
            message = self._valid_api_v1_assistant_message(raw_message)
            if message is None and "text" in choice and "message" not in choice:
                text = choice.get("text")
                if isinstance(text, str) and text.strip():
                    message = {"role": "assistant", "content": text}
            model_profile = getattr(self.model_manager, "model_profile", {}) or {}
            if message is not None:
                cleaned_content, invalid_reason = (
                    self._api_v1_normalize_qwen_non_thinking_content(
                        model_profile, message.get("content")
                    )
                )
                if invalid_reason is not None:
                    self._last_api_v1_invalid_model_output_reason = invalid_reason
                    return None
                message = {"role": "assistant", "content": cleaned_content}
            if message is None:
                self._last_api_v1_invalid_model_output_reason = "unsupported_completion_shape"
            return message

        # API v1 relay inference is explicitly non-streaming; runtimes must return
        # a complete chat completion object for this path.
        self._last_api_v1_invalid_model_output_reason = "unsupported_completion_shape"
        return None

    @staticmethod
    def _api_v1_safe_completion_shape(completion: Any) -> Dict[str, Any]:
        """Return safe diagnostics for a runtime completion without model text."""

        shape: Dict[str, Any] = {"type": type(completion).__name__}
        if isinstance(completion, dict):
            shape["keys"] = sorted(str(key) for key in completion.keys())[:10]
            choices = completion.get("choices")
            if isinstance(choices, list):
                shape["choices_type"] = "list"
                shape["choices_count"] = len(choices)
                if choices and isinstance(choices[0], dict):
                    choice = choices[0]
                    shape["choice_keys"] = sorted(str(key) for key in choice.keys())[:10]
                    message = choice.get("message")
                    if isinstance(message, dict):
                        shape["message_keys"] = sorted(str(key) for key in message.keys())[:10]
                        content = message.get("content")
                        shape["message_content_type"] = type(content).__name__
                    if "text" in choice:
                        shape["text_type"] = type(choice.get("text")).__name__
        return shape

    def _generate_api_v1_response_with_runtime_model(
        self,
        *,
        request_id: str,
        model_id: str,
        messages: List[Dict[str, Any]],
        options: Dict[str, Any],
        requested_context_tier: str = DEFAULT_CONTEXT_TIER,
    ) -> Dict[str, Any]:
        """Generate an API v1 assistant message with the desktop runtime model."""

        self._last_api_v1_runtime_health = {
            "runtime_healthy": True,
            "recovery_attempted": False,
            "recovery_succeeded": False,
        }
        self._last_api_v1_invalid_model_output_reason = None

        validation_result = self._validate_api_v1_chat_messages(messages)
        if not validation_result.valid:
            self._log_api_v1_chat_validation_rejection(validation_result)
            return self._api_v1_response_envelope(
                request_id,
                error=self._api_v1_chat_validation_error(validation_result),
            )

        get_llm_instance = getattr(self.model_manager, "get_llm_instance", None)
        recovery_completion = getattr(
            self.model_manager, "create_chat_completion_with_recovery", None
        )
        has_direct_runtime_completion = callable(get_llm_instance) or callable(
            recovery_completion
        )
        if not self._runtime_model_can_satisfy(model_id):
            return self._api_v1_response_envelope(
                request_id,
                error=self._api_v1_enrich_safe_error(
                    {
                        "code": "compute_node_model_unsupported",
                        "message": "Requested model is not available in the desktop runtime",
                    },
                    request_id=request_id,
                    requested_context_tier=requested_context_tier,
                ),
            )

        (
            options_supported,
            option_error_code,
            rejected_option,
            safe_options,
        ) = self._api_v1_validate_and_normalise_options(options)
        if not options_supported:
            error_code = option_error_code or "compute_node_options_unsupported"
            if error_code == "compute_node_invalid_request":
                error_message = (
                    "Requested option is invalid for the desktop runtime: "
                    f"{rejected_option}"
                )
            else:
                error_message = (
                    "Requested option is unsupported by the desktop runtime: "
                    f"{rejected_option}"
                )
            return self._api_v1_response_envelope(
                request_id,
                error=self._api_v1_enrich_safe_error(
                    {
                        "code": error_code,
                        "message": error_message,
                    },
                    request_id=request_id,
                    requested_context_tier=requested_context_tier,
                    internal_reason=(
                        "invalid_generation_option"
                        if error_code == "compute_node_invalid_request"
                        else "unsupported_generation_option"
                    ),
                    rejected_option=rejected_option,
                    retryable=False,
                ),
            )

        if not has_direct_runtime_completion:
            # API v1 desktop relay generation must fail closed rather than
            # falling back to legacy chat-history runtimes. This is intentional
            # for empty options and explicit stream:false requests as well.
            return self._api_v1_response_envelope(
                request_id,
                error=self._api_v1_enrich_safe_error(
                    {
                        "code": "compute_node_model_unsupported",
                        "message": (
                            "Desktop runtime does not expose API v1 non-streaming "
                            "chat completion"
                        ),
                    },
                    request_id=request_id,
                    requested_context_tier=requested_context_tier,
                    internal_reason="direct_runtime_completion_unavailable",
                    retryable=False,
                ),
            )

        runtime_messages = self._prepare_api_v1_runtime_messages(model_id, messages)
        model_profile = getattr(self.model_manager, "model_profile", {}) or {}
        # Use Qwen's documented /no_think message-level control before both
        # admission and generation.  llama-cpp-python's create_chat_completion
        # does not expose template kwargs, so admission intentionally renders the
        # same message shape instead of adding an admission-only
        # enable_thinking=False assistant prefix.  Output validation below still
        # fails closed if a runtime returns visible or hidden thinking output.
        runtime_messages = self._api_v1_prepare_qwen_non_thinking_messages(
            runtime_messages, model_profile
        )
        try:
            assistant_message: Optional[Dict[str, Any]] = None
            completion = None
            prompt_tokens: Optional[int] = None
            requested_output_tokens: Optional[int] = None
            llm_instance = get_llm_instance() if callable(get_llm_instance) else None
            if llm_instance is None and callable(recovery_completion):
                recover_runtime = getattr(
                    self.model_manager, "get_llm_instance_with_recovery", None
                )
                if callable(recover_runtime):
                    self._last_api_v1_runtime_health["recovery_attempted"] = True
                    llm_instance = recover_runtime()
                    self._last_api_v1_runtime_health["recovery_succeeded"] = (
                        llm_instance is not None
                    )
            if llm_instance is None and not callable(recovery_completion):
                self._last_api_v1_runtime_health = {
                    "runtime_healthy": False,
                    "recovery_attempted": False,
                    "recovery_succeeded": False,
                }
                log_error("Desktop runtime LLM initialization failed for API v1 relay request")
                return self._api_v1_response_envelope(
                    request_id,
                    error=self._api_v1_enrich_safe_error(
                        {
                            "code": "compute_node_internal_error",
                            "message": "Desktop runtime inference failed",
                        },
                        request_id=request_id,
                        requested_context_tier=requested_context_tier,
                        internal_reason="runtime_initialization_failed",
                    ),
                )

            completion_kwargs = self._api_v1_runtime_completion_kwargs(safe_options)
            requested_output_tokens = int(completion_kwargs["max_tokens"])
            admitted, admission_error, prompt_tokens = self._api_v1_authoritative_context_admission(
                llm_instance=llm_instance,
                messages=runtime_messages,
                requested_output_tokens=requested_output_tokens,
                requested_context_tier=requested_context_tier,
            )
            if not admitted:
                return self._api_v1_response_envelope(
                    request_id,
                    error=self._api_v1_enrich_safe_error(
                        admission_error or {
                            "code": "compute_node_context_admission_unavailable",
                            "message": "Desktop runtime context admission failed",
                        },
                        request_id=request_id,
                        requested_context_tier=requested_context_tier,
                        prompt_tokens=prompt_tokens,
                        requested_output_tokens=requested_output_tokens,
                    ),
                )

            create_chat_completion = recovery_completion
            if not callable(create_chat_completion) and llm_instance is not None:
                create_chat_completion = getattr(llm_instance, "create_chat_completion", None)

            if callable(create_chat_completion):
                log_info(
                    (
                        "API v1 runtime generation branch selected: "
                        "request_id={} model_id={} protocol={} route={} branch={}"
                    ),
                    request_id,
                    model_id,
                    "tokenplace_api_v1_relay_e2ee",
                    "/api/v1/relay/responses",
                    "direct_non_streaming_completion",
                )
                started = time.monotonic()
                completion = create_chat_completion(
                    messages=runtime_messages,
                    **completion_kwargs,
                )
                inference_duration = time.monotonic() - started
                log_info(
                    "api_v1.inference_complete active_tier={} prompt_tokens={} output_reservation={} admission_result=admitted inference_duration_seconds={} safe_error_code=none",
                    normalize_context_tier(getattr(self.model_manager, "context_tier", DEFAULT_CONTEXT_TIER)),
                    prompt_tokens if prompt_tokens is not None else "unknown",
                    completion_kwargs["max_tokens"],
                    round(inference_duration, 3),
                )
                assistant_message = self._assistant_message_from_runtime_completion(
                    completion
                )

            if assistant_message is None:
                invalid_reason = (
                    getattr(self, "_last_api_v1_invalid_model_output_reason", None)
                    or "unsupported_completion_shape"
                )
                log_error(
                    "Desktop runtime returned invalid API v1 assistant output reason={} shape={}",
                    invalid_reason,
                    self._api_v1_safe_completion_shape(completion),
                )
                return self._api_v1_response_envelope(
                    request_id,
                    error=self._api_v1_enrich_safe_error(
                        {
                            "code": "compute_node_invalid_model_output",
                            "message": "Desktop runtime returned invalid assistant output",
                        },
                        request_id=request_id,
                        requested_context_tier=requested_context_tier,
                        prompt_tokens=prompt_tokens,
                        requested_output_tokens=requested_output_tokens,
                        internal_reason=invalid_reason,
                    ),
                )

            return self._api_v1_response_envelope(request_id, message=assistant_message)
        except Exception as exc:
            if _is_llama_cpp_inference_request_error(exc):
                diagnostics = getattr(exc, "diagnostics", {})
                internal_reason = (
                    diagnostics.get("reason")
                    if isinstance(diagnostics, dict) and isinstance(diagnostics.get("reason"), str)
                    else "runtime_rejected_generation_options"
                )
                rejected_option = (
                    diagnostics.get("rejected_option")
                    if isinstance(diagnostics, dict) and isinstance(diagnostics.get("rejected_option"), str)
                    else None
                )
                error_code = (
                    diagnostics.get("code")
                    if isinstance(diagnostics, dict) and diagnostics.get("code") == "compute_node_options_unsupported"
                    else "compute_node_internal_error"
                )
                self._last_api_v1_runtime_health = {
                    "runtime_healthy": True,
                    "recovery_attempted": False,
                    "recovery_succeeded": False,
                }
                log_error(
                    "Desktop runtime rejected API v1 relay inference request",
                    exc_info=True,
                )
                return self._api_v1_response_envelope(
                    request_id,
                    error=self._api_v1_enrich_safe_error(
                        {
                            "code": error_code,
                            "message": "Desktop runtime rejected the inference request",
                        },
                        request_id=request_id,
                        requested_context_tier=requested_context_tier,
                        prompt_tokens=prompt_tokens,
                        requested_output_tokens=requested_output_tokens,
                        internal_reason=internal_reason,
                        rejected_option=rejected_option,
                    ),
                )
            recovery_attempted = (
                "replacement" in str(exc).lower()
                or "restart" in str(exc).lower()
                or (
                    exc.__cause__ is not None
                    and _is_llama_cpp_restartable_worker_error(exc.__cause__)
                )
            )
            runtime_healthy = not recovery_attempted
            self._last_api_v1_runtime_health = {
                "runtime_healthy": runtime_healthy,
                "recovery_attempted": recovery_attempted,
                "recovery_succeeded": False,
            }
            log_error(
                "Desktop runtime inference failed for API v1 relay request",
                exc_info=True,
            )
            return self._api_v1_response_envelope(
                request_id,
                error=self._api_v1_enrich_safe_error(
                    {
                        "code": "compute_node_internal_error",
                        "message": "Desktop runtime inference failed",
                    },
                    request_id=request_id,
                    requested_context_tier=requested_context_tier,
                    prompt_tokens=prompt_tokens,
                    requested_output_tokens=requested_output_tokens,
                    internal_reason="runtime_inference_failed",
                ),
            )

    def process_client_request_result(self, request_data: Dict[str, Any]) -> RelayProcessingResult:
        """Process a client request and return a typed, privacy-safe outcome."""
        try:
            try:
                _validate_with_fallback(request_data, MESSAGE_SCHEMA)
            except ValueError as e:
                log_error("Invalid request data format: {}", str(e))
                return RelayProcessingResult.submission_failed(safe_error_code="invalid_relay_payload")

            client_pub_key_b64 = _normalize_client_public_key_b64(request_data['client_public_key'])
            if client_pub_key_b64 is None:
                log_error("Invalid client_public_key format in relay request metadata")
                return RelayProcessingResult.submission_failed(safe_error_code="invalid_relay_payload")
            stream_requested = request_data.get('stream') is True
            stream_session_id = request_data.get('stream_session_id')
            try:
                client_pub_key = base64.b64decode(client_pub_key_b64, validate=True)
            except (AttributeError, binascii.Error, ValueError):
                log_error("Invalid client_public_key encoding in relay request metadata")
                return RelayProcessingResult.submission_failed(safe_error_code="invalid_relay_payload")

            log_info("Decrypting client request...")
            decrypted_chat_history = self.crypto_manager.decrypt_message(request_data)
            if decrypted_chat_history is None:
                log_info("Decryption failed. Skipping.")
                return RelayProcessingResult.submission_failed(safe_error_code="decrypt_failed")

            log_info("Decrypted client request")
            api_v1_request_payload = _extract_api_v1_request_payload(
                decrypted_chat_history,
                client_pub_key_b64,
            )
            if api_v1_request_payload is not None:
                try:
                    if getattr(self, "_api_v1_registered_relays", set()):
                        self._api_v1_start_heartbeat_worker()
                    response_envelope = self._generate_api_v1_response_with_runtime_model(
                        request_id=api_v1_request_payload["request_id"],
                        model_id=api_v1_request_payload["model"],
                        messages=api_v1_request_payload["messages"],
                        options=dict(api_v1_request_payload["options"]),
                        requested_context_tier=api_v1_request_payload["routing"]["context_tier"],
                    )
                    api_v1_response = response_envelope.get("api_v1_response", {})
                    error = api_v1_response.get("error") if isinstance(api_v1_response, dict) else None
                    safe_error_code = error.get("code") if isinstance(error, dict) else None
                    runtime_health = getattr(self, "_last_api_v1_runtime_health", {})
                    runtime_healthy = bool(runtime_health.get("runtime_healthy", True))
                    recovery_attempted = bool(runtime_health.get("recovery_attempted", False))
                    recovery_succeeded = bool(runtime_health.get("recovery_succeeded", False))
                    if safe_error_code not in {
                        "compute_node_internal_error",
                        "compute_node_process_failed",
                    }:
                        runtime_healthy = True
                    submitted = self._post_api_v1_response(
                        response_envelope,
                        client_pub_key_b64=client_pub_key_b64,
                        client_pub_key=client_pub_key,
                    )
                    return RelayProcessingResult(
                        inference_succeeded=safe_error_code is None and submitted,
                        submitted=submitted,
                        safe_error_code=safe_error_code,
                        runtime_healthy=runtime_healthy,
                        recovery_attempted=recovery_attempted,
                        recovery_succeeded=recovery_succeeded,
                    )
                finally:
                    self._api_v1_stop_heartbeat_worker()

            chat_history = _extract_chat_history_and_validate_key_binding(
                decrypted_chat_history,
                client_pub_key_b64,
            )
            if chat_history is None:
                return RelayProcessingResult.submission_failed(safe_error_code="invalid_relay_payload")

            if stream_requested and isinstance(stream_session_id, str) and stream_session_id.strip():
                log_info("Processing streaming relay request for session {}", stream_session_id)
                response_history = self.model_manager.llama_cpp_get_response(chat_history)
                encrypted_response = self.crypto_manager.encrypt_message(response_history, client_pub_key)
                chunk_payload = {
                    'session_id': stream_session_id,
                    'chunk': {
                        'client_public_key': client_pub_key_b64,
                        **encrypted_response,
                    },
                    'final': True,
                }

                request_kwargs = {
                    'json': chunk_payload,
                    'timeout': self._request_timeout,
                }
                headers = self._auth_headers()
                if headers:
                    request_kwargs['headers'] = headers

                timeout = request_kwargs.pop('timeout', self._request_timeout)
                stream_response = requests.post(
                    f'{self.relay_url}/stream/source',
                    timeout=timeout,
                    **request_kwargs,
                )
                if stream_response.status_code != 200:
                    log_error("Error status from /stream/source: {}", stream_response.status_code)
                    return RelayProcessingResult.submission_failed(safe_error_code="stream_submission_failed")
                return RelayProcessingResult(inference_succeeded=True, submitted=True)

            log_info("Getting response from LLM...")
            response_history = self.model_manager.llama_cpp_get_response(chat_history)
            log_info("LLM generated response")

            log_info("Encrypting response for client...")
            encrypted_response = self.crypto_manager.encrypt_message(
                response_history,
                client_pub_key
            )

            source_payload = {
                'client_public_key': client_pub_key_b64,
                **encrypted_response
            }

            try:
                _validate_with_fallback(source_payload, MESSAGE_SCHEMA)
            except ValueError as e:
                log_error("Invalid response payload format: {}", str(e))
                return RelayProcessingResult.submission_failed(safe_error_code="invalid_response_payload")

            log_info("Posting response to {}/source. Payload keys: {}", self.relay_url, list(source_payload.keys()))

            request_kwargs = {
                'json': source_payload,
                'timeout': self._request_timeout,
            }
            headers = self._auth_headers()
            if headers:
                request_kwargs['headers'] = headers

            timeout = request_kwargs.pop('timeout', self._request_timeout)
            source_response = requests.post(
                f'{self.relay_url}/source',
                timeout=timeout,
                **request_kwargs
            )

            log_info(
                "Response sent to /source. Status: {}, body length: {}",
                source_response.status_code,
                len(source_response.text)
            )

            if source_response.status_code != 200:
                log_error("Error status from /source: {}", source_response.status_code)
                return RelayProcessingResult.submission_failed(safe_error_code="response_submission_failed")

            response_content = source_response.text.strip()
            if not response_content:
                log_error("Empty response from /source")
                return RelayProcessingResult.submission_failed(safe_error_code="empty_relay_response")

            return RelayProcessingResult(inference_succeeded=True, submitted=True)

        except requests.ConnectionError as e:
            log_error("Connection error when posting to relay source endpoint: {}", str(e), exc_info=True)
            return RelayProcessingResult.submission_failed(safe_error_code="relay_connection_error")
        except requests.Timeout as e:
            log_error("Request timeout when posting to relay source endpoint: {}", str(e), exc_info=True)
            return RelayProcessingResult.submission_failed(safe_error_code="relay_timeout")
        except requests.RequestException as e:
            log_error("Request exception when posting to relay source endpoint: {}", str(e), exc_info=True)
            return RelayProcessingResult.submission_failed(safe_error_code="relay_request_exception")
        except Exception as e:
            log_error("Exception during request processing: {}", str(e), exc_info=True)
            return RelayProcessingResult.submission_failed(safe_error_code="compute_node_internal_error", runtime_healthy=False)

    def process_client_request(self, request_data: Dict[str, Any]) -> bool:
        """Compatibility wrapper; True means encrypted response/error submission succeeded."""

        return bool(self.process_client_request_result(request_data))

    def process_api_v1_chat_request(self, request_data: Dict[str, Any]) -> bool:
        """Relay API v1 plaintext dispatch is disabled pending an E2EE-compatible design."""

        log_error("Rejected disabled relay API v1 payload dispatch")
        return False

    def _normalise_poll_wait_seconds(self, wait_seconds: Any) -> float:
        """Return a safe non-negative polling delay for relay-provided wait values."""

        if isinstance(wait_seconds, bool):
            return float(self._request_timeout)
        try:
            normalised_wait = float(wait_seconds)
        except (TypeError, ValueError):
            return float(self._request_timeout)
        if not math.isfinite(normalised_wait) or normalised_wait < 0:
            return float(self._request_timeout)
        return normalised_wait

    def poll_api_v1_encrypted_work_continuously(self):  # pragma: no cover
        """Continuously poll API v1 E2EE relay routes and process encrypted work."""

        self.start()
        consecutive_failures = 0
        max_failures = _max_poll_failures_before_stop()
        log_info("Starting API v1 E2EE relay polling loop")
        while not self.stop_polling:
            try:
                relay_response = self.poll_api_v1_encrypted_work()
                if not isinstance(relay_response, dict):
                    consecutive_failures += 1
                    if max_failures is not None and consecutive_failures >= max_failures:
                        log_error(
                            "Stopping API v1 E2EE relay polling after {} consecutive invalid responses.",
                            consecutive_failures,
                        )
                        self.stop_polling = True
                        break
                    time.sleep(self._request_timeout)
                    continue

                wait_seconds = relay_response.get('next_ping_in_x_seconds', self._request_timeout)
                wait_seconds = self._normalise_poll_wait_seconds(wait_seconds)
                relay_error = relay_response.get('error')
                if relay_error:
                    consecutive_failures += 1
                    log_error("Error from API v1 E2EE relay poll: {}", relay_error)
                    if max_failures is not None and consecutive_failures >= max_failures:
                        log_error(
                            "Stopping API v1 E2EE relay polling after {} consecutive relay errors.",
                            consecutive_failures,
                        )
                        self.stop_polling = True
                        break
                    time.sleep(wait_seconds)
                    continue

                consecutive_failures = 0
                if relay_response.get('protocol') == 'tokenplace_api_v1_relay_e2ee':
                    self.process_client_request(relay_response)
                time.sleep(wait_seconds)
            except Exception as e:
                consecutive_failures += 1
                log_error("Exception during API v1 E2EE polling loop: {}", str(e), exc_info=True)
                if max_failures is not None and consecutive_failures >= max_failures:
                    log_error(
                        "Stopping API v1 E2EE relay polling after {} consecutive failures.",
                        consecutive_failures,
                    )
                    self.stop_polling = True
                    break
                time.sleep(self._request_timeout)

    def poll_relay_continuously(self):  # pragma: no cover
        """
        Continuously poll the relay for new chat messages and process them.
        This method runs in an infinite loop and should be called in a separate thread.

        Call start() before running this method to set stop_polling to False.
        Call stop() to terminate the polling loop cleanly.

        Example:
            ```python
            import threading

            # Create a thread for polling
            relay_client.start()  # Allow polling to run
            thread = threading.Thread(target=relay_client.poll_relay_continuously)
            thread.daemon = True  # Thread will exit when main program exits
            thread.start()

            # Main program continues...

            # Later when you want to stop polling:
            relay_client.stop()
            thread.join(timeout=10)  # Wait for thread to finish
            ```
        """
        if self.stop_polling:
            log_info("Starting relay polling")
            self.stop_polling = False
            self._polling_stopped_by_request = False

        consecutive_failures = 0
        max_failures = _max_poll_failures_before_stop()
        while not self.stop_polling:
            try:
                # Ping the relay and check for client requests
                relay_response = self.ping_relay()

                # Validate the relay response contains expected fields
                if not isinstance(relay_response, dict):
                    log_error("Invalid relay response type: {}", type(relay_response))
                    consecutive_failures += 1
                    if max_failures is not None and consecutive_failures >= max_failures:
                        log_error(
                            "Stopping relay polling after {} consecutive invalid responses.",
                            consecutive_failures,
                        )
                        self.stop_polling = True
                        break
                    time.sleep(self._request_timeout)
                    continue

                if 'next_ping_in_x_seconds' not in relay_response:
                    log_error("Missing 'next_ping_in_x_seconds' in relay response")
                    consecutive_failures += 1
                    if max_failures is not None and consecutive_failures >= max_failures:
                        log_error(
                            "Stopping relay polling after {} consecutive malformed responses.",
                            consecutive_failures,
                        )
                        self.stop_polling = True
                        break
                    time.sleep(self._request_timeout)
                    continue

                relay_error = relay_response.get('error')
                if relay_error:
                    log_error("Error from relay: {}", relay_error)
                    consecutive_failures += 1
                    if max_failures is not None and consecutive_failures >= max_failures:
                        log_error(
                            "Stopping relay polling after {} consecutive relay errors.",
                            consecutive_failures,
                        )
                        self.stop_polling = True
                        break
                else:
                    consecutive_failures = 0
                    # Avoid logging potentially sensitive ciphertext or keys.
                    # Only log the top-level keys present in the relay response.
                    log_info(
                        "Received data from relay with keys: {}",
                        list(relay_response.keys())
                    )

                    # Check if there's a client request to process
                    required_fields = ['client_public_key', 'chat_history', 'cipherkey', 'iv']
                    if all(field in relay_response for field in required_fields):
                        log_info("Processing client request...")
                        self.process_client_request(relay_response)
                    else:
                        log_info("No client request data in sink response.")

                # Sleep before the next ping
                sleep_duration = relay_response.get('next_ping_in_x_seconds', self._request_timeout)
                log_info("Sleeping for {} seconds...", sleep_duration)
                time.sleep(sleep_duration)

            except Exception as e:
                consecutive_failures += 1
                log_error("Exception during polling loop: {}", str(e), exc_info=True)
                if max_failures is not None and consecutive_failures >= max_failures:
                    log_error(
                        "Stopping relay polling after {} consecutive failures.",
                        consecutive_failures,
                    )
                    self.stop_polling = True
                    break
                time.sleep(self._request_timeout)  # Sleep for 10 seconds on error
