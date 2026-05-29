"""Compute provider abstraction for API v1 request handling.

This module allows API v1 routes to target either local llama execution or a
remote distributed compute-node endpoint while preserving a local fallback.
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional, Protocol
from urllib.parse import urlparse

import requests

from api.v1.models import generate_response
from utils.crypto.crypto_manager import CryptoManager

logger = logging.getLogger("api.v1.compute_provider")
_last_backend_path: contextvars.ContextVar[str] = contextvars.ContextVar(
    "api_v1_last_backend_path",
    default="unknown",
)


_DEFAULT_PRODUCTION_RELAY_URL = "https://token.place"
_EXPLICIT_DISTRIBUTED_TARGET_ENVS = (
    "TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL",
    "TOKENPLACE_DISTRIBUTED_RELAY_URL",
    "TOKENPLACE_DISTRIBUTED_COMPUTE_URL",
)
_RELAY_INTERNAL_URL_ENVS = (
    "TOKENPLACE_RELAY_INTERNAL_URL",
    "TOKEN_PLACE_RELAY_INTERNAL_URL",
    "RELAY_INTERNAL_URL",
)
_RELAY_PUBLIC_URL_ENVS = (
    "TOKENPLACE_RELAY_PUBLIC_URL",
    "TOKEN_PLACE_RELAY_PUBLIC_URL",
    "RELAY_PUBLIC_URL",
)


@dataclass(frozen=True)
class DistributedTargetSelection:
    """Resolved distributed relay target and diagnostics for provider logs."""

    url: str
    source: str
    relay_only: bool = False


class ComputeProviderError(Exception):
    """Raised when a compute provider cannot satisfy a request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "compute_provider_error",
        error_type: str = "server_error",
        public_message: str | None = None,
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_type = error_type
        self.public_message = public_message or "Unable to generate a response right now."
        self.status_code = status_code


_RELAY_ERROR_MAP: dict[str, dict[str, Any]] = {
    "no_registered_compute_nodes": {
        "error_type": "service_unavailable_error",
        "public_message": "No registered compute nodes are available on this relay.",
        "status_code": 503,
    },
    "compute_node_timeout": {
        "error_type": "timeout_error",
        "public_message": "The LLM server took too long to respond. Please try again.",
        "status_code": 504,
    },
    "compute_node_bridge_timeout": {
        "error_type": "timeout_error",
        "public_message": "The LLM server took too long to respond. Please try again.",
        "status_code": 504,
    },
    "compute_node_unreachable": {
        "error_type": "service_unavailable_error",
        "public_message": "The LLM server is unavailable right now. Please try again.",
        "status_code": 503,
    },
    "compute_node_bridge_error": {
        "error_type": "server_error",
        "public_message": "Unable to contact the LLM server right now. Please try again.",
        "status_code": 502,
    },
    "compute_node_invalid_payload": {
        "error_type": "server_error",
        "public_message": "The LLM server returned an invalid response. Please try again.",
        "status_code": 502,
    },
}


def _error_from_code(code: str, *, message: str) -> ComputeProviderError:
    mapped = _RELAY_ERROR_MAP.get(code, {})
    return ComputeProviderError(
        message,
        code=code,
        error_type=mapped.get("error_type", "server_error"),
        public_message=mapped.get("public_message", "Unable to generate a response right now."),
        status_code=int(mapped.get("status_code", 502)),
    )


