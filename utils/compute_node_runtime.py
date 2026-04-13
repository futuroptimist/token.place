"""Shared compute-node runtime used by server.py and future desktop bridge code."""
from __future__ import annotations

import logging
import platform
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


def normalize_compute_mode(mode: Optional[str]) -> str:
    """Normalize operator-provided compute mode values."""

    selected = (mode or "auto").strip().lower()
    if selected in SUPPORTED_COMPUTE_MODES:
        return selected
    return "auto"


def _detect_available_gpu_backend() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "cuda"
    if system == "darwin":
        return "metal"
    return "none"


def _hybrid_gpu_layers(manager: Any) -> int:
    configured = 20
    config = getattr(manager, "config", None)
    if config is not None and hasattr(config, "get"):
        configured = config.get("model.hybrid_n_gpu_layers", 20)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = 20
    return max(1, value)


def apply_compute_mode(manager: Any, mode: Optional[str]) -> str:
    """Apply normalized compute mode to model-manager GPU settings."""

    selected = normalize_compute_mode(mode)
    available_backend = _detect_available_gpu_backend()
    requested_mode = selected
    effective_mode = "cpu"
    mode_reason: Optional[str] = None

    if selected == "cpu":
        manager.default_n_gpu_layers = 0
        mode_reason = "Operator selected CPU-only mode."
    elif selected in {"cuda", "metal"}:
        requested_mode = "gpu"
        if selected == available_backend:
            manager.default_n_gpu_layers = -1
            effective_mode = f"{available_backend}_gpu"
            mode_reason = f"Explicit {available_backend} request."
        else:
            manager.default_n_gpu_layers = 0
            mode_reason = (
                f"Requested {selected} backend is not available on this host; using CPU fallback."
            )
    elif selected == "gpu":
        if available_backend == "none":
            manager.default_n_gpu_layers = 0
            mode_reason = "No supported GPU backend available on this host; using CPU fallback."
        else:
            manager.default_n_gpu_layers = -1
            effective_mode = f"{available_backend}_gpu"
            mode_reason = f"Operator selected GPU-only mode ({available_backend})."
    elif selected == "hybrid":
        if available_backend == "none":
            manager.default_n_gpu_layers = 0
            mode_reason = "Hybrid mode requested but no GPU backend is available; using CPU fallback."
        else:
            manager.default_n_gpu_layers = _hybrid_gpu_layers(manager)
            effective_mode = f"hybrid_{available_backend}"
            mode_reason = (
                f"Operator selected hybrid mode with n_gpu_layers={manager.default_n_gpu_layers}."
            )
    else:
        if available_backend == "none":
            manager.default_n_gpu_layers = 0
            mode_reason = "Auto selected; no supported GPU backend detected, using CPU."
        else:
            manager.default_n_gpu_layers = -1
            effective_mode = f"{available_backend}_gpu"
            mode_reason = f"Auto selected; requesting full {available_backend} GPU offload."

    manager.requested_compute_mode = requested_mode
    manager.available_gpu_backend = available_backend
    manager.mode_reason = mode_reason
    manager.last_runtime_compute_status = {
        "requested_mode": requested_mode,
        "effective_mode": effective_mode if manager.default_n_gpu_layers != 0 else "cpu",
        "backend_available": available_backend,
        "mode_reason": mode_reason,
        "n_gpu_layers": manager.default_n_gpu_layers,
    }
    return requested_mode


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
