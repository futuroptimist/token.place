"""Compute provider abstraction for API v1 request handling.

This module allows API v1 routes to target either local llama execution or a
remote distributed compute-node endpoint while preserving a local fallback.
"""

from __future__ import annotations

import contextvars
import logging
import os
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests

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
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_type = error_type
        self.status_code = status_code


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
                f"{self.base_url.rstrip('/')}/relay/api/v1/chat/completions",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise ComputeProviderError(
                "Timed out while contacting the LLM compute bridge.",
                code="compute_bridge_timeout",
                error_type="timeout_error",
                status_code=504,
            ) from exc
        except requests.RequestException as exc:
            raise ComputeProviderError(
                "Unable to reach the LLM compute bridge.",
                code="compute_node_unreachable",
                error_type="server_error",
                status_code=503,
            ) from exc

        if response.status_code != 200:
            payload = None
            try:
                payload = response.json()
            except ValueError:
                payload = None

            relay_message = None
            relay_code = None
            if isinstance(payload, dict):
                error_payload = payload.get("error")
                if isinstance(error_payload, dict):
                    relay_message = error_payload.get("message")
                    relay_code = error_payload.get("code")

            if response.status_code == 503 and (
                relay_message == "No registered compute nodes available" or relay_code == 503
            ):
                raise ComputeProviderError(
                    "No LLM servers are available right now.",
                    code="no_compute_nodes_available",
                    error_type="service_unavailable_error",
                    status_code=503,
                )

            if response.status_code == 504:
                raise ComputeProviderError(
                    "Timed out waiting for an LLM server response.",
                    code="compute_node_timeout",
                    error_type="timeout_error",
                    status_code=504,
                )

            if response.status_code == 502:
                raise ComputeProviderError(
                    "The LLM server returned an invalid response.",
                    code="compute_node_invalid_payload",
                    error_type="server_error",
                    status_code=502,
                )

            raise ComputeProviderError(
                f"distributed provider returned status {response.status_code}",
                code="compute_node_bridge_error",
                error_type="server_error",
                status_code=502,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ComputeProviderError(
                "The LLM compute bridge returned a non-JSON response.",
                code="compute_node_invalid_payload",
                error_type="server_error",
                status_code=502,
            ) from exc

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ComputeProviderError(
                "The LLM server returned an invalid response payload.",
                code="compute_node_invalid_payload",
                error_type="server_error",
                status_code=502,
            )

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ComputeProviderError(
                "The LLM server returned an invalid response payload.",
                code="compute_node_invalid_payload",
                error_type="server_error",
                status_code=502,
            )

        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ComputeProviderError(
                "The LLM server returned an invalid response payload.",
                code="compute_node_invalid_payload",
                error_type="server_error",
                status_code=502,
            )

        _last_backend_path.set("registered_desktop_compute_node")
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
