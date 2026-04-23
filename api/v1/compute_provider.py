"""Compute provider abstraction for API v1 request handling.

This module allows API v1 routes to target either local llama execution or a
remote distributed compute-node endpoint while preserving a local fallback.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests

from api.v1.models import generate_response

logger = logging.getLogger("api.v1.compute_provider")


class ComputeProviderError(Exception):
    """Raised when a compute provider cannot satisfy a request."""


class ApiV1ComputeProvider(Protocol):
    """Contract for API v1-compatible compute providers."""

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> "ApiV1ComputeResult":
        """Return assistant payload + backend diagnostics for API v1 chat responses."""


@dataclass(frozen=True)
class ApiV1ComputeResult:
    """Structured result for API v1 compute execution."""

    assistant_message: dict[str, Any]
    backend_path: str
    backend_provider: str


@dataclass(frozen=True)
class LocalApiV1ComputeProvider:
    """Default provider that executes inference in-process via llama.cpp."""

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> ApiV1ComputeResult:
        updated_messages = generate_response(model_id, messages, **(options or {}))
        if not updated_messages:
            raise ComputeProviderError("model returned an empty message list")
        assistant_message = updated_messages[-1]
        if not isinstance(assistant_message, dict):
            raise ComputeProviderError("assistant response must be a message object")
        return ApiV1ComputeResult(
            assistant_message=assistant_message,
            backend_path="local",
            backend_provider=self.__class__.__name__,
        )


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
    ) -> ApiV1ComputeResult:
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

        return ApiV1ComputeResult(
            assistant_message=message,
            backend_path="distributed",
            backend_provider=self.__class__.__name__,
        )


@dataclass(frozen=True)
class RelayRegisteredApiV1ComputeProvider:
    """Provider that routes API v1 chat to a registered relay compute node."""

    relay_base_url: str
    timeout_seconds: float = 60.0

    def complete_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> ApiV1ComputeResult:
        payload: Dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "options": {k: v for k, v in (options or {}).items() if k != "stream"},
        }
        try:
            response = requests.post(
                f"{self.relay_base_url.rstrip('/')}/relay/api-v1/chat/dispatch",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise ComputeProviderError(f"relay registered-node request failed: {exc}") from exc

        if response.status_code != 200:
            raise ComputeProviderError(
                f"relay registered-node provider returned status {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ComputeProviderError("relay registered-node provider returned non-JSON response") from exc

        message = body.get("message")
        if not isinstance(message, dict):
            raise ComputeProviderError("relay registered-node response missing message")
        return ApiV1ComputeResult(
            assistant_message=message,
            backend_path="relay_registered_node",
            backend_provider=self.__class__.__name__,
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
    ) -> ApiV1ComputeResult:
        try:
            return self.primary.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )
        except ComputeProviderError as exc:
            logger.warning("distributed compute fallback triggered: %s", exc)
            fallback_result = self.fallback.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )
            return ApiV1ComputeResult(
                assistant_message=fallback_result.assistant_message,
                backend_path=f"fallback:{fallback_result.backend_path}",
                backend_provider=fallback_result.backend_provider,
            )


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
                "TOKENPLACE_DISTRIBUTED_COMPUTE_URL to point at relay base URL"
            )
        logger.info(
            "api_v1.compute_provider.selected provider=relay_registered mode=%s target=%s",
            mode,
            distributed_url.rstrip("/"),
        )
        return RelayRegisteredApiV1ComputeProvider(relay_base_url=distributed_url)

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
    if isinstance(provider, RelayRegisteredApiV1ComputeProvider):
        return "relay_registered"
    if isinstance(provider, LocalApiV1ComputeProvider):
        return "local"
    return "unknown"
