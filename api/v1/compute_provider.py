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

logger = logging.getLogger("api.v1.compute_provider")


def generate_response(model_id: str, messages: list[dict[str, Any]], **options: Any):
    """Compatibility wrapper for local generation (kept for tests monkeypatching this symbol)."""

    from api.v1.models import generate_response as _generate_response

    return _generate_response(model_id, messages, **options)


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
        # Import lazily so distributed-only relay runtime does not eagerly import
        # api.v1.models (which can in turn import llama_cpp at module import time).
        updated_messages = generate_response(model_id, messages, **(options or {}))
        if not updated_messages:
            raise ComputeProviderError("model returned an empty message list")
        assistant_message = updated_messages[-1]
        if not isinstance(assistant_message, dict):
            raise ComputeProviderError("assistant response must be a message object")
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
            return self.fallback.complete_chat(
                model_id=model_id,
                messages=messages,
                options=options,
            )


@lru_cache(maxsize=8)
def _build_api_v1_compute_provider(mode: str, distributed_url: str) -> ApiV1ComputeProvider:
    """Create a compute provider for the normalized environment inputs."""

    local_provider = LocalApiV1ComputeProvider()

    if mode != "distributed":
        return local_provider

    if not distributed_url:
        logger.warning(
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER=distributed set without "
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL; using local fallback"
        )
        return local_provider

    distributed_provider = DistributedApiV1ComputeProvider(base_url=distributed_url)
    allow_fallback = os.environ.get("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1").strip() != "0"
    if not allow_fallback:
        return distributed_provider
    return FallbackApiV1ComputeProvider(primary=distributed_provider, fallback=local_provider)


def get_api_v1_compute_provider() -> ApiV1ComputeProvider:
    """Resolve the active provider based on environment configuration."""

    mode = os.environ.get("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "local").strip().lower()
    distributed_url = os.environ.get("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "").strip()
    provider = _build_api_v1_compute_provider(mode, distributed_url)
    logger.info(
        "api.v1.compute_provider.selected mode=%s distributed_url_configured=%s fallback_enabled=%s provider=%s",
        mode,
        bool(distributed_url),
        os.environ.get("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1").strip() != "0",
        provider.__class__.__name__,
    )
    return provider
