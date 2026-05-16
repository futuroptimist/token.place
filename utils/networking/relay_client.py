"""Relay client module for managing communication with relay servers."""
from __future__ import annotations

import base64
import binascii
import importlib
import ipaddress
import json
import jsonschema
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse, urlunparse

import requests

# Configure logging
logger = logging.getLogger('relay_client')

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
    if not isinstance(model, str) or not model.strip():
        log_error("Rejected API v1 relay payload: model must be a non-empty string")
        return None
    if not isinstance(messages, list):
        log_error("Rejected API v1 relay payload: messages must be a list")
        return None
    if not isinstance(options, dict):
        log_error("Rejected API v1 relay payload: options must be an object")
        return None

    return {
        "request_id": request_id,
        "model": model,
        "messages": messages,
        "options": options,
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
    def __init__(
        self,
        base_url: str,
        port: Optional[int],
        crypto_manager,
        model_manager,
        *,
        include_configured_servers: bool = True,
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
        """
        self.base_url = base_url
        self.port = port
        self.crypto_manager = crypto_manager
        self.model_manager = model_manager
        self.stop_polling = True  # Flag to control polling loop - starts as True so loop won't run until explicitly started
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

        self._relay_urls = self._build_relay_targets(
            base_url,
            port,
            configured_servers,
            cluster_only=self._cluster_only,
        )
        self._active_relay_index = 0
        self._sink_start_index = 0
        self._last_api_v1_work_relay_url: Optional[str] = None

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

    def start(self):
        """Start the polling loop by setting stop_polling to False"""
        self.stop_polling = False

    def stop(self):
        """Stop the polling loop by setting stop_polling to True"""
        log_info("Stopping relay polling")
        self.stop_polling = True

    def unregister_from_relay(self) -> bool:
        """Best-effort unregister call for graceful compute-node shutdown."""

        last_error: Optional[str] = None
        had_success = False

        for offset in range(len(self._relay_urls)):
            index = (self._active_relay_index + offset) % len(self._relay_urls)
            candidate_url = self._relay_urls[index]
            try:
                request_kwargs = {
                    'json': {'server_public_key': self.crypto_manager.public_key_b64},
                }
                headers = self._auth_headers()
                if headers:
                    request_kwargs['headers'] = headers

                response = requests.post(
                    f'{candidate_url}/unregister',
                    timeout=self._request_timeout,
                    **request_kwargs,
                )
                if response.status_code == 200:
                    self._active_relay_index = index
                    log_info("Unregistered compute node from relay {}", candidate_url)
                    had_success = True
                    continue

                last_error = f"HTTP {response.status_code}"
                log_error(
                    "Failed to unregister compute node from {}: {}",
                    candidate_url,
                    last_error,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                log_error(
                    "Error unregistering compute node from {}: {}",
                    candidate_url,
                    last_error,
                    exc_info=True,
                )
            except Exception as exc:  # pragma: no cover - unexpected edge cases
                last_error = str(exc)
                log_error(
                    "Unexpected error unregistering compute node from {}: {}",
                    candidate_url,
                    last_error,
                    exc_info=True,
                )

        if had_success:
            return True

        if last_error:
            log_error("Unable to unregister compute node from relay: {}", last_error)
        return False

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
                    jsonschema.validate(instance=relay_response, schema=RELAY_RESPONSE_SCHEMA)
                except jsonschema.exceptions.ValidationError as exc:
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
                log_error("Connection error when pinging relay: {}", str(exc), exc_info=True)
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True
            except requests.Timeout as exc:
                log_error("Request timeout when pinging relay: {}", str(exc), exc_info=True)
                last_error = {'error': str(exc), 'next_ping_in_x_seconds': self._request_timeout}
                encountered_error = True
            except requests.RequestException as exc:
                log_error("Request exception when pinging relay: {}", str(exc), exc_info=True)
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

    def register_api_v1_compute_node(self, relay_url: Optional[str] = None) -> Dict[str, Any]:
        target_url = relay_url or self.relay_url
        payload = {'server_public_key': self.crypto_manager.public_key_b64}
        request_kwargs: Dict[str, Any] = {'json': payload, 'timeout': self._request_timeout}
        headers = self._auth_headers()
        if headers:
            request_kwargs['headers'] = headers
        response = requests.post(
            self._build_api_v1_url(target_url, "/relay/servers/register"),
            timeout=request_kwargs.pop('timeout'),
            **request_kwargs,
        )
        if response.status_code != 200:
            return {'error': f'HTTP {response.status_code}', 'next_ping_in_x_seconds': self._request_timeout}
        return response.json()

    @staticmethod
    def _build_api_v1_url(relay_url: str, route: str) -> str:
        """Build API v1 URLs without duplicating a pre-existing /api/v1 suffix."""
        base = relay_url.rstrip("/")
        normalized_route = route if route.startswith("/") else f"/{route}"
        if base.endswith("/api/v1"):
            return f"{base}{normalized_route}"
        return f"{base}/api/v1{normalized_route}"

    def poll_api_v1_encrypted_work(self) -> Dict[str, Any]:
        """Register then poll API v1 relay routes for encrypted work."""

        last_error: Optional[Dict[str, Any]] = None
        for offset in range(len(self._relay_urls)):
            index = (self._active_relay_index + offset) % len(self._relay_urls)
            candidate_url = self._relay_urls[index]

            try:
                register_response = self.register_api_v1_compute_node(candidate_url)
                register_wait = self._request_timeout
                if isinstance(register_response, dict):
                    register_wait = register_response.get('next_ping_in_x_seconds', self._request_timeout)
                    if register_response.get('error'):
                        last_error = {
                            'error': register_response.get('error'),
                            'next_ping_in_x_seconds': register_wait,
                        }
                        continue

                request_kwargs: Dict[str, Any] = {
                    'json': {'server_public_key': self.crypto_manager.public_key_b64},
                    'timeout': self._request_timeout,
                }
                headers = self._auth_headers()
                if headers:
                    request_kwargs['headers'] = headers

                response = requests.post(
                    self._build_api_v1_url(candidate_url, "/relay/servers/poll"),
                    timeout=request_kwargs.pop('timeout'),
                    **request_kwargs,
                )
                if response.status_code != 200:
                    last_error = {
                        'error': f'HTTP {response.status_code}',
                        'next_ping_in_x_seconds': register_wait,
                    }
                    continue
                payload = response.json()
                if not isinstance(payload, dict):
                    last_error = {
                        'error': 'Invalid response format: expected object payload',
                        'next_ping_in_x_seconds': register_wait,
                    }
                    continue
                payload.setdefault('next_ping_in_x_seconds', register_wait)
                self._active_relay_index = index
                self._last_api_v1_work_relay_url = candidate_url
                log_info(
                    "API v1 relay poll route=/api/v1/relay/servers/poll api_v1_payload={} request_id={}",
                    payload.get('protocol') == 'tokenplace_api_v1_relay_e2ee',
                    payload.get('request_id', 'none'),
                )
                return payload
            except Exception as exc:
                log_error("API v1 relay poll failed for {}: {}", candidate_url, str(exc), exc_info=True)
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
        error: Optional[Dict[str, str]] = None,
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

            response_url = self._build_api_v1_url(
                self._api_v1_response_relay_url(),
                "/relay/responses",
            )
            source_response = requests.post(
                response_url,
                timeout=self._request_timeout,
                **request_kwargs,
            )
            submitted = source_response.status_code == 200
            log_info(
                "API v1 E2EE response submission request_id={} route=/api/v1/relay/responses submitted={}",
                response_envelope["request_id"],
                submitted,
            )
            return submitted
        except Exception:
            log_error(
                "Failed to encrypt or post API v1 response to relay /api/v1/relay/responses",
                exc_info=True,
            )
            return False

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

    @staticmethod
    def _messages_are_valid_api_v1_chat(messages: Any) -> bool:
        if not isinstance(messages, list) or not messages:
            return False
        for message in messages:
            if not isinstance(message, dict):
                return False
            if not isinstance(message.get("role"), str):
                return False
            if "content" not in message:
                return False
        return True

    @staticmethod
    def _api_v1_stringify_content_blocks(content: Any) -> Any:
        """Collapse OpenAI-style content blocks into llama.cpp-compatible text."""

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

            if block_type == "image_url":
                image_url = block.get("image_url")
                url_value = image_url.get("url") if isinstance(image_url, dict) else image_url
                if isinstance(url_value, str) and url_value.strip():
                    if url_value.lstrip().lower().startswith("data:"):
                        segments.append("[Inline image attached]")
                    else:
                        segments.append(f"[Image: {url_value.strip()}]")
                continue

            if block_type in {"input_image", "image"}:
                segments.append("[Inline image attached]")

        if not segments:
            return ""
        return "\n\n".join(segments)

    @classmethod
    def _normalise_api_v1_chat_messages(
        cls, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Return API v1 messages with structured content blocks collapsed to text."""

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

        adapter_instructions = {
            "llama-3-8b-instruct:alignment": (
                "You are the alignment-focused variant of Meta Llama 3.1 8B. "
                "Follow the provided safety charter to remain helpful, honest, "
                "harmless, and to call out uncertain answers."
            ),
        }
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

    def _runtime_model_can_satisfy(self, model_id: str) -> bool:
        """Return whether the attached desktop runtime can serve the requested model.

        The packaged desktop bridge owns a single already-initialized runtime model.
        Accept the API v1 landing-page Llama identifiers when the runtime is a
        Llama-family manager or mock; reject unrelated model IDs instead of
        silently substituting the desktop model as a successful response.
        """

        normalized_model = model_id.strip().lower()
        if not normalized_model:
            return False
        base_model = normalized_model.split(":", 1)[0]
        if bool(getattr(self.model_manager, "use_mock_llm", False)):
            return True

        configured_ids = {
            str(value).strip().lower()
            for value in (
                getattr(self.model_manager, "api_model_id", None),
                getattr(self.model_manager, "model_id", None),
                getattr(self.model_manager, "file_name", None),
                os.path.basename(str(getattr(self.model_manager, "model_path", ""))),
            )
            if value
        }
        if normalized_model in configured_ids or base_model in configured_ids:
            return True

        if not configured_ids and callable(
            getattr(self.model_manager, "llama_cpp_get_response", None)
        ):
            return base_model.startswith("llama-")

        # The desktop runtime defaults to Meta Llama 3/3.1 GGUFs, while API v1
        # exposes the stable catalogue ID used by the landing-page chat.
        llama_runtime = any("llama" in configured_id for configured_id in configured_ids)
        llama_api_ids = {
            "llama-3-8b-instruct",
            "meta/llama-3.1-8b-instruct",
            "meta-llama-3.1-8b-instruct-q4_k_m.gguf",
        }
        return llama_runtime and (
            normalized_model in llama_api_ids or base_model in llama_api_ids
        )

    @staticmethod
    def _api_v1_supported_options(options: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        passthrough_options = {
            "frequency_penalty",
            "max_tokens",
            "presence_penalty",
            "response_format",
            "seed",
            "stop",
            "stream",
            "temperature",
            "tool_choice",
            "tools",
            "top_p",
        }
        unsupported = sorted(key for key in options if key not in passthrough_options)
        if unsupported:
            return False, ", ".join(unsupported)
        if options.get("stream") is True:
            return False, "stream"
        return True, None

    def _generate_api_v1_response_with_server_model_if_available(
        self,
        *,
        request_id: str,
        model_id: str,
        messages: List[Dict[str, Any]],
        options: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Best-effort non-desktop API v1 model path for repo-local tests.

        Packaged desktop success never depends on this optional server module.
        """

        try:
            models_module = importlib.import_module("api.v1.models")
        except ModuleNotFoundError:
            return None

        model_error_cls = getattr(models_module, "ModelError", Exception)
        response_generator = getattr(models_module, "generate_" + "response", None)
        if not callable(response_generator):
            return None

        try:
            response_history = response_generator(model_id, messages, **options)
        except model_error_cls as exc:
            error_type = getattr(exc, "error_type", "")
            runtime_response = None
            if error_type in {"model_not_found", "model_load_error"}:
                runtime_response_fn = getattr(
                    self.model_manager,
                    "llama_cpp_get_response",
                    None,
                )
                if callable(runtime_response_fn):
                    try:
                        runtime_response = runtime_response_fn(list(messages))
                    except Exception:
                        log_error(
                            "Runtime-model execution failed for API v1 relay request",
                            exc_info=True,
                        )
                    else:
                        if isinstance(runtime_response, list) and runtime_response:
                            assistant_message = self._valid_api_v1_assistant_message(
                                runtime_response[-1]
                            )
                            if assistant_message is not None:
                                return self._api_v1_response_envelope(
                                    request_id,
                                    message=assistant_message,
                                )
                        log_error(
                            "Runtime-model execution returned invalid API v1 output"
                        )

            error_code = (
                "compute_node_model_unsupported"
                if error_type == "model_not_found"
                else "compute_node_model_error"
            )
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": error_code,
                    "message": str(exc),
                },
            )
        except Exception:
            log_error(
                "Optional repo API v1 model execution failed for relay request",
                exc_info=True,
            )
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_internal_error",
                    "message": "Unexpected internal error during inference",
                },
            )

        if not isinstance(response_history, list) or not response_history:
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_internal_error",
                    "message": "LLM returned invalid response history",
                },
            )
        assistant_message = response_history[-1]
        if not isinstance(assistant_message, dict):
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_internal_error",
                    "message": "LLM returned invalid assistant message",
                },
            )
        return self._api_v1_response_envelope(request_id, message=assistant_message)

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
            "stream": True,
        }
        completion_kwargs.update(safe_options)
        return completion_kwargs

    def _assistant_message_from_runtime_completion(
        self, completion: Any
    ) -> Optional[Dict[str, Any]]:
        """Extract an API v1 assistant message from direct llama.cpp output."""

        if (
            isinstance(completion, dict)
            and isinstance(completion.get("choices"), list)
            and completion["choices"]
            and isinstance(completion["choices"][0], dict)
        ):
            return self._valid_api_v1_assistant_message(
                completion["choices"][0].get("message")
            )

        consume_stream = getattr(self.model_manager, "_consume_streaming_completion", None)
        if callable(consume_stream):
            try:
                return self._valid_api_v1_assistant_message(consume_stream(completion))
            except Exception:
                log_error("Failed to consume streaming API v1 runtime completion", exc_info=True)
                return None

        return None

    def _generate_api_v1_response_with_runtime_model(
        self,
        *,
        request_id: str,
        model_id: str,
        messages: List[Dict[str, Any]],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate an API v1 assistant message with the desktop runtime model."""

        if not self._messages_are_valid_api_v1_chat(messages):
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_invalid_request",
                    "message": "Invalid chat message format",
                },
            )

        llama_cpp_get_response = getattr(self.model_manager, "llama_cpp_get_response", None)
        has_runtime_chat = callable(llama_cpp_get_response)
        get_llm_instance = getattr(self.model_manager, "get_llm_instance", None)
        manager_defines_llm_instance = callable(
            getattr(type(self.model_manager), "get_llm_instance", None)
        )
        has_direct_runtime_completion = (
            callable(get_llm_instance) and manager_defines_llm_instance
        )
        if not has_runtime_chat and not has_direct_runtime_completion:
            server_response = self._generate_api_v1_response_with_server_model_if_available(
                request_id=request_id,
                model_id=model_id,
                messages=messages,
                options=options,
            )
            if server_response is not None:
                return server_response

        if not self._runtime_model_can_satisfy(model_id):
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_model_unsupported",
                    "message": "Requested model is not available in the desktop runtime",
                },
            )

        options_supported, unsupported_option = self._api_v1_supported_options(options)
        if not options_supported:
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_options_unsupported",
                    "message": (
                        "Requested option is unsupported by the desktop runtime: "
                        f"{unsupported_option}"
                    ),
                },
            )

        safe_options = {key: value for key, value in options.items() if key != "stream"}
        runtime_messages = self._prepare_api_v1_runtime_messages(model_id, messages)
        try:
            assistant_message: Optional[Dict[str, Any]] = None
            if safe_options and has_direct_runtime_completion:
                llm_instance = get_llm_instance()
                create_chat_completion = getattr(llm_instance, "create_chat_completion", None)
                if not callable(create_chat_completion):
                    return self._api_v1_response_envelope(
                        request_id,
                        error={
                            "code": "compute_node_options_unsupported",
                            "message": (
                                "Requested options require direct runtime completion support"
                            ),
                        },
                    )

                completion_kwargs = self._api_v1_runtime_completion_kwargs(safe_options)
                completion = create_chat_completion(
                    messages=runtime_messages,
                    **completion_kwargs,
                )
                assistant_message = self._assistant_message_from_runtime_completion(
                    completion
                )
                if assistant_message is None and completion_kwargs.get("stream") is True:
                    fallback_kwargs = dict(completion_kwargs)
                    fallback_kwargs["stream"] = False
                    completion = create_chat_completion(
                        messages=runtime_messages,
                        **fallback_kwargs,
                    )
                    assistant_message = self._assistant_message_from_runtime_completion(
                        completion
                    )
            else:
                if not has_runtime_chat:
                    return self._api_v1_response_envelope(
                        request_id,
                        error={
                            "code": "compute_node_model_unsupported",
                            "message": "Desktop runtime does not expose chat inference",
                        },
                    )
                response_history = llama_cpp_get_response(list(runtime_messages))
                if isinstance(response_history, list) and response_history:
                    assistant_message = self._valid_api_v1_assistant_message(
                        response_history[-1]
                    )

            if assistant_message is None:
                log_error("Desktop runtime returned invalid API v1 assistant output")
                return self._api_v1_response_envelope(
                    request_id,
                    error={
                        "code": "compute_node_invalid_model_output",
                        "message": "Desktop runtime returned invalid assistant output",
                    },
                )

            return self._api_v1_response_envelope(request_id, message=assistant_message)
        except Exception:
            log_error(
                "Desktop runtime inference failed for API v1 relay request",
                exc_info=True,
            )
            return self._api_v1_response_envelope(
                request_id,
                error={
                    "code": "compute_node_internal_error",
                    "message": "Desktop runtime inference failed",
                },
            )

    def process_client_request(self, request_data: Dict[str, Any]) -> bool:
        """
        Process a client request from the relay.

        Args:
            request_data: Data received from the relay containing the encrypted client request

        Returns:
            bool: True if processing succeeded, False otherwise
        """
        try:
            try:
                jsonschema.validate(instance=request_data, schema=MESSAGE_SCHEMA)
            except jsonschema.exceptions.ValidationError as e:
                log_error("Invalid request data format: {}", str(e))
                return False

            client_pub_key_b64 = _normalize_client_public_key_b64(request_data['client_public_key'])
            if client_pub_key_b64 is None:
                log_error("Invalid client_public_key format in relay request metadata")
                return False
            stream_requested = request_data.get('stream') is True
            stream_session_id = request_data.get('stream_session_id')
            try:
                client_pub_key = base64.b64decode(client_pub_key_b64, validate=True)
            except (AttributeError, binascii.Error, ValueError):
                log_error("Invalid client_public_key encoding in relay request metadata")
                return False

            log_info("Decrypting client request...")
            decrypted_chat_history = self.crypto_manager.decrypt_message(request_data)
            if decrypted_chat_history is None:
                log_info("Decryption failed. Skipping.")
                return False

            log_info("Decrypted client request")
            api_v1_request_payload = _extract_api_v1_request_payload(
                decrypted_chat_history,
                client_pub_key_b64,
            )
            if api_v1_request_payload is not None:
                response_envelope = self._generate_api_v1_response_with_runtime_model(
                    request_id=api_v1_request_payload["request_id"],
                    model_id=api_v1_request_payload["model"],
                    messages=api_v1_request_payload["messages"],
                    options=dict(api_v1_request_payload["options"]),
                )
                return self._post_api_v1_response(
                    response_envelope,
                    client_pub_key_b64=client_pub_key_b64,
                    client_pub_key=client_pub_key,
                )

            chat_history = _extract_chat_history_and_validate_key_binding(
                decrypted_chat_history,
                client_pub_key_b64,
            )
            if chat_history is None:
                return False

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
                    return False
                return True

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
                jsonschema.validate(instance=source_payload, schema=MESSAGE_SCHEMA)
            except jsonschema.exceptions.ValidationError as e:
                log_error("Invalid response payload format: {}", str(e))
                return False

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
                return False

            response_content = source_response.text.strip()
            if not response_content:
                log_error("Empty response from /source")
                return False

            return True

        except requests.ConnectionError as e:
            log_error("Connection error when posting to relay source endpoint: {}", str(e), exc_info=True)
            return False
        except requests.Timeout as e:
            log_error("Request timeout when posting to relay source endpoint: {}", str(e), exc_info=True)
            return False
        except requests.RequestException as e:
            log_error("Request exception when posting to relay source endpoint: {}", str(e), exc_info=True)
            return False
        except Exception as e:
            log_error("Exception during request processing: {}", str(e), exc_info=True)
            return False

    def process_api_v1_chat_request(self, request_data: Dict[str, Any]) -> bool:
        """Relay API v1 plaintext dispatch is disabled pending an E2EE-compatible design."""

        log_error("Rejected disabled relay API v1 payload dispatch")
        return False

    def _normalise_poll_wait_seconds(self, wait_seconds: Any) -> float:
        """Return a safe non-negative polling delay for relay-provided wait values."""

        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            return float(self._request_timeout)

        normalised_wait = float(wait_seconds)
        if not math.isfinite(normalised_wait) or normalised_wait < 0:
            return float(self._request_timeout)
        return normalised_wait

    def poll_api_v1_encrypted_work_continuously(self):  # pragma: no cover
        """Continuously poll API v1 E2EE relay routes and process encrypted work."""

        self.stop_polling = False
        log_info("Starting API v1 E2EE relay polling loop")
        while not self.stop_polling:
            try:
                relay_response = self.poll_api_v1_encrypted_work()
                wait_seconds = self._request_timeout
                if isinstance(relay_response, dict):
                    wait_seconds = relay_response.get('next_ping_in_x_seconds', self._request_timeout)
                    wait_seconds = self._normalise_poll_wait_seconds(wait_seconds)
                    if relay_response.get('protocol') == 'tokenplace_api_v1_relay_e2ee':
                        self.process_client_request(relay_response)
                else:
                    wait_seconds = self._normalise_poll_wait_seconds(wait_seconds)
                time.sleep(wait_seconds)
            except Exception as e:
                log_error("Exception during API v1 E2EE polling loop: {}", str(e), exc_info=True)
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

        while not self.stop_polling:
            try:
                # Ping the relay and check for client requests
                relay_response = self.ping_relay()

                # Validate the relay response contains expected fields
                if not isinstance(relay_response, dict):
                    log_error("Invalid relay response type: {}", type(relay_response))
                    time.sleep(self._request_timeout)
                    continue

                if 'next_ping_in_x_seconds' not in relay_response:
                    log_error("Missing 'next_ping_in_x_seconds' in relay response")
                    time.sleep(self._request_timeout)
                    continue

                if 'error' in relay_response:
                    log_error("Error from relay: {}", relay_response['error'])
                else:
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
                log_error("Exception during polling loop: {}", str(e), exc_info=True)
                time.sleep(self._request_timeout)  # Sleep for 10 seconds on error
