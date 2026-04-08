"""Reusable runtime for token.place compute-node behavior."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import urlparse

from utils.crypto.crypto_manager import get_crypto_manager
from utils.llm.model_manager import get_model_manager
from utils.networking.relay_client import RelayClient

logger = logging.getLogger("server_app")


@dataclass(frozen=True)
class ComputeNodeRuntimeConfig:
    """Runtime configuration shared by compute-node callers."""

    relay_url: str
    relay_port: Optional[int]
    server_host: str
    server_port: int


def _first_env(keys: List[str]) -> Optional[str]:
    """Return the first non-empty environment variable in ``keys``."""

    for key in keys:
        value = os.environ.get(key)
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def resolve_relay_url(cli_default: str) -> str:
    """Resolve the relay base URL from CLI or env."""

    env_override = _first_env(
        [
            "TOKENPLACE_RELAY_URL",
            "TOKEN_PLACE_RELAY_URL",
            "TOKENPLACE_RELAY_BASE_URL",
            "TOKEN_PLACE_RELAY_BASE_URL",
            "TOKENPLACE_RELAY_UPSTREAM_URL",
            "TOKEN_PLACE_RELAY_UPSTREAM_URL",
            "RELAY_URL",
        ]
    )
    return env_override or cli_default


def resolve_relay_port(
    cli_default: Optional[int],
    relay_url: str,
    *,
    log_error: Callable[[str], None],
) -> Optional[int]:
    """Resolve the relay port from CLI, env, or the relay URL."""

    env_port = _first_env(["TOKENPLACE_RELAY_PORT", "TOKEN_PLACE_RELAY_PORT", "RELAY_PORT"])

    if env_port is not None:
        try:
            return int(env_port)
        except ValueError:
            log_error(f"Invalid relay port override: {env_port}")
            return cli_default

    parsed = urlparse(relay_url if "://" in relay_url else f"http://{relay_url}")
    if parsed.port:
        return parsed.port

    if cli_default is not None:
        return cli_default

    return None


def format_relay_target(relay_url: str, relay_port: Optional[int]) -> str:
    """Create a display-ready relay target without duplicating explicit URL ports."""

    parsed = urlparse(relay_url if "://" in relay_url else f"http://{relay_url}")
    if relay_port is None or parsed.port is not None:
        return relay_url
    return f"{relay_url}:{relay_port}"


class ComputeNodeRuntime:
    """Shared compute-node runtime used by server.py and future desktop bridge code."""

    def __init__(
        self,
        config: ComputeNodeRuntimeConfig,
        *,
        relay_client: Optional[RelayClient] = None,
        model_manager=None,
        crypto_manager=None,
        log_info: Callable[[str], None],
        log_error: Callable[[str], None],
    ):
        self.config = config
        self.log_info = log_info
        self.log_error = log_error

        self.model_manager = model_manager or get_model_manager()
        self.crypto_manager = crypto_manager or get_crypto_manager()
        self.relay_client = relay_client or RelayClient(
            base_url=config.relay_url,
            port=config.relay_port,
            crypto_manager=self.crypto_manager,
            model_manager=self.model_manager,
        )

    def initialize_model(self) -> bool:
        """Bootstrap model/download readiness for compute-node operation."""

        self.log_info("Initializing LLM...")
        if self.model_manager.use_mock_llm:
            self.log_info("Using mock LLM based on configuration")
            return True

        if self.model_manager.download_model_if_needed():
            self.log_info("Model ready for inference")
            return True

        self.log_error("Failed to download or verify model")
        return False

    def start_relay_polling(self) -> threading.Thread:
        """Start relay polling/registration flow in a daemon thread."""

        relay_thread = threading.Thread(
            target=self.relay_client.poll_relay_continuously,
            daemon=True,
        )
        relay_thread.start()
        relay_target = format_relay_target(self.config.relay_url, self.config.relay_port)
        self.log_info(f"Started relay polling thread for {relay_target}")
        return relay_thread

