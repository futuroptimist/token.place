"""Shared compute-node runtime used by server.py and future desktop bridge code."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol, Sequence
from urllib.parse import urlparse

if TYPE_CHECKING:
    from utils.networking.relay_client import RelayClient

logger = logging.getLogger(__name__)


def _log_info(message: str) -> None:
    """Mirror server logging behavior (suppressed in production)."""

    from config import get_config

    if not get_config().is_production:
        logger.info(message)


def _log_error(message: str, *, exc_info: bool = False) -> None:
    """Mirror server logging behavior (suppressed in production)."""

    from config import get_config

    if not get_config().is_production:
        logger.error(message, exc_info=exc_info)


@dataclass(frozen=True)
class ComputeNodeRuntimeConfig:
    """Runtime configuration shared by all compute-node hosts."""

    relay_url: str
    relay_port: Optional[int]
    use_configured_relay_fallbacks: bool = True


LEGACY_RELAY_REQUIRED_FIELDS = frozenset({"client_public_key", "chat_history", "cipherkey", "iv"})


def is_legacy_relay_payload(payload: Dict[str, Any]) -> bool:
    """Return whether ``payload`` matches the legacy relay sink/source contract."""

    if not isinstance(payload, dict):
        return False
    return LEGACY_RELAY_REQUIRED_FIELDS.issubset(payload.keys())


class RelayRequestAdapter(Protocol):
    """Adapter interface for runtime request handling during protocol migration."""

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        """Return True when the adapter can process ``request_data``."""

    def process(self, request_data: Dict[str, Any]) -> bool:
        """Process ``request_data`` and return success."""


class LegacyRelayRequestAdapter:
    """Compatibility adapter for the existing relay sink/source request shape."""

    def __init__(self, relay_client: "RelayClient"):
        self._relay_client = relay_client

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        return is_legacy_relay_payload(request_data)

    def process(self, request_data: Dict[str, Any]) -> bool:
        return self._relay_client.process_client_request(request_data)


def first_env(keys: List[str]) -> Optional[str]:
    """Return the first non-empty environment variable in ``keys``."""

    for key in keys:
        value = os.environ.get(key)
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def resolve_relay_url(cli_default: str, *, prefer_cli: bool = False) -> str:
    """Resolve the relay base URL from CLI or env."""

    if prefer_cli and cli_default.strip():
        return cli_default.strip()

    env_override = first_env(
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


def resolve_relay_port(cli_default: Optional[int], relay_url: str) -> Optional[int]:
    """Resolve the relay port from CLI, env, or the relay URL."""

    env_port = first_env(["TOKENPLACE_RELAY_PORT", "TOKEN_PLACE_RELAY_PORT", "RELAY_PORT"])

    if env_port is not None:
        try:
            return int(env_port)
        except ValueError:
            _log_error(f"Invalid relay port override: {env_port}")
            return cli_default

    parsed = urlparse(relay_url if "://" in relay_url else f"http://{relay_url}")
    if parsed.port is not None:
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


SUPPORTED_COMPUTE_MODES = frozenset({"auto", "cpu", "gpu", "hybrid"})
LEGACY_COMPUTE_MODE_ALIASES = {"cuda": "gpu", "metal": "gpu"}


def normalize_compute_mode(mode: Optional[str]) -> str:
    """Normalize operator-provided compute mode values."""

    selected = (mode or "auto").strip().lower()
    selected = LEGACY_COMPUTE_MODE_ALIASES.get(selected, selected)
    if selected in SUPPORTED_COMPUTE_MODES:
        return selected
    return "auto"


def apply_compute_mode(manager: Any, mode: Optional[str]) -> str:
    """Apply normalized compute mode to model-manager GPU settings."""

    selected = normalize_compute_mode(mode)
    manager.requested_compute_mode = selected
    if selected == "cpu":
        manager.default_n_gpu_layers = 0
    elif selected == "hybrid":
        manager.default_n_gpu_layers = getattr(manager, "hybrid_n_gpu_layers", 24)
    else:
        # ``auto`` and explicit ``gpu`` request full offload when a compatible backend exists.
        manager.default_n_gpu_layers = -1
    manager.last_compute_diagnostics = {
        "requested_mode": selected,
        "effective_mode": "cpu" if selected == "cpu" else "pending",
        "backend_available": "unknown",
        "backend_selected": "cpu" if selected == "cpu" else "unknown",
        "backend_used": "cpu" if selected == "cpu" else "unknown",
        "n_gpu_layers": manager.default_n_gpu_layers,
        "fallback_reason": None,
    }
    return selected


def compute_mode_diagnostics(manager: Any) -> Dict[str, Any]:
    """Return compute-mode diagnostics from manager state for bridge/UI status payloads."""

    requested = normalize_compute_mode(getattr(manager, "requested_compute_mode", "auto"))
    runtime = getattr(manager, "last_compute_diagnostics", None)
    if isinstance(runtime, dict) and runtime.get("requested_mode") == requested:
        return dict(runtime)

    if requested == "cpu":
        return {
            "requested_mode": requested,
            "effective_mode": "cpu",
            "backend_available": "unknown",
            "backend_selected": "cpu",
            "backend_used": "cpu",
            "n_gpu_layers": 0,
            "fallback_reason": None,
        }
    return {
        "requested_mode": requested,
        "effective_mode": "pending",
        "backend_available": "unknown",
        "backend_selected": "unknown",
        "backend_used": "unknown",
        "n_gpu_layers": getattr(manager, "default_n_gpu_layers", -1),
        "fallback_reason": None,
    }


class ComputeNodeRuntime:
    """Reusable compute-node runtime that wraps relay + model lifecycle concerns."""

    def __init__(
        self,
        runtime_config: ComputeNodeRuntimeConfig,
        *,
        relay_client: Optional["RelayClient"] = None,
        crypto_manager=None,
        model_manager=None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        request_adapters: Optional[Sequence[RelayRequestAdapter]] = None,
    ):
        from utils.crypto.crypto_manager import get_crypto_manager
        from utils.llm.model_manager import get_model_manager
        from utils.networking.relay_client import RelayClient

        self.config = runtime_config
        self._thread_factory = thread_factory
        self.model_manager = model_manager or get_model_manager()
        self.crypto_manager = crypto_manager or get_crypto_manager()
        self.relay_client = relay_client or RelayClient(
            base_url=runtime_config.relay_url,
            port=runtime_config.relay_port,
            crypto_manager=self.crypto_manager,
            model_manager=self.model_manager,
            include_configured_servers=runtime_config.use_configured_relay_fallbacks,
        )
        if request_adapters is None:
            self.request_adapters = [LegacyRelayRequestAdapter(self.relay_client)]
        else:
            self.request_adapters = list(request_adapters)

    def ensure_model_ready(self) -> bool:
        """Initialize model runtime and report readiness."""

        _log_info("Initializing LLM...")
        if self.model_manager.use_mock_llm:
            _log_info("Using mock LLM based on configuration")
            return True

        if self.model_manager.download_model_if_needed():
            _log_info("Model ready for inference")
            return True

        _log_error("Failed to download or verify model")
        return False

    def start_relay_polling(self) -> threading.Thread:
        """Start relay polling in a background thread and return the thread."""

        relay_thread = self._thread_factory(
            target=self.relay_client.poll_relay_continuously,
            daemon=True,
        )
        relay_thread.start()
        relay_target = format_relay_target(self.config.relay_url, self.config.relay_port)
        _log_info(f"Started relay polling thread for {relay_target}")
        return relay_thread

    def process_relay_request(self, request_data: Dict[str, Any]) -> bool:
        """Process relay payloads via registered protocol adapters."""

        for adapter in self.request_adapters:
            if adapter.can_process(request_data):
                return adapter.process(request_data)

        _log_error(
            f"No relay request adapter matched payload keys: {sorted(request_data.keys())}"
        )
        return False

    def register_and_poll_once(self) -> Dict[str, Any]:
        """Ping relay /sink and return response data."""

        return self.relay_client.ping_relay()

    def stop(self) -> None:
        """Stop relay polling and network activity."""

        unregister = getattr(self.relay_client, "unregister", None)
        if callable(unregister):
            if unregister():
                _log_info("Relay compute node unregistered")
            else:
                _log_error("Relay compute node unregister failed; continuing shutdown")
        self.relay_client.stop()
