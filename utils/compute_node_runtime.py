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


def _log_warning(message: str, *, exc_info: bool = False) -> None:
    """Mirror server logging behavior (suppressed in production)."""

    from config import get_config

    if not get_config().is_production:
        logger.warning(message, exc_info=exc_info)


@dataclass(frozen=True)
class ComputeNodeRuntimeConfig:
    """Runtime configuration shared by all compute-node hosts."""

    relay_url: str
    relay_port: Optional[int]
    use_configured_relay_fallbacks: bool = True


LEGACY_RELAY_REQUIRED_FIELDS = frozenset({"client_public_key", "chat_history", "cipherkey", "iv"})


def is_legacy_relay_payload(payload: Dict[str, Any]) -> bool:
    """Return whether ``payload`` matches the deprecated relay contract."""

    if not isinstance(payload, dict):
        return False
    if payload.get("protocol") == "tokenplace_api_v1_relay_e2ee" or payload.get("e2ee_v1") is True:
        return False
    return LEGACY_RELAY_REQUIRED_FIELDS.issubset(payload.keys())


def is_api_v1_relay_payload(payload: Dict[str, Any]) -> bool:
    """Return whether ``payload`` matches the API v1 E2EE relay envelope."""

    if not isinstance(payload, dict):
        return False
    required_string_fields = ("request_id", "client_public_key", "chat_history", "cipherkey", "iv")
    if payload.get("protocol") != "tokenplace_api_v1_relay_e2ee":
        return False
    if payload.get("version") != 1:
        return False
    for field in required_string_fields:
        value = payload.get(field)
        if not isinstance(value, str):
            return False
    return True


class RelayRequestAdapter(Protocol):
    """Adapter interface for runtime request handling during protocol migration."""

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        """Return True when the adapter can process ``request_data``."""

    def process(self, request_data: Dict[str, Any]) -> bool:
        """Process ``request_data`` and return success."""


class LegacyRelayRequestAdapter:
    """Compatibility adapter for the existing deprecated relay request shape."""

    def __init__(self, relay_client: "RelayClient"):
        self._relay_client = relay_client

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        return is_legacy_relay_payload(request_data)

    def process(self, request_data: Dict[str, Any]) -> bool:
        return self._relay_client.process_client_request(request_data)


class ApiV1RelayRequestAdapter:
    """Adapter for API v1 E2EE relay request envelopes."""

    def __init__(self, relay_client: "RelayClient"):
        self._relay_client = relay_client

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        return is_api_v1_relay_payload(request_data)

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
            self.request_adapters = [
                ApiV1RelayRequestAdapter(self.relay_client),
            ]
        else:
            self.request_adapters = list(request_adapters)

    def ensure_model_ready(self) -> bool:
        """Download or verify the model file before runtime initialization."""

        _log_info("Initializing LLM model-file preflight...")
        if self.model_manager.use_mock_llm:
            _log_info("Using mock LLM based on configuration")
            return True

        model_path = getattr(self.model_manager, "model_path", "unknown")
        _log_info(f"Checking model file for API v1 runtime warmup: {model_path}")
        if self.model_manager.download_model_if_needed():
            _log_info(f"Model file ready for runtime initialization: {model_path}")
            return True

        _log_error("Failed to download or verify model file")
        return False

    def ensure_api_v1_runtime_ready(self) -> bool:
        """Warm and validate API v1 non-streaming runtime before polling."""

        if not self.ensure_model_ready():
            return False

        get_llm_instance = getattr(self.model_manager, "get_llm_instance", None)
        if not callable(get_llm_instance):
            _log_error("Model manager missing get_llm_instance required for API v1 warmup")
            return False

        model_path = getattr(self.model_manager, "model_path", "unknown")
        _log_info(f"API v1 runtime warmup about to instantiate model: {model_path}")
        try:
            llm_runtime = get_llm_instance()
        except Exception as exc:
            setattr(self.model_manager, 'last_runtime_init_error', str(exc))
            _log_error("Failed to initialize API v1 runtime for compute node", exc_info=True)
            return False

        if llm_runtime is None:
            detail = getattr(self.model_manager, 'last_runtime_init_error', None)
            message = "API v1 runtime warmup failed: get_llm_instance returned None"
            if detail:
                message = f"{message} ({detail})"
            _log_error(message)
            return False

        create_chat_completion = getattr(llm_runtime, "create_chat_completion", None)
        if not callable(create_chat_completion):
            _log_error(
                "API v1 runtime warmup failed: runtime missing callable create_chat_completion"
            )
            return False

        _log_info(f"API v1 runtime warmup model instantiated: {model_path}")

        diagnostics = getattr(self.model_manager, "last_compute_diagnostics", None)
        if isinstance(diagnostics, dict):
            diagnostics["api_v1_runtime_ready"] = True
            self.model_manager.last_compute_diagnostics = diagnostics

        return True

    def start_relay_polling(self) -> threading.Thread:
        """Start relay polling in a background thread and return the thread."""

        poll_target = getattr(
            self.relay_client,
            "poll_api_v1_encrypted_work_continuously",
            None,
        )
        if not callable(poll_target):
            raise RuntimeError(
                "API v1 E2EE relay polling is required; legacy relay polling is deprecated"
            )
        relay_thread = self._thread_factory(
            target=poll_target,
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

    def start_relay_session(self) -> None:
        """Reset relay-client stop state before a fresh operator session polls."""

        start = getattr(self.relay_client, "start", None)
        if callable(start):
            start()

    def register_and_poll_once(self) -> Dict[str, Any]:
        """Poll relay for one encrypted API v1 work item."""

        return self.relay_client.poll_api_v1_encrypted_work()

    def submit_api_v1_error_response(
        self, request_data: Dict[str, Any], *, code: str, message: str
    ) -> bool:
        """Submit an encrypted API v1 error envelope without exposing plaintext to relay state."""

        submit_error = getattr(self.relay_client, "submit_api_v1_error_response", None)
        if not callable(submit_error):
            _log_error("Relay client cannot submit API v1 encrypted error responses")
            return False
        return bool(submit_error(request_data, code=code, message=message))

    def stop(self) -> None:
        """Stop relay polling and network activity."""
        try:
            self.relay_client.stop()
            unregister_fn = getattr(self.relay_client, "unregister_from_relay", None)
            if callable(unregister_fn):
                if not unregister_fn():
                    _log_warning("Relay unregister request failed during shutdown")
        except Exception:
            _log_warning(
                "Relay unregister request raised during shutdown; continuing stop",
                exc_info=True,
            )
