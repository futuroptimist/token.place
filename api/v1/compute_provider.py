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
    ) -> dict[str, Any]:
        """Return an assistant message payload compatible with OpenAI chat responses."""

    def diagnostics(self) -> Dict[str, Any]:
        """Return resolved provider diagnostics for request-path observability."""


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
        return assistant_message

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "provider_class": self.__class__.__name__,
            "resolved_path": "local",
            "distributed_target": None,
            "fallback_enabled": False,
        }


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

        return message

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "provider_class": self.__class__.__name__,
            "resolved_path": "distributed",
            "distributed_target": self.base_url.rstrip("/"),
            "fallback_enabled": False,
        }


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
            return self.fallback.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )

    def diagnostics(self) -> Dict[str, Any]:
        primary_diag = (
            self.primary.diagnostics()
            if hasattr(self.primary, "diagnostics")
            else {"provider_class": self.primary.__class__.__name__, "distributed_target": None}
        )
        return {
            "provider_class": self.__class__.__name__,
            "resolved_path": "distributed_with_local_fallback",
            "distributed_target": primary_diag.get("distributed_target"),
            "fallback_enabled": True,
        }


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
