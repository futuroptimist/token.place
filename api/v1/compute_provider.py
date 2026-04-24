"""Compute provider abstraction for API v1 request handling.

This module allows API v1 routes to target either local llama execution or a
remote distributed compute-node endpoint while preserving a local fallback.
"""

from __future__ import annotations

import contextvars
import base64
import json
import logging
import os
import time
import uuid
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests

from api.v1.encryption import encryption_manager
from api.v1.models import generate_response

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
    """Provider that dispatches API v1 requests through the relay E2EE envelope."""

    base_url: str
    timeout_seconds: float = 30.0

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        request_id = f"api-v1-{uuid.uuid4().hex}"
        request_payload = {
            "protocol": "api_v1_e2ee",
            "version": 1,
            "request_id": request_id,
            "api_v1_request": {
                "request_id": request_id,
                "model": model_id,
                "messages": messages,
                "options": options or {},
            },
        }

        relay_url = self.base_url.rstrip("/")
        timeout = self.timeout_seconds
        try:
            next_server_response = requests.get(
                f"{relay_url}/next_server",
                timeout=timeout,
            )
            next_server_response.raise_for_status()
            next_server_data = next_server_response.json()
        except requests.Timeout as exc:
            raise _error_from_code("compute_node_timeout", message=str(exc)) from exc
        except requests.RequestException as exc:
            raise _error_from_code("compute_node_unreachable", message=str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise _error_from_code("compute_node_invalid_payload", message=str(exc)) from exc

        server_public_key = next_server_data.get("server_public_key")
        if not isinstance(server_public_key, str) or not server_public_key:
            raise _error_from_code(
                "no_registered_compute_nodes",
                message="relay returned no compute node public key",
            )

        encrypted_request = encryption_manager.encrypt_message(request_payload, server_public_key)
        if not encrypted_request:
            raise _error_from_code(
                "compute_node_bridge_error",
                message="failed to encrypt distributed API v1 relay envelope",
            )

        faucet_payload = {
            "client_public_key": encryption_manager.public_key_b64,
            "server_public_key": server_public_key,
            "chat_history": encrypted_request["ciphertext"],
            "cipherkey": encrypted_request["cipherkey"],
            "iv": encrypted_request["iv"],
        }

        try:
            faucet_response = requests.post(
                f"{relay_url}/faucet",
                json=faucet_payload,
                timeout=timeout,
            )
            faucet_response.raise_for_status()
        except requests.Timeout as exc:
            raise _error_from_code("compute_node_timeout", message=str(exc)) from exc
        except requests.RequestException as exc:
            raise _error_from_code("compute_node_unreachable", message=str(exc)) from exc

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                retrieve_response = requests.post(
                    f"{relay_url}/retrieve",
                    json={"client_public_key": encryption_manager.public_key_b64},
                    timeout=min(2.0, timeout),
                )
                retrieve_response.raise_for_status()
                retrieve_data = retrieve_response.json()
            except requests.Timeout:
                continue
            except requests.RequestException as exc:
                raise _error_from_code("compute_node_unreachable", message=str(exc)) from exc
            except json.JSONDecodeError as exc:
                raise _error_from_code("compute_node_invalid_payload", message=str(exc)) from exc

            if retrieve_data.get("error"):
                time.sleep(0.2)
                continue

            try:
                decrypted = encryption_manager.decrypt_message(
                    {
                        "ciphertext": base64.b64decode(retrieve_data["chat_history"]),
                        "iv": base64.b64decode(retrieve_data["iv"]),
                    },
                    base64.b64decode(retrieve_data["cipherkey"]),
                )
                if decrypted is None:
                    raise ValueError("empty decrypted payload")
                response_payload = json.loads(decrypted.decode("utf-8"))
            except Exception as exc:
                raise _error_from_code("compute_node_invalid_payload", message=str(exc)) from exc

            if isinstance(response_payload, dict):
                api_v1_error = response_payload.get("api_v1_error")
                if isinstance(api_v1_error, dict):
                    error_code = api_v1_error.get("code", "compute_node_bridge_error")
                    raise _error_from_code(
                        str(error_code),
                        message=str(api_v1_error.get("message", "distributed API v1 relay error")),
                    )
                api_v1_response = response_payload.get("api_v1_response")
                if isinstance(api_v1_response, dict) and isinstance(api_v1_response.get("message"), dict):
                    _last_backend_path.set("distributed_relay_e2ee")
                    return api_v1_response["message"]

            raise _error_from_code(
                "compute_node_invalid_payload",
                message="distributed API v1 relay response had unexpected shape",
            )

        raise _error_from_code(
            "compute_node_timeout",
            message="timed out while waiting for distributed API v1 relay response",
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
