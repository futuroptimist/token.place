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
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests

from api.v1.models import generate_response
from utils.crypto.crypto_manager import CryptoManager

logger = logging.getLogger("api.v1.compute_provider")
_last_backend_path: contextvars.ContextVar[str] = contextvars.ContextVar(
    "api_v1_last_backend_path",
    default="unknown",
)


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
        "public_message": "No LLM servers are available right now.",
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
        return f"{self.base_url.rstrip('/')}{path}"

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
        crypto_manager = self._build_request_crypto_manager()
        relay_timeout = max(min(self.timeout_seconds, 30.0), 1.0)
        relay_request_id = f"api-v1-{uuid.uuid4().hex}"

        try:
            next_server_response = requests.get(
                self._relay_url("/next_server"),
                timeout=relay_timeout,
            )
        except requests.RequestException as exc:
            raise _error_from_code(
                "compute_node_unreachable",
                message=f"unable to reach relay next_server endpoint: {exc}",
            ) from exc

        if next_server_response.status_code >= 500:
            raise _error_from_code(
                "compute_node_unreachable",
                message=(
                    "relay next_server endpoint returned "
                    f"unexpected status {next_server_response.status_code}"
                ),
            )

        try:
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

        encrypted_envelope = crypto_manager.encrypt_message(plaintext_envelope, server_public_key)
        faucet_payload = {
            "client_public_key": crypto_manager.public_key_b64,
            "server_public_key": server_public_key,
            **encrypted_envelope,
        }

        try:
            faucet_response = requests.post(
                self._relay_url("/faucet"),
                json=faucet_payload,
                timeout=relay_timeout,
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

        deadline = time.time() + relay_timeout
        poll_interval = self._poll_interval_seconds()
        while time.time() < deadline:
            try:
                retrieve_response = requests.post(
                    self._relay_url("/retrieve"),
                    json={"client_public_key": crypto_manager.public_key_b64},
                    timeout=poll_interval + 0.5,
                )
            except requests.RequestException:
                time.sleep(poll_interval)
                continue

            if retrieve_response.status_code != 200:
                time.sleep(poll_interval)
                continue

            try:
                retrieve_payload = retrieve_response.json()
            except ValueError:
                time.sleep(poll_interval)
                continue

            if not isinstance(retrieve_payload, dict):
                time.sleep(poll_interval)
                continue

            if retrieve_payload.get("error"):
                time.sleep(poll_interval)
                continue

            if not {"chat_history", "cipherkey", "iv"}.issubset(retrieve_payload.keys()):
                time.sleep(poll_interval)
                continue

            decrypted_response = crypto_manager.decrypt_message(retrieve_payload)
            if not isinstance(decrypted_response, dict):
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="decrypted relay response payload must be an object",
                )

            if decrypted_response.get("protocol") != "tokenplace_api_v1_relay_e2ee":
                time.sleep(poll_interval)
                continue

            if decrypted_response.get("request_id") != relay_request_id:
                time.sleep(poll_interval)
                continue

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


@lru_cache(maxsize=8)
def _build_api_v1_compute_provider(
    mode: str, distributed_url: str, distributed_fallback_enabled: bool
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
                "TOKENPLACE_DISTRIBUTED_COMPUTE_URL when "
                "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK is disabled"
            )
            logger.error(
                "%s (fallback_enabled=%s)",
                message,
                distributed_fallback_enabled,
            )
            raise ComputeProviderError(message)
        logger.warning(
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER=distributed set without "
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL; using local fallback "
            "(fallback_enabled=%s)",
            distributed_fallback_enabled,
        )
        return local_provider

    distributed_provider = DistributedApiV1ComputeProvider(base_url=distributed_url)
    if not distributed_fallback_enabled:
        logger.info(
            "api_v1.compute_provider.selected provider=distributed mode=%s "
            "fallback_enabled=false target=%s",
            mode,
            distributed_url.rstrip("/"),
        )
        return distributed_provider

    logger.info(
        "api_v1.compute_provider.selected provider=distributed_with_local_fallback "
        "mode=%s fallback_enabled=true target=%s",
        mode,
        distributed_url.rstrip("/"),
    )
    return FallbackApiV1ComputeProvider(primary=distributed_provider, fallback=local_provider)


def get_api_v1_compute_provider() -> ApiV1ComputeProvider:
    """Resolve the active provider based on environment configuration."""

    mode = os.environ.get("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "local").strip().lower()
    distributed_url = os.environ.get("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "").strip()
    distributed_fallback_enabled = (
        os.environ.get("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    return _build_api_v1_compute_provider(mode, distributed_url, distributed_fallback_enabled)


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
