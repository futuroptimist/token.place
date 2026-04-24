"""Compute provider abstraction for API v1 request handling.

This module allows API v1 routes to target either local llama execution or a
remote distributed compute-node endpoint while preserving a local fallback.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import base64
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests

from encrypt import decrypt, encrypt, generate_keys

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
    """Provider that relays API v1 requests through the legacy E2EE faucet/retrieve path."""

    base_url: str
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.25

    def _normalised_base_url(self) -> str:
        return self.base_url.rstrip("/")

    def _request_timeout(self) -> float:
        return max(1.0, self.timeout_seconds)

    def _select_server_public_key(self) -> str:
        response = requests.get(
            f"{self._normalised_base_url()}/next_server",
            timeout=self._request_timeout(),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise _error_from_code(
                "compute_node_invalid_payload",
                message="relay /next_server returned a non-object payload",
            )

        server_public_key = payload.get("server_public_key")
        if not isinstance(server_public_key, str) or not server_public_key.strip():
            raise _error_from_code(
                "no_registered_compute_nodes",
                message="relay has no registered compute nodes",
            )
        return server_public_key

    def _encrypt_envelope_for_compute_node(
        self,
        *,
        server_public_key_b64: str,
        client_public_key_b64: str,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Dict[str, Any],
    ) -> dict[str, str]:
        payload_bytes = json.dumps(
            {
                "chat_history": messages,
                "client_public_key": client_public_key_b64,
                "api_v1_envelope": {
                    "version": "v1",
                    "model": model_id,
                    "options": options,
                },
            }
        ).encode("utf-8")
        ciphertext_dict, cipherkey, iv = encrypt(payload_bytes, base64.b64decode(server_public_key_b64))
        return {
            "chat_history": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
            "cipherkey": base64.b64encode(cipherkey).decode("utf-8"),
            "iv": base64.b64encode(iv).decode("utf-8"),
        }

    def _retrieve_encrypted_response(self, *, client_public_key_b64: str) -> Dict[str, Any]:
        deadline = time.time() + self.timeout_seconds

        while time.time() < deadline:
            retrieve_response = requests.post(
                f"{self._normalised_base_url()}/retrieve",
                json={"client_public_key": client_public_key_b64},
                timeout=self._request_timeout(),
            )
            retrieve_response.raise_for_status()
            retrieve_payload = retrieve_response.json()
            if not isinstance(retrieve_payload, dict):
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="relay /retrieve returned a non-object payload",
                )

            if all(field in retrieve_payload for field in ("chat_history", "cipherkey", "iv")):
                return retrieve_payload

            time.sleep(self.poll_interval_seconds)

        raise _error_from_code(
            "compute_node_timeout",
            message="timed out waiting for distributed relay response",
        )

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if options:
            unsupported = ", ".join(sorted(options.keys()))
            raise ComputeProviderError(
                f"distributed API v1 relay currently does not support options: {unsupported}",
                code="distributed_api_v1_options_unsupported",
                error_type="invalid_request_error",
                public_message=(
                    "Distributed API v1 currently supports only model + messages. "
                    "Please remove advanced options or use local mode."
                ),
                status_code=400,
            )

        try:
            client_private_key, client_public_key = generate_keys()
            client_public_key_b64 = base64.b64encode(client_public_key).decode("utf-8")
            server_public_key_b64 = self._select_server_public_key()
            encrypted_request = self._encrypt_envelope_for_compute_node(
                server_public_key_b64=server_public_key_b64,
                client_public_key_b64=client_public_key_b64,
                model_id=model_id,
                messages=messages,
                options=options or {},
            )

            faucet_response = requests.post(
                f"{self._normalised_base_url()}/faucet",
                json={
                    "client_public_key": client_public_key_b64,
                    "server_public_key": server_public_key_b64,
                    **encrypted_request,
                },
                timeout=self._request_timeout(),
            )
            faucet_response.raise_for_status()

            encrypted_response = self._retrieve_encrypted_response(
                client_public_key_b64=client_public_key_b64,
            )
            decrypted_bytes = decrypt(
                {
                    "ciphertext": base64.b64decode(encrypted_response["chat_history"]),
                    "iv": base64.b64decode(encrypted_response["iv"]),
                },
                base64.b64decode(encrypted_response["cipherkey"]),
                client_private_key,
            )
            response_history = json.loads(decrypted_bytes.decode("utf-8"))
            if not isinstance(response_history, list) or not response_history:
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="distributed relay response did not include a message history",
                )
            assistant_message = response_history[-1]
            if not isinstance(assistant_message, dict):
                raise _error_from_code(
                    "compute_node_invalid_payload",
                    message="assistant message payload must be an object",
                )
            _last_backend_path.set("distributed_relay_e2ee")
            return assistant_message
        except ComputeProviderError:
            raise
        except requests.Timeout as exc:
            raise _error_from_code("compute_node_timeout", message=str(exc)) from exc
        except requests.ConnectionError as exc:
            raise _error_from_code("compute_node_unreachable", message=str(exc)) from exc
        except requests.RequestException as exc:
            raise _error_from_code("compute_node_bridge_error", message=str(exc)) from exc
        except Exception as exc:
            raise _error_from_code("compute_node_invalid_payload", message=str(exc)) from exc


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
