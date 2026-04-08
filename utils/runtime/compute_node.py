"""Reusable compute-node runtime used by server.py and future desktop bridge code."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from utils.crypto.crypto_manager import get_crypto_manager
from utils.llm.model_manager import get_model_manager
from utils.networking.relay_client import RelayClient

logger = logging.getLogger("server_app")


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime configuration shared across compute-node entry points."""

    relay_url: str
    relay_port: Optional[int]
    server_host: str = "127.0.0.1"
    server_port: int = 3000


class ComputeNodeRuntime:
    """Owns compute-node bootstrap and relay request handling orchestration."""

    def __init__(self, runtime_config: RuntimeConfig, *, is_production: bool = False):
        self.runtime_config = runtime_config
        self._is_production = is_production
        self.model_manager = get_model_manager()
        self.crypto_manager = get_crypto_manager()
        self.relay_client = RelayClient(
            base_url=runtime_config.relay_url,
            port=runtime_config.relay_port,
            crypto_manager=self.crypto_manager,
            model_manager=self.model_manager,
        )

    def log_info(self, message: str) -> None:
        """Log info only in non-production environments."""

        if not self._is_production:
            logger.info(message)

    def log_error(self, message: str, exc_info: bool = False) -> None:
        """Log errors only in non-production environments."""

        if not self._is_production:
            logger.error(message, exc_info=exc_info)

    def initialize_model_readiness(self) -> None:
        """Initialize the LLM by downloading the model if needed."""

        self.log_info("Initializing LLM...")
        if self.model_manager.use_mock_llm:
            self.log_info("Using mock LLM based on configuration")
            return

        if self.model_manager.download_model_if_needed():
            self.log_info("Model ready for inference")
            return

        self.log_error("Failed to download or verify model")

    def process_relay_request(self, request_data: dict) -> bool:
        """Decrypt request, run inference, encrypt response, and post back via relay."""

        return self.relay_client.process_client_request(request_data)

    def start_relay_polling(self) -> threading.Thread:
        """Start relay polling in a background daemon thread."""

        relay_thread = threading.Thread(
            target=self.relay_client.poll_relay_continuously,
            daemon=True,
        )
        relay_thread.start()
        self.log_info(f"Started relay polling thread for {self.relay_target}")
        return relay_thread

    @property
    def relay_target(self) -> str:
        """Create a display-ready relay target without duplicating explicit URL ports."""

        relay_url = self.runtime_config.relay_url
        relay_port = self.runtime_config.relay_port
        if relay_port is None:
            return relay_url

        from urllib.parse import urlparse

        parsed = urlparse(relay_url if "://" in relay_url else f"http://{relay_url}")
        if parsed.port is not None:
            return relay_url

        return f"{relay_url}:{relay_port}"
