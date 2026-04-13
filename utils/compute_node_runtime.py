"""Shared compute-node runtime used by server.py and future desktop bridge code."""
from __future__ import annotations

import logging
import os
import sys
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


def resolve_relay_url(cli_default: str) -> str:
    """Resolve the relay base URL from CLI or env."""

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


SUPPORTED_COMPUTE_MODES = frozenset({"auto", "cpu", "gpu", "hybrid", "metal", "cuda"})
HYBRID_GPU_LAYERS = 16


@dataclass(frozen=True)
class ComputeModeResolution:
    requested_mode: str
    backend_available: str
    effective_mode: str
    mode_reason: Optional[str]
    n_gpu_layers: int


def normalize_compute_mode(mode: Optional[str]) -> str:
    """Normalize operator-provided compute mode values."""

    selected = (mode or "auto").strip().lower()
    legacy_aliases = {"metal": "gpu", "cuda": "gpu"}
    selected = legacy_aliases.get(selected, selected)
    if selected in SUPPORTED_COMPUTE_MODES:
        if selected in ("metal", "cuda"):
            return "gpu"
        return selected
    return "auto"


def detect_backend_for_host() -> str:
    """Return platform-default accelerator backend."""

    platform = (os.environ.get("TOKEN_PLACE_PLATFORM") or sys.platform).lower()
    if platform.startswith("win"):
        return "cuda"
    if platform == "darwin":
        return "metal"
    return "cpu"


def resolve_compute_mode(manager: Any, mode: Optional[str]) -> ComputeModeResolution:
    """Resolve user mode to backend intent and GPU layer policy."""

    selected = normalize_compute_mode(mode)
    backend_available = detect_backend_for_host()

    if selected == "cpu":
        return ComputeModeResolution(
            requested_mode=selected,
            backend_available=backend_available,
            effective_mode="cpu",
            mode_reason="operator selected CPU-only mode",
            n_gpu_layers=0,
        )

    if backend_available == "cpu":
        return ComputeModeResolution(
            requested_mode=selected,
            backend_available=backend_available,
            effective_mode="cpu",
            mode_reason="no supported GPU backend available on this host",
            n_gpu_layers=0,
        )

    if selected == "hybrid":
        return ComputeModeResolution(
            requested_mode=selected,
            backend_available=backend_available,
            effective_mode=f"{backend_available}-hybrid",
            mode_reason=f"partial {backend_available} offload requested",
            n_gpu_layers=HYBRID_GPU_LAYERS,
        )

    # GPU and auto both prefer full offload on GPU-capable hosts.
    mode_label = f"{backend_available}-gpu" if selected == "gpu" else f"{backend_available}-auto"
    reason = (
        f"full {backend_available} offload requested"
        if selected == "gpu"
        else f"auto selected {backend_available} backend"
    )
    return ComputeModeResolution(
        requested_mode=selected,
        backend_available=backend_available,
        effective_mode=mode_label,
        mode_reason=reason,
        n_gpu_layers=-1,
    )


def apply_compute_mode(manager: Any, mode: Optional[str]) -> str:
    """Apply normalized compute mode to model-manager GPU settings."""

    resolution = resolve_compute_mode(manager, mode)
    manager.default_n_gpu_layers = resolution.n_gpu_layers
    manager.requested_compute_mode = resolution.requested_mode
    manager.available_compute_backend = resolution.backend_available
    manager.requested_mode_reason = resolution.mode_reason
    return resolution.requested_mode


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

        self.relay_client.stop()