class ApiV1ComputeProvider(Protocol):
    """Contract for API v1-compatible compute providers."""

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Return an assistant message payload compatible with OpenAI chat responses."""


@dataclass(frozen=True)
class LocalApiV1ComputeProvider:
    """Default provider that executes inference in-process via llama.cpp."""

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        updated_messages = generate_response(model_id, messages, **(options or {}))
        if not updated_messages:
            raise ComputeProviderError("model returned an empty message list")
        assistant_message = updated_messages[-1]
        if not isinstance(assistant_message, dict):
            raise ComputeProviderError("assistant response must be a message object")
        _last_backend_path.set("local_in_process")
        return assistant_message


@dataclass(frozen=True)
class DistributedApiV1ComputeProvider:
    """Provider that dispatches API v1 requests via relay-blind E2EE envelopes."""

    base_url: str
    timeout_seconds: float = 30.0

    def _relay_url(self, path: str) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/api/v1") and path.startswith("/api/v1/"):
            path = path[len("/api/v1"):]
        return f"{base_url}{path}"

    def _poll_interval_seconds(self) -> float:
        return min(max(self.timeout_seconds / 20.0, 0.1), 0.5)

    def _build_request_crypto_manager(self) -> CryptoManager:
        """Create an isolated crypto manager for each relay request."""
        return CryptoManager()

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        _last_backend_path.set("distributed_relay_e2ee_pending")
        crypto_manager = self._build_request_crypto_manager()
        relay_timeout = max(min(self.timeout_seconds, 30.0), 1.0)
        deadline = time.time() + relay_timeout
        relay_request_id = f"api-v1-{uuid.uuid4().hex}"

        def _remaining_timeout() -> float:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise _error_from_code(
                    "compute_node_timeout",
                    message="timed out waiting for distributed relay API v1 encrypted response",
                )
            return remaining

        try:
            next_server_response = requests.get(
                self._relay_url("/api/v1/relay/servers/next"),
                timeout=_remaining_timeout(),
            )
        except requests.RequestException as exc:
            raise _error_from_code(
                "compute_node_unreachable",
                message=f"unable to reach relay next_server endpoint: {exc}",
            ) from exc

        next_server_payload: Any | None = None
        if next_server_response.status_code == 503:
            try:
                next_server_payload = next_server_response.json()
            except ValueError:
                next_server_payload = None
            error_payload = (
                next_server_payload.get("error")
                if isinstance(next_server_payload, dict)
                else None
            )
            error_code = error_payload.get("code") if isinstance(error_payload, dict) else None
            if error_code == "no_registered_compute_nodes":
                raise _error_from_code(
                    "no_registered_compute_nodes",
                    message="relay reported no registered compute nodes",
                )

        if next_server_response.status_code >= 500:
            raise _error_from_code(
                "compute_node_unreachable",
                message=(
                    "relay next_server endpoint returned "
                    f"unexpected status {next_server_response.status_code}"
                ),
            )

        try:
            if next_server_payload is None:
                next_server_payload = next_server_response.json()
        except ValueError as exc:
            raise _error_from_code(
                "compute_node_invalid_payload",
                message="relay next_server response was not valid JSON",
            ) from exc

        if not isinstance(next_server_payload, dict):
            raise _error_from_code(
                "compute_node_invalid_payload",
                message="relay next_server response must be an object",
            )

        server_public_key = next_server_payload.get("server_public_key")
        if not isinstance(server_public_key, str) or not server_public_key.strip():
            raise _error_from_code(
                "no_registered_compute_nodes",
                message="relay reported no registered compute nodes",
            )

        plaintext_envelope = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": relay_request_id,
            "client_public_key": crypto_manager.public_key_b64,
            "api_v1_request": {
                "model": model_id,
                "messages": messages,
                "options": options or {},
            },
        }

        try:
            encrypted_envelope = crypto_manager.encrypt_message(plaintext_envelope, server_public_key)
        except Exception as exc:
            raise _error_from_code(
                "compute_node_invalid_payload",
                message=f"failed to encrypt relay request envelope: {exc}",
            ) from exc
        faucet_payload = {
            "client_public_key": crypto_manager.public_key_b64,
            "server_public_key": server_public_key,
            "request_id": relay_request_id,
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            **encrypted_envelope,
        }

        try:
            faucet_response = requests.post(
                self._relay_url("/api/v1/relay/requests"),
                json=faucet_payload,
                timeout=_remaining_timeout(),
            )
        except requests.RequestException as exc:
            raise _error_from_code(
                "compute_node_unreachable",
                message=f"unable to post encrypted request to relay faucet endpoint: {exc}",
            ) from exc

        if faucet_response.status_code == 404:
            raise _error_from_code(
                "no_registered_compute_nodes",
                message="relay reported selected compute node is no longer available",
            )

        if faucet_response.status_code != 200:
            raise _error_from_code(
                "compute_node_bridge_error",
                message=(
                    "relay faucet endpoint returned unexpected status "
                    f"{faucet_response.status_code}"
                ),
            )

        poll_interval = self._poll_interval_seconds()
        while time.time() < deadline:
            try:
                retrieve_timeout = min(poll_interval + 0.5, _remaining_timeout())
                retrieve_response = requests.post(
                    self._relay_url("/api/v1/relay/responses/retrieve"),
                    json={
                        "client_public_key": crypto_manager.public_key_b64,
                        "request_id": relay_request_id,
                    },
                    timeout=retrieve_timeout,
                )
            except requests.RequestException:
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if retrieve_response.status_code == 202:
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if retrieve_response.status_code == 404:
                raise _error_from_code(
                    "compute_node_bridge_error",
                    message="relay reported unknown API v1 response request id",
                )

            if retrieve_response.status_code != 200:
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            try:
                retrieve_payload = retrieve_response.json()
            except ValueError:
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if not isinstance(retrieve_payload, dict):
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if retrieve_payload.get("error"):
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if not {"chat_history", "cipherkey", "iv"}.issubset(retrieve_payload.keys()):
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            decrypted_response = crypto_manager.decrypt_message(retrieve_payload)
            if decrypted_response is None:
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if not isinstance(decrypted_response, dict):
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="decrypted relay response payload must be an object",
                )

            if decrypted_response.get("protocol") != "tokenplace_api_v1_relay_e2ee":
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if decrypted_response.get("request_id") != relay_request_id:
                time.sleep(min(poll_interval, max(deadline - time.time(), 0.0)))
                continue

            if decrypted_response.get("client_public_key") != crypto_manager.public_key_b64:
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="decrypted relay response client_public_key binding mismatch",
                )

            api_v1_response = decrypted_response.get("api_v1_response")
            if not isinstance(api_v1_response, dict):
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="decrypted relay response missing api_v1_response object",
                )

            if api_v1_response.get("error"):
                raise _error_from_code(
                    "compute_node_bridge_error",
                    message=f"compute node reported error: {api_v1_response.get('error')}",
                )

            assistant_message = api_v1_response.get("message")
            if not isinstance(assistant_message, dict):
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="compute node response missing assistant message",
                )

            _last_backend_path.set("distributed_relay_e2ee")
            return assistant_message

        raise _error_from_code(
            "compute_node_timeout",
            message="timed out waiting for distributed relay API v1 encrypted response",
        )


@dataclass(frozen=True)
class FallbackApiV1ComputeProvider:
    """Wrap distributed execution with local fallback for migration safety."""

    primary: ApiV1ComputeProvider
    fallback: ApiV1ComputeProvider

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        try:
            message = self.primary.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )
            return message
        except ComputeProviderError as exc:
            logger.warning("distributed compute fallback triggered: %s", exc)
            message = self.fallback.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )
            _last_backend_path.set("fallback_local_in_process")
            return message


def _normalise_target_url(value: str | None) -> str:
    """Normalize configured relay URLs for stable selection and cache keys."""

    return (value or "").strip().rstrip("/")


def _validated_target_url(value: str | None, *, source: str) -> str:
    """Return a normalized absolute HTTP(S) target or fail clearly."""

    target = _normalise_target_url(value)
    if not target:
        return ""

    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ComputeProviderError(
            f"Invalid distributed relay target from {source}: target must be an "
            "absolute HTTP(S) URL."
        )
    return target


def _current_token_place_env() -> str:
    """Return the active token.place deployment environment label."""

    return os.environ.get("TOKEN_PLACE_ENV", "development").strip().lower()


def _config_value(key: str) -> str:
    """Read a string config value without making provider selection depend on imports."""

    try:
        from config import get_config

        value = get_config().get(key, "")
    except Exception as exc:  # pragma: no cover - defensive against early bootstrap cycles
        logger.debug("api_v1.compute_provider.config_read_failed key=%s", key, exc_info=exc)
        return ""
    return value if isinstance(value, str) else ""


def select_api_v1_distributed_target() -> DistributedTargetSelection:
    """Resolve the distributed API v1 relay target with explicit staging-safe precedence.

    Precedence:
    1. Explicit API v1/distributed relay target environment variables.
    2. Internal relay URL environment variables for relay-only self-dispatch.
    3. Relay public URL environment variables when this process is the public relay.
    4. Explicit non-default config relay URLs.
    5. Production default only when TOKEN_PLACE_ENV=production.

    Non-production environments intentionally do not silently fall back to
    https://token.place because that makes staging appear to call production.
    """

    for env_name in _EXPLICIT_DISTRIBUTED_TARGET_ENVS:
        target = _validated_target_url(
            os.environ.get(env_name),
            source=f"env:{env_name}",
        )
        if target:
            return DistributedTargetSelection(
                url=target,
                source=f"env:{env_name}",
                relay_only=False,
            )

    for env_name in _RELAY_INTERNAL_URL_ENVS:
        target = _validated_target_url(
            os.environ.get(env_name),
            source=f"relay_internal_env:{env_name}",
        )
        if target:
            return DistributedTargetSelection(
                url=target,
                source=f"relay_internal_env:{env_name}",
                relay_only=True,
            )

    for env_name in _RELAY_PUBLIC_URL_ENVS:
        target = _validated_target_url(
            os.environ.get(env_name),
            source=f"relay_public_env:{env_name}",
        )
        if target:
            return DistributedTargetSelection(
                url=target,
                source=f"relay_public_env:{env_name}",
                relay_only=True,
            )

    env_name = _current_token_place_env()
    for config_key in ("api.relay_url", "relay.server_url"):
        target = _validated_target_url(
            _config_value(config_key),
            source=f"config:{config_key}",
        )
        if not target or target == _DEFAULT_PRODUCTION_RELAY_URL:
            continue
        return DistributedTargetSelection(
            url=target,
            source=f"config:{config_key}",
            relay_only=False,
        )

    if env_name == "production":
        return DistributedTargetSelection(
            url=_DEFAULT_PRODUCTION_RELAY_URL,
            source="production_default",
            relay_only=False,
        )

    return DistributedTargetSelection(url="", source="unset", relay_only=False)


@lru_cache(maxsize=16)
def _build_api_v1_compute_provider(
    mode: str,
    distributed_url: str,
    distributed_fallback_enabled: bool,
    target_source: str = "unknown",
    relay_only: bool = False,
) -> ApiV1ComputeProvider:
    """Create a compute provider for the normalized environment inputs."""

    local_provider = LocalApiV1ComputeProvider()

    if mode != "distributed":
        logger.info("api_v1.compute_provider.selected provider=local mode=%s", mode)
        return local_provider

    if not distributed_url:
        if not distributed_fallback_enabled:
            message = (
                "TOKENPLACE_API_V1_COMPUTE_PROVIDER=distributed requires "
                "TOKENPLACE_DISTRIBUTED_COMPUTE_URL or another explicit distributed "
                "relay target outside production. Set one of "
                f"{', '.join(_EXPLICIT_DISTRIBUTED_TARGET_ENVS)}, "
                "TOKENPLACE_RELAY_INTERNAL_URL, or TOKENPLACE_RELAY_PUBLIC_URL."
            )
            logger.error(
                "api_v1.compute_provider.target_unset mode=%s fallback_enabled=%s "
                "target_source=%s relay_only=%s",
                mode,
                distributed_fallback_enabled,
                target_source,
                relay_only,
            )
            raise ComputeProviderError(message)
        logger.warning(
            "api_v1.compute_provider.target_unset mode=%s fallback_enabled=%s "
            "target_source=%s relay_only=%s; using local fallback",
            mode,
            distributed_fallback_enabled,
            target_source,
            relay_only,
        )
        return local_provider

    selected_target = distributed_url.rstrip("/")
    distributed_provider = DistributedApiV1ComputeProvider(base_url=selected_target)
    if not distributed_fallback_enabled:
        logger.info(
            "api_v1.compute_provider.selected provider=distributed mode=%s "
            "fallback_enabled=false target=%s target_source=%s relay_only=%s",
            mode,
            selected_target,
            target_source,
            relay_only,
        )
        return distributed_provider

    logger.info(
        "api_v1.compute_provider.selected provider=distributed_with_local_fallback "
        "mode=%s fallback_enabled=true target=%s target_source=%s relay_only=%s",
        mode,
        selected_target,
        target_source,
        relay_only,
    )
    return FallbackApiV1ComputeProvider(primary=distributed_provider, fallback=local_provider)


def get_api_v1_compute_provider() -> ApiV1ComputeProvider:
    """Resolve the active provider based on environment configuration."""

    mode, target_selection, distributed_fallback_enabled = _read_api_v1_provider_env()
    return _build_api_v1_compute_provider(
        mode,
        target_selection.url,
        distributed_fallback_enabled,
        target_selection.source,
        target_selection.relay_only,
    )


def _read_api_v1_provider_env() -> tuple[str, DistributedTargetSelection, bool]:
    """Read and normalize API v1 provider environment configuration."""

    mode = os.environ.get("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "local").strip().lower()
    distributed_fallback_enabled = (
        os.environ.get("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    return mode, select_api_v1_distributed_target(), distributed_fallback_enabled


def get_api_v1_compute_provider_for_mode(
    *,
    mode: str,
    distributed_url: str | None = None,
    distributed_fallback_enabled: bool | None = None,
    distributed_target_source: str = "request_override",
    distributed_relay_only: bool = False,
) -> ApiV1ComputeProvider:
    """Resolve provider with an explicit mode override for request-scoped routing."""

    normalized_mode = (mode or "local").strip().lower()
    _, env_target_selection, fallback_enabled_from_env = _read_api_v1_provider_env()
    if distributed_fallback_enabled is None:
        distributed_fallback_enabled = fallback_enabled_from_env
    if distributed_url is not None:
        target_selection = DistributedTargetSelection(
            url=_normalise_target_url(distributed_url),
            source=distributed_target_source,
            relay_only=distributed_relay_only,
        )
    else:
        target_selection = env_target_selection
    return _build_api_v1_compute_provider(
        normalized_mode,
        target_selection.url,
        bool(distributed_fallback_enabled),
        target_selection.source,
        target_selection.relay_only,
    )


def get_api_v1_resolved_provider_path(provider: ApiV1ComputeProvider) -> str:
    """Return a stable diagnostics label for the resolved provider instance."""

    if isinstance(provider, FallbackApiV1ComputeProvider):
        return "distributed_with_local_fallback"
    if isinstance(provider, DistributedApiV1ComputeProvider):
        return "distributed"
    if isinstance(provider, LocalApiV1ComputeProvider):
        return "local"
    return "unknown"


def get_api_v1_last_backend_path() -> str:
    """Return per-request backend execution diagnostics label."""

    return _last_backend_path.get()
