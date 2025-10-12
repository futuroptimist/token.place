"""Relay client module for managing communication with relay servers."""
from __future__ import annotations

import base64
import ipaddress
import json
import jsonschema
import logging
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
    """Log a message if not in production; fallback to always logging on error"""
    try:
        config = get_config_lazy()
        if config.is_production:
            return
    except Exception:
        pass
    log_func = getattr(logger, level)
    formatted = message.format(*args) if args else message
    kwargs = {"exc_info": exc_info} if exc_info is not None else {}
    log_func(formatted, **kwargs)


def log_info(message, *args) -> None:
    """Log info only in non-production environments using consistent formatting"""
    _log("info", message, *args)


def log_error(message, *args, exc_info: bool = False) -> None:
    """Log errors only in non-production environments using consistent formatting"""
    _log("error", message, *args, exc_info=exc_info)

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
    def __init__(self, base_url: str, port: int, crypto_manager, model_manager):
        """
        Initialize the RelayClient.

        Args:
            base_url: The base URL of the relay server (e.g., 'http://localhost')
            port: The port number of the relay server
            crypto_manager: Instance of CryptoManager for encryption/decryption
            model_manager: Instance of ModelManager for LLM interaction
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
            cluster_env = _coerce_optional_bool(os.environ.get('TOKEN_PLACE_RELAY_CLUSTER_ONLY'))
            self._cluster_only = cluster_env if cluster_env is not None else False

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

        if parsed.port is None and port is not None:
            hostname = parsed.hostname or netloc
            userinfo = ''
            if parsed.username:
                userinfo = parsed.username
                if parsed.password:
                    userinfo = f"{userinfo}:{parsed.password}"
                userinfo = f"{userinfo}@"
            netloc = f"{userinfo}{hostname}:{int(port)}"

        return urlunparse((scheme, netloc, path, '', '', '')).rstrip('/')

    @classmethod
    def _build_relay_targets(
        cls,
        primary_base: str,
        primary_port: int,
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

    def process_client_request(self, request_data: Dict[str, Any]) -> bool:
        """
        Process a client request from the relay.

        Args:
            request_data: Data received from the relay containing the encrypted client request

        Returns:
            bool: True if processing succeeded, False otherwise

        Example:
            ```python
            # Process data from relay
            request_data = {
                'client_public_key': 'base64_encoded_client_key',
                'chat_history': 'encrypted_data',
                'cipherkey': 'encrypted_key',
                'iv': 'initialization_vector'
            }
            success = relay_client.process_client_request(request_data)
            ```
        """
        try:
            # Validate request data against schema
            try:
                jsonschema.validate(instance=request_data, schema=MESSAGE_SCHEMA)
            except jsonschema.exceptions.ValidationError as e:
                log_error("Invalid request data format: {}", str(e))
                return False

            client_pub_key_b64 = request_data['client_public_key']

            # Decrypt the request
            log_info("Decrypting client request...")
            decrypted_chat_history = self.crypto_manager.decrypt_message(request_data)

            if decrypted_chat_history is None:
                log_info("Decryption failed. Skipping.")
                return False

            log_info("Decrypted client request")

            # Process with LLM
            log_info("Getting response from LLM...")
            response_history = self.model_manager.llama_cpp_get_response(decrypted_chat_history)
            log_info("LLM generated response")

            # Encrypt the response for the client
            log_info("Encrypting response for client...")
            client_pub_key = base64.b64decode(client_pub_key_b64)

            encrypted_response = self.crypto_manager.encrypt_message(
                response_history,
                client_pub_key
            )

            # Create the payload for the source endpoint
            source_payload = {
                'client_public_key': client_pub_key_b64,
                **encrypted_response  # Include chat_history, cipherkey, and iv
            }

            # Validate the outgoing payload
            try:
                jsonschema.validate(instance=source_payload, schema=MESSAGE_SCHEMA)
            except jsonschema.exceptions.ValidationError as e:
                log_error("Invalid response payload format: {}", str(e))
                return False

            log_info("Posting response to {}/source. Payload keys: {}", self.relay_url, list(source_payload.keys()))

            # Send the response to the relay
            try:
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

                # Validate response beyond just status code
                if source_response.status_code != 200:
                    log_error("Error status from /source: {}", source_response.status_code)
                    return False

                # Check if response has valid content
                response_content = source_response.text.strip()
                if not response_content:
                    log_error("Empty response from /source")
                    return False

                return True

            except requests.ConnectionError as e:
                log_error("Connection error when posting to /source: {}", str(e), exc_info=True)
                return False
            except requests.Timeout as e:
                log_error("Request timeout when posting to /source: {}", str(e), exc_info=True)
                return False
            except requests.RequestException as e:
                log_error("Request exception when posting to /source: {}", str(e), exc_info=True)
                return False

        except Exception as e:
            log_error("Exception during request processing: {}", str(e), exc_info=True)
            return False

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
