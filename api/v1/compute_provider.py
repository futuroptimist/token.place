"""Compute provider abstraction for API v1 request handling.

This module allows API v1 routes to target either local llama execution or a
remote distributed compute-node endpoint while preserving a local fallback.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from contextvars import ContextVar
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests

from api.v1.models import generate_response
from encrypt import decrypt, encrypt, generate_keys

logger = logging.getLogger("api.v1.compute_provider")
_last_execution_path: ContextVar[str] = ContextVar(
    "api_v1_compute_provider_execution_path",
    default="unknown",
)


class ComputeProviderError(Exception):
    """Raised when a compute provider cannot satisfy a request."""


def _set_execution_path(path: str) -> None:
    _last_execution_path.set(path)


def get_api_v1_last_execution_path() -> str:
    """Return the execution-path marker for the current request context."""

    return _last_execution_path.get()


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
        _set_execution_path("local_in_process")
        return assistant_message


@dataclass(frozen=True)
class DistributedApiV1ComputeProvider:
    """Provider that forwards API v1 chat payloads to a remote compute endpoint."""

    base_url: str
    timeout_seconds: float = 30.0

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
        }
        for key, value in (options or {}).items():
            if key == "stream":
                continue
            payload[key] = value

        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/api/v1/chat/completions",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise ComputeProviderError(f"distributed provider request failed: {exc}") from exc

        if response.status_code != 200:
            raise ComputeProviderError(
                f"distributed provider returned status {response.status_code}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ComputeProviderError("distributed provider returned non-JSON response") from exc

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ComputeProviderError("distributed provider returned no choices")

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ComputeProviderError("distributed provider choice payload is invalid")

        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ComputeProviderError("distributed provider response missing message")

        _set_execution_path("distributed_api_v1")
        return message


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
            return self.primary.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )
        except ComputeProviderError as exc:
            logger.warning("distributed compute fallback triggered: %s", exc)
            message = self.fallback.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )
            _set_execution_path("fallback_local_in_process")
            return message


@dataclass(frozen=True)
class RelayRegisteredApiV1ComputeProvider:
    """Provider that routes API v1 calls through relay registered compute nodes."""

    base_url: str
    timeout_seconds: float = 30.0
    retrieve_max_attempts: int = 60
    retrieve_retry_seconds: float = 0.25

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        del model_id, options  # relay /sink path consumes only the message history payload
        relay_url = self.base_url.rstrip("/")
        private_key, public_key = generate_keys()
        client_public_key_b64 = base64.b64encode(public_key).decode("utf-8")

        try:
            next_server_response = requests.get(
                f"{relay_url}/next_server",
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise ComputeProviderError(f"relay next_server request failed: {exc}") from exc
        if next_server_response.status_code != 200:
            raise ComputeProviderError(
                f"relay next_server returned status {next_server_response.status_code}"
            )
        try:
            next_server_payload = next_server_response.json()
        except ValueError as exc:
            raise ComputeProviderError("relay next_server returned non-JSON response") from exc

        server_public_key_b64 = next_server_payload.get("server_public_key")
        if not isinstance(server_public_key_b64, str) or not server_public_key_b64.strip():
            raise ComputeProviderError("relay next_server did not return a server_public_key")
        try:
            server_public_key = base64.b64decode(server_public_key_b64)
        except Exception as exc:
            raise ComputeProviderError("relay returned invalid server_public_key encoding") from exc

        wrapped_payload = {
            "chat_history": messages,
            "client_public_key": client_public_key_b64,
        }
        encrypted_payload, encrypted_key, iv = encrypt(
            json.dumps(wrapped_payload).encode("utf-8"),
            server_public_key,
        )
        faucet_payload = {
            "client_public_key": client_public_key_b64,
            "server_public_key": server_public_key_b64,
            "chat_history": base64.b64encode(encrypted_payload["ciphertext"]).decode("utf-8"),
            "cipherkey": base64.b64encode(encrypted_key).decode("utf-8"),
            "iv": base64.b64encode(iv).decode("utf-8"),
        }
        try:
            faucet_response = requests.post(
                f"{relay_url}/faucet",
                json=faucet_payload,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise ComputeProviderError(f"relay faucet request failed: {exc}") from exc
        if faucet_response.status_code != 200:
            raise ComputeProviderError(f"relay faucet returned status {faucet_response.status_code}")

        retrieve_payload = {"client_public_key": client_public_key_b64}
        for _ in range(max(1, self.retrieve_max_attempts)):
            try:
                retrieve_response = requests.post(
                    f"{relay_url}/retrieve",
                    json=retrieve_payload,
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                raise ComputeProviderError(f"relay retrieve request failed: {exc}") from exc

            if retrieve_response.status_code != 200:
                raise ComputeProviderError(
                    f"relay retrieve returned status {retrieve_response.status_code}"
                )
            try:
                retrieve_body = retrieve_response.json()
            except ValueError as exc:
                raise ComputeProviderError("relay retrieve returned non-JSON response") from exc

            if "error" in retrieve_body:
                error_text = str(retrieve_body.get("error"))
                if "No response available" in error_text:
                    time.sleep(max(0.0, self.retrieve_retry_seconds))
                    continue
                raise ComputeProviderError(f"relay retrieve error: {error_text}")

            if not all(key in retrieve_body for key in ("chat_history", "cipherkey", "iv")):
                raise ComputeProviderError("relay retrieve response missing encrypted fields")

            try:
                decrypted_response = decrypt(
                    {
                        "ciphertext": base64.b64decode(retrieve_body["chat_history"]),
                        "iv": base64.b64decode(retrieve_body["iv"]),
                    },
                    base64.b64decode(retrieve_body["cipherkey"]),
                    private_key,
                )
            except Exception as exc:
                raise ComputeProviderError(f"failed to decrypt relay response: {exc}") from exc

            if decrypted_response is None:
                raise ComputeProviderError("failed to decrypt relay response")

            try:
                decoded_response = json.loads(decrypted_response.decode("utf-8"))
            except Exception as exc:
                raise ComputeProviderError(f"relay response was not valid JSON: {exc}") from exc
            if not isinstance(decoded_response, list) or not decoded_response:
                raise ComputeProviderError("relay response chat history is empty or invalid")

            for candidate in reversed(decoded_response):
                if isinstance(candidate, dict) and candidate.get("role") == "assistant":
                    _set_execution_path("relay_registered_compute_node")
                    return candidate

            raise ComputeProviderError("relay response missing assistant message")

        raise ComputeProviderError("timed out waiting for relay retrieve response")


@lru_cache(maxsize=8)
def _build_api_v1_compute_provider(
    mode: str, distributed_url: str, distributed_fallback_enabled: bool
) -> ApiV1ComputeProvider:
    """Create a compute provider for the normalized environment inputs."""

    local_provider = LocalApiV1ComputeProvider()

    if mode == "relay_registered":
        if not distributed_url:
            raise ComputeProviderError(
                "TOKENPLACE_API_V1_COMPUTE_PROVIDER=relay_registered requires "
                "TOKENPLACE_DISTRIBUTED_COMPUTE_URL"
            )
        logger.info(
            "api_v1.compute_provider.selected provider=relay_registered mode=%s target=%s",
            mode,
            distributed_url.rstrip("/"),
        )
        return RelayRegisteredApiV1ComputeProvider(base_url=distributed_url)

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

    if isinstance(provider, RelayRegisteredApiV1ComputeProvider):
        return "relay_registered"
    if isinstance(provider, FallbackApiV1ComputeProvider):
        return "distributed_with_local_fallback"
    if isinstance(provider, DistributedApiV1ComputeProvider):
        return "distributed"
    if isinstance(provider, LocalApiV1ComputeProvider):
        return "local"
    return "unknown"
