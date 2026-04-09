"""Internal compute-provider abstraction for API v1 request handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests


class ComputeProviderError(RuntimeError):
    """Raised when a compute provider cannot produce a valid response."""


class ChatComputeProvider:
    """Protocol-like base class for API v1 chat generation providers."""

    requires_local_model_validation: bool = True

    def generate(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


@dataclass
class LocalChatComputeProvider(ChatComputeProvider):
    """Adapter around the legacy in-process generation flow."""

    local_handler: Callable[..., List[Dict[str, Any]]]
    requires_local_model_validation: bool = True

    def generate(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return self.local_handler(model_id, messages, **(options or {}))


@dataclass
class DistributedApiV1ChatComputeProvider(ChatComputeProvider):
    """Provider that forwards API v1 chat generation to a remote compute node."""

    base_url: str
    timeout_seconds: float = 30.0
    requires_local_model_validation: bool = False

    def _build_target_url(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/api/v1/chat/completions"):
            return base
        return f"{base}/api/v1/chat/completions"

    def generate(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "model": model_id,
            "messages": messages,
        }
        payload.update(options or {})

        response = requests.post(
            self._build_target_url(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ComputeProviderError("Remote compute response missing choices")

        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ComputeProviderError("Remote compute response missing assistant message")

        return list(messages) + [message]


@dataclass
class FallbackChatComputeProvider(ChatComputeProvider):
    """Use distributed provider first and fall back to local generation on error."""

    primary: ChatComputeProvider
    fallback: ChatComputeProvider
    warning_logger: Optional[Callable[[str], None]] = None
    requires_local_model_validation: bool = False

    def generate(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        try:
            return self.primary.generate(model_id, messages, options=options)
        except Exception as exc:
            if self.warning_logger:
                self.warning_logger(
                    "Distributed API v1 compute provider failed; falling back to local runtime: "
                    f"{exc}"
                )
            return self.fallback.generate(model_id, messages, options=options)


def build_chat_compute_provider(
    *,
    local_handler: Callable[..., List[Dict[str, Any]]],
    warning_logger: Optional[Callable[[str], None]] = None,
) -> ChatComputeProvider:
    """Build a chat compute provider using env-driven distributed settings."""

    local_provider = LocalChatComputeProvider(local_handler=local_handler)

    distributed_url = os.getenv("TOKENPLACE_API_V1_DISTRIBUTED_URL", "").strip()
    if not distributed_url:
        return local_provider

    timeout_raw = os.getenv("TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT", "30").strip()
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError:
        timeout_seconds = 30.0

    distributed_provider = DistributedApiV1ChatComputeProvider(
        base_url=distributed_url,
        timeout_seconds=max(timeout_seconds, 1.0),
    )

    if os.getenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return distributed_provider

    return FallbackChatComputeProvider(
        primary=distributed_provider,
        fallback=local_provider,
        warning_logger=warning_logger,
    )
