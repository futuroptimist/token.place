"""Shared compute-node runtime used by server.py and future desktop bridge code."""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from urllib.parse import urlparse

from utils.processing_result import RelayProcessingResult

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



_COMPLETION_SMOKE_REASON_BY_CATEGORY = {
    "metal_memory_allocation": "runtime_completion_smoke_metal_memory_allocation",
    "kv_cache_allocation": "runtime_completion_smoke_kv_cache_allocation",
    "rope_yarn_eval_failure": "runtime_completion_smoke_rope_yarn_eval_failure",
    "unsupported_generation_kwarg": "runtime_completion_smoke_plain_completion_unexpected_kwarg",
    "unexpected_kwarg": "runtime_completion_smoke_plain_completion_unexpected_kwarg",
    "unsupported_prompt_kwarg": "runtime_completion_smoke_plain_completion_method_shape",
    "unsupported_stream_kwarg": "runtime_completion_smoke_plain_completion_unexpected_kwarg",
    "unsupported_stop_kwarg": "runtime_completion_smoke_plain_completion_unexpected_kwarg",
    "method_shape": "runtime_completion_smoke_plain_completion_method_shape",
    "malformed_completion_output": "runtime_completion_smoke_plain_completion_malformed_output",
    "empty_completion_output": "runtime_completion_smoke_plain_completion_empty_output",
    "thinking_leaked": "runtime_completion_smoke_plain_completion_thinking_leaked",
    "worker_exception": "runtime_completion_smoke_plain_completion_worker_exception",
    "worker_timeout": "runtime_completion_smoke_worker_timeout",
    "worker_dead": "runtime_completion_smoke_worker_dead",
}

_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_KEYS = {
    "code",
    "reason",
    "generation_exception_category",
    "exception_type",
    "rejected_option",
    "rejected_generation_kwarg",
    "attempted_generation_kwargs",
    "attempted_plain_completion_methods",
    "result_shape",
    "method",
    "stream",
    "retryable",
    "runtime_healthy",
    "recovery_attempted",
    "recovery_succeeded",
    "profile_id",
    "context_tier",
    "context_window_tokens",
    "n_ctx",
    "kv_cache_mode",
    "type_k",
    "type_v",
    "stderr_tail",
    "child_stderr_tail",
    "sanitized_error_summary",
}


_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_ENUM_VALUES = {
    "code": {
        "compute_node_internal_error",
        "compute_node_options_unsupported",
        "compute_node_runtime_unavailable",
    },
    "reason": {
        "unsupported_generation_option",
        "runtime_chat_template_metadata_missing",
        "runtime_chat_template_renderer_unavailable",
        "runtime_template_tokenizer_bridge_unavailable",
        "malformed_completion_output",
        "empty_completion_output",
        "thinking_leaked",
    },
    "generation_exception_category": {
        "metal_memory_allocation",
        "kv_cache_allocation",
        "rope_yarn_eval_failure",
        "unsupported_generation_kwarg",
        "unexpected_kwarg",
        "unsupported_prompt_kwarg",
        "unsupported_stream_kwarg",
        "unsupported_stop_kwarg",
        "method_shape",
        "worker_exception",
        "empty_completion_output",
        "thinking_leaked",
        "worker_timeout",
        "worker_dead",
        "unknown_generation_exception",
        "malformed_completion_output",
        "empty_completion_output",
        "thinking_leaked",
    },
    "method": {
        "apply_chat_template",
        "create_chat_completion",
        "create_chat_completion_with_recovery",
        "render_and_tokenize_chat",
        "create_chat_completion_from_rendered_prompt",
        "create_completion_from_rendered_prompt",
        "create_completion_keyword_prompt",
        "create_completion_positional_prompt",
        "llama_call_positional_prompt",
        "tokenize",
    },
    "kv_cache_mode": {"f16", "q8_0", "q4_0", "auto", "unknown"},
}
_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")
_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_REDACTED_SUMMARY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]{0,127}:(?:redacted|metal_memory_allocation|kv_cache_allocation|"
    r"rope_yarn_eval_failure|unsupported_kwarg)$"
)
_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_TAIL_WORDS = {
    "llama", "llama_context", "ggml", "metal", "kv", "cache", "alloc", "allocation",
    "memory", "oom", "buffer", "rope", "yarn", "flash_attn", "type_k", "type_v",
    "n_ctx", "context", "unsupported", "keyword", "argument", "failed", "error",
    "runtime", "worker", "exception", "redacted", "path", "out", "of", "to", "create",
}


def _is_controlled_redacted_completion_smoke_diagnostic_tail(value: str) -> bool:
    if len(value) > 1200 or not value.strip():
        return False
    words = re.findall(r"[A-Za-z_]+", value.lower())
    return bool(words) and all(
        word in _SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_TAIL_WORDS for word in words
    )


def _safe_completion_smoke_worker_diagnostic_value(key: str, value: Any) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if not isinstance(value, str):
        return None
    bounded = value[:256]
    enum_values = _SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_ENUM_VALUES.get(key)
    if enum_values is not None:
        return bounded if bounded in enum_values else None
    if key == "exception_type":
        return bounded if _SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_CLASS_RE.fullmatch(bounded) else None
    if key in {"rejected_option", "rejected_generation_kwarg", "profile_id", "context_tier", "type_k", "type_v", "result_shape"}:
        return bounded if _SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_IDENTIFIER_RE.fullmatch(bounded) else None
    if key in {"attempted_generation_kwargs", "attempted_plain_completion_methods"}:
        names = [part for part in bounded.split(",") if part]
        if names and all(_SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_IDENTIFIER_RE.fullmatch(part) for part in names):
            return ",".join(names[:32])
        return None
    if key == "sanitized_error_summary":
        return (
            bounded
            if _SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_REDACTED_SUMMARY_RE.fullmatch(bounded)
            else None
        )
    if key in {"stderr_tail", "child_stderr_tail"}:
        return bounded if _is_controlled_redacted_completion_smoke_diagnostic_tail(bounded) else None
    return None


def _safe_completion_smoke_worker_diagnostics(diagnostics: Any) -> Dict[str, Any]:
    if not isinstance(diagnostics, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key, value in diagnostics.items():
        if key in _SAFE_COMPLETION_SMOKE_WORKER_DIAGNOSTIC_KEYS and isinstance(key, str):
            safe_value = _safe_completion_smoke_worker_diagnostic_value(key, value)
            if safe_value is not None or value is None:
                safe[key] = safe_value
    return safe


def _bounded_safe_error_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}:redacted"


def _classify_completion_smoke_exception(exc: BaseException) -> Tuple[str, str, Dict[str, Any]]:
    diagnostics: Dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "sanitized_error_summary": _bounded_safe_error_summary(exc),
    }
    worker_diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(worker_diagnostics, dict):
        safe_worker = _safe_completion_smoke_worker_diagnostics(worker_diagnostics)
        diagnostics["worker_diagnostics"] = safe_worker
        category = safe_worker.get("generation_exception_category")
        if category in _COMPLETION_SMOKE_REASON_BY_CATEGORY:
            return str(category), _COMPLETION_SMOKE_REASON_BY_CATEGORY[str(category)], diagnostics
        if safe_worker.get("reason") == "unsupported_generation_option":
            return "unsupported_generation_kwarg", _COMPLETION_SMOKE_REASON_BY_CATEGORY["unsupported_generation_kwarg"], diagnostics
    name = type(exc).__name__
    text = f"{name} {exc}".lower()
    if "timeout" in text:
        category = "worker_timeout"
    elif any(token in text for token in ("workerdead", "worker dead", "eof", "broken pipe", "exited before")):
        category = "worker_dead"
    elif "metal" in text and any(token in text for token in ("alloc", "memory", "out of memory", "oom")):
        category = "metal_memory_allocation"
    elif "kv" in text and any(token in text for token in ("alloc", "cache", "memory", "out of memory", "oom")):
        category = "kv_cache_allocation"
    elif "yarn" in text or ("rope" in text and any(token in text for token in ("eval", "scal", "freq"))):
        category = "rope_yarn_eval_failure"
    elif "unexpected keyword argument" in text:
        category = "unsupported_generation_kwarg"
    else:
        category = "unknown_generation_exception"
    return category, _COMPLETION_SMOKE_REASON_BY_CATEGORY.get(category, "runtime_completion_smoke_exception"), diagnostics


def _completion_smoke_reason_from_api_v1_error(error: Dict[str, Any]) -> str:
    internal_reason = error.get("internal_reason")
    if internal_reason == "qwen_thinking_output_leaked":
        return "runtime_completion_smoke_thinking_leaked"
    if internal_reason == "qwen_empty_after_think_wrapper_strip":
        return "runtime_completion_smoke_empty_after_think_strip"
    if internal_reason in {
        "unsupported_generation_option",
        "runtime_rejected_generation_options",
        "runtime_unsupported_generation_kwarg",
    }:
        return "runtime_completion_smoke_unsupported_generation_kwarg"
    if internal_reason in {"rope_yarn_eval_failure", "runtime_rope_yarn_eval_failure"}:
        return "runtime_completion_smoke_rope_yarn_eval_failure"
    if internal_reason in {"metal_memory_allocation", "runtime_metal_memory_allocation"}:
        return "runtime_completion_smoke_metal_memory_allocation"
    if internal_reason in {"kv_cache_allocation", "runtime_kv_cache_allocation"}:
        return "runtime_completion_smoke_kv_cache_allocation"
    if internal_reason in {"worker_timeout", "runtime_worker_timeout"}:
        return "runtime_completion_smoke_worker_timeout"
    if internal_reason in {"worker_dead", "runtime_worker_dead"}:
        return "runtime_completion_smoke_worker_dead"
    # Map new plain-completion diagnostic categories surfaced by the subprocess worker.
    generation_exception_category = error.get("generation_exception_category")
    if generation_exception_category == "empty_completion_output":
        return "runtime_completion_smoke_plain_completion_empty_output"
    if generation_exception_category == "thinking_leaked":
        return "runtime_completion_smoke_plain_completion_thinking_leaked"
    if generation_exception_category == "malformed_completion_output":
        return "runtime_completion_smoke_plain_completion_malformed_output"
    if error.get("code") == "compute_node_invalid_model_output":
        return "runtime_completion_smoke_invalid_model_output"
    if error.get("code") == "compute_node_options_unsupported":
        return "runtime_completion_smoke_unsupported_generation_kwarg"
    return "runtime_completion_smoke_exception"


def _readiness_smoke_model_id(model_manager: Any) -> str:
    """Choose the best configured model id for the API v1 readiness smoke."""

    for value in (
        getattr(model_manager, "api_model_id", None),
        getattr(model_manager, "model_id", None),
        getattr(model_manager, "file_name", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    model_path = getattr(model_manager, "model_path", None)
    if isinstance(model_path, str) and model_path.strip():
        basename = os.path.basename(model_path.strip())
        if basename:
            return basename
    return ""


@dataclass(frozen=True)
class ComputeNodeRuntimeConfig:
    """Runtime configuration shared by all compute-node hosts."""

    relay_url: str
    relay_port: Optional[int]
    use_configured_relay_fallbacks: bool = True
    relay_urls: Tuple[str, ...] = ()


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

    def process(self, request_data: Dict[str, Any]) -> RelayProcessingResult:
        """Process ``request_data`` and return a typed outcome."""


def _process_relay_client_request(
    relay_client: "RelayClient", request_data: Dict[str, Any]
) -> RelayProcessingResult:
    """Process a request through a relay client with typed-result fallback.

    The class-level probe intentionally avoids MagicMock's auto-created instance
    attributes while keeping adapter behavior compatible with older clients.
    """

    process_result = getattr(type(relay_client), "process_client_request_result", None)
    if callable(process_result):
        return process_result(relay_client, request_data)
    submitted = bool(relay_client.process_client_request(request_data))
    return RelayProcessingResult(inference_succeeded=submitted, submitted=submitted)


class LegacyRelayRequestAdapter:
    """Compatibility adapter for the existing deprecated relay request shape."""

    def __init__(self, relay_client: "RelayClient"):
        self._relay_client = relay_client

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        return is_legacy_relay_payload(request_data)

    def process(self, request_data: Dict[str, Any]) -> RelayProcessingResult:
        return _process_relay_client_request(self._relay_client, request_data)


class ApiV1RelayRequestAdapter:
    """Adapter for API v1 E2EE relay request envelopes."""

    def __init__(self, relay_client: "RelayClient"):
        self._relay_client = relay_client

    def can_process(self, request_data: Dict[str, Any]) -> bool:
        return is_api_v1_relay_payload(request_data)

    def process(self, request_data: Dict[str, Any]) -> RelayProcessingResult:
        return _process_relay_client_request(self._relay_client, request_data)


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


def resolve_relay_port(
    cli_default: Optional[int], relay_url: str, *, prefer_cli: bool = False
) -> Optional[int]:
    """Resolve the relay port from CLI, env, or the relay URL."""

    parsed = urlparse(relay_url if "://" in relay_url else f"http://{relay_url}")
    if prefer_cli and parsed.port is not None:
        return parsed.port
    if prefer_cli and cli_default is not None:
        return cli_default

    env_port = first_env(["TOKENPLACE_RELAY_PORT", "TOKEN_PLACE_RELAY_PORT", "RELAY_PORT"])

    if env_port is not None:
        try:
            return int(env_port)
        except ValueError:
            _log_error(f"Invalid relay port override: {env_port}")
            return cli_default

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
        "context_tier": getattr(manager, "context_tier", "8k-fast"),
        "context_window_tokens": getattr(manager, "context_window_tokens", None),
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
        "context_tier": getattr(manager, "context_tier", "8k-fast"),
        "context_window_tokens": getattr(manager, "context_window_tokens", None),
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
            explicit_relay_urls=runtime_config.relay_urls[1:],
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

        setattr(self.model_manager, 'last_runtime_init_error', None)
        if not self.ensure_model_ready():
            setattr(
                self.model_manager,
                'last_runtime_init_error',
                'model_file_preflight_failed',
            )
            return False

        get_llm_instance = getattr(self.model_manager, "get_llm_instance", None)
        if not callable(get_llm_instance):
            setattr(
                self.model_manager,
                'last_runtime_init_error',
                'model_manager_missing_get_llm_instance',
            )
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
            else:
                setattr(
                    self.model_manager,
                    'last_runtime_init_error',
                    'get_llm_instance_returned_none',
                )
            _log_error(message)
            return False

        create_chat_completion = getattr(llm_runtime, "create_chat_completion", None)
        if not callable(create_chat_completion):
            setattr(
                self.model_manager,
                'last_runtime_init_error',
                'runtime_missing_create_chat_completion',
            )
            _log_error(
                "API v1 runtime warmup failed: runtime missing callable create_chat_completion"
            )
            return False

        _log_info(f"API v1 runtime warmup model instantiated: {model_path}")

        diagnostics = getattr(self.model_manager, "last_compute_diagnostics", None)
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        model_profile = getattr(self.model_manager, "model_profile", {}) or {}
        context_tier = getattr(self.model_manager, "context_tier", "8k-fast")
        context_window_tokens = getattr(self.model_manager, "context_window_tokens", None)
        rope_policy = model_profile.get("rope_scaling_policy") or {}
        packaged_render_tokenize_bridge_available = callable(
            getattr(llm_runtime, "render_and_tokenize_chat", None)
        )
        legacy_template_available = callable(getattr(llm_runtime, "apply_chat_template", None))
        legacy_tokenizer_available = callable(getattr(llm_runtime, "tokenize", None))
        from utils.networking.relay_client import RelayClient

        qwen_non_thinking_enforced = RelayClient._api_v1_qwen_non_thinking_required(
            model_profile
        )

        yarn_diagnostics = getattr(self.model_manager, "last_yarn_rope_diagnostics", None)
        if not isinstance(yarn_diagnostics, dict):
            yarn_diagnostics = {}

        diagnostics.update({
            "api_v1_readiness_model_id": getattr(self.model_manager, "api_model_id", None),
            "api_v1_readiness_model_profile_provider": model_profile.get("provider"),
            "api_v1_readiness_model_profile_id": (
                model_profile.get("profile_id")
                or model_profile.get("id")
                or model_profile.get("name")
            ),
            "api_v1_readiness_context_tier": context_tier,
            "api_v1_readiness_context_window_tokens": context_window_tokens,
            "api_v1_readiness_template_mode": model_profile.get("chat_template_policy") or "llama-3",
            "api_v1_readiness_packaged_bridge_available": packaged_render_tokenize_bridge_available,
            "api_v1_readiness_legacy_template_available": legacy_template_available,
            "api_v1_readiness_legacy_tokenizer_available": legacy_tokenizer_available,
            "api_v1_readiness_tokenizer_render_bridge_capability_available": (
                packaged_render_tokenize_bridge_available
                or (legacy_template_available and legacy_tokenizer_available)
            ),
            "api_v1_readiness_tokenizer_render_bridge_available": False,
            "api_v1_readiness_non_thinking_enforced": qwen_non_thinking_enforced,
            "api_v1_readiness_yarn_rope_enabled": bool(
                rope_policy.get("type") == "yarn" and yarn_diagnostics.get("supported") is True
            ),
            "api_v1_readiness_yarn_rope_supported": yarn_diagnostics.get("supported"),
            "api_v1_readiness_yarn_rope_missing_reason": yarn_diagnostics.get("missing_reason"),
            "api_v1_readiness_yarn_rope_factor": rope_policy.get("factor"),
            "api_v1_readiness_yarn_original_context_tokens": rope_policy.get(
                "original_context_tokens"
            ),
            "api_v1_readiness_yarn_resolver_source": yarn_diagnostics.get("yarn_resolver_source"),
            "api_v1_readiness_kv_cache_mode": diagnostics.get("kv_cache_mode"),
            "api_v1_readiness_llama_cpp_python_version": yarn_diagnostics.get("llama_cpp_python_version"),
            "api_v1_readiness_backend_used": diagnostics.get("backend_used"),
        })

        yarn_required_for_active_tier = (
            model_profile.get("provider") == "qwen"
            and rope_policy.get("type") == "yarn"
            and context_tier == rope_policy.get("required_for_tier", "64k-full")
        )
        if yarn_required_for_active_tier and yarn_diagnostics.get("supported") is not True:
            diagnostics.update({
                "api_v1_runtime_ready": False,
                "api_v1_readiness_result": "failed",
                "api_v1_readiness_error_code": "compute_node_yarn_rope_unsupported",
                "api_v1_readiness_error_reason": (
                    yarn_diagnostics.get("missing_reason") or "missing_yarn_rope_runtime_support"
                ),
            })
            self.model_manager.last_compute_diagnostics = diagnostics
            setattr(
                self.model_manager,
                'last_runtime_init_error',
                "API v1 runtime readiness failed: Qwen 64K YaRN/RoPE support missing",
            )
            _log_error("API v1 runtime readiness failed: Qwen 64K YaRN/RoPE support missing")
            return False

        try:
            smoke_messages = [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": "Reply with exactly: ok"},
            ]
            admitted, admission_error, prompt_tokens = (
                self.relay_client._api_v1_authoritative_context_admission(
                    llm_instance=llm_runtime,
                    messages=RelayClient._api_v1_prepare_qwen_non_thinking_messages(
                        smoke_messages, model_profile
                    ),
                    requested_output_tokens=1,
                    requested_context_tier=str(context_tier),
                )
            )
        except Exception as exc:
            admitted = False
            admission_error = {"code": "compute_node_context_admission_unavailable"}
            prompt_tokens = None
            diagnostics["api_v1_readiness_exception_type"] = type(exc).__name__

        prompt_tokens_available = isinstance(prompt_tokens, int) and prompt_tokens > 0
        diagnostics.update({
            "api_v1_runtime_ready": bool(admitted),
            "api_v1_readiness_result": "passed" if admitted else "failed",
            "api_v1_readiness_prompt_tokens": prompt_tokens,
            "api_v1_readiness_tokenizer_render_bridge_available": bool(
                admitted and prompt_tokens_available
            ),
            "api_v1_readiness_error_code": (admission_error or {}).get("code") if not admitted else None,
            "api_v1_readiness_error_reason": (
                (admission_error or {}).get("internal_reason")
                or (admission_error or {}).get("reason")
            ) if not admitted else None,
        })
        self.model_manager.last_compute_diagnostics = diagnostics
        completion_smoke_required = bool(
            diagnostics["api_v1_readiness_non_thinking_enforced"]
            or os.getenv("TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION") == "1"
        )
        if admitted and completion_smoke_required:
            smoke_max_tokens = 64 if qwen_non_thinking_enforced else 4
            diagnostics["api_v1_readiness_completion_smoke_max_tokens"] = smoke_max_tokens
            diagnostics["api_v1_readiness_completion_smoke_path"] = "shared_api_v1_generation"
            try:
                generation_client = self.relay_client
                if not callable(
                    getattr(generation_client, "_generate_api_v1_response_with_runtime_model", None)
                ):
                    generation_client = RelayClient(
                        self.config.relay_url,
                        self.config.relay_port,
                        self.crypto_manager,
                        self.model_manager,
                        include_configured_servers=False,
                    )
                smoke_envelope = generation_client._generate_api_v1_response_with_runtime_model(
                    request_id="api-v1-readiness-smoke",
                    model_id=_readiness_smoke_model_id(self.model_manager),
                    messages=smoke_messages,
                    options={"max_tokens": smoke_max_tokens, "stream": False},
                    requested_context_tier=str(context_tier),
                )
                api_v1_response = (
                    smoke_envelope.get("api_v1_response")
                    if isinstance(smoke_envelope, dict)
                    else None
                )
                smoke_error = (
                    api_v1_response.get("error")
                    if isinstance(api_v1_response, dict)
                    and isinstance(api_v1_response.get("error"), dict)
                    else None
                )
                smoke_message = (
                    api_v1_response.get("message")
                    if isinstance(api_v1_response, dict)
                    and isinstance(api_v1_response.get("message"), dict)
                    else None
                )
                if smoke_error is not None:
                    admitted = False
                    smoke_invalid_reason = _completion_smoke_reason_from_api_v1_error(smoke_error)
                    admission_error = {
                        "code": smoke_error.get("code") or "compute_node_context_admission_unavailable",
                        "internal_reason": smoke_invalid_reason,
                    }
                    diagnostics["api_v1_readiness_completion_smoke_result"] = "failed"
                    diagnostics["api_v1_readiness_completion_smoke_failure_reason"] = smoke_invalid_reason
                    diagnostics["api_v1_readiness_completion_smoke_shape"] = "api_v1_error"
                    diagnostics["api_v1_readiness_completion_smoke_error_code"] = smoke_error.get("code")
                    diagnostics["api_v1_readiness_completion_smoke_internal_reason"] = smoke_error.get("internal_reason")
                    exception_category = smoke_error.get("exception_category")
                    if isinstance(exception_category, str):
                        diagnostics["api_v1_readiness_completion_smoke_exception_category"] = (
                            exception_category.replace("runtime_", "")
                        )
                    exception_type = smoke_error.get("exception_type")
                    if isinstance(exception_type, str):
                        diagnostics["api_v1_readiness_completion_smoke_exception_type"] = exception_type
                    worker_diagnostics = smoke_error.get("worker_diagnostics")
                    if isinstance(worker_diagnostics, dict):
                        diagnostics["api_v1_readiness_completion_smoke_worker_diagnostics"] = (
                            _safe_completion_smoke_worker_diagnostics(worker_diagnostics)
                        )
                    for key in (
                        "runtime_healthy",
                        "recovery_attempted",
                        "recovery_succeeded",
                        "rejected_option",
    "rejected_generation_kwarg",
    "attempted_generation_kwargs",
    "attempted_plain_completion_methods",
    "result_shape",
                    ):
                        if key in smoke_error:
                            diagnostics[f"api_v1_readiness_completion_smoke_{key}"] = smoke_error[key]
                elif (
                    smoke_message is not None
                    and smoke_message.get("role") == "assistant"
                    and isinstance(smoke_message.get("content"), str)
                    and smoke_message.get("content", "").strip()
                ):
                    diagnostics["api_v1_readiness_completion_smoke_result"] = "passed"
                    diagnostics["api_v1_readiness_completion_smoke_shape"] = "api_v1_assistant_message"
                    supported_generation_kwargs = getattr(
                        generation_client, "_api_v1_generation_kwargs_supported", set()
                    )
                    filtered_generation_kwargs = getattr(
                        generation_client, "_api_v1_generation_kwargs_filtered", set()
                    )
                    diagnostics["api_v1_generation_kwargs_supported"] = sorted(
                        str(name) for name in supported_generation_kwargs
                    )
                    diagnostics["api_v1_generation_kwargs_filtered"] = sorted(
                        str(name) for name in filtered_generation_kwargs
                    )
                else:
                    admitted = False
                    admission_error = {
                        "code": "compute_node_invalid_model_output",
                        "internal_reason": "runtime_completion_smoke_invalid_model_output",
                    }
                    diagnostics["api_v1_readiness_completion_smoke_result"] = "failed"
                    diagnostics["api_v1_readiness_completion_smoke_failure_reason"] = "runtime_completion_smoke_invalid_model_output"
                    diagnostics["api_v1_readiness_completion_smoke_shape"] = "invalid_api_v1_envelope"
            except Exception as exc:
                admitted = False
                exception_category, safe_reason, exception_diagnostics = (
                    _classify_completion_smoke_exception(exc)
                )
                admission_error = {
                    "code": "compute_node_context_admission_unavailable",
                    "internal_reason": safe_reason,
                }
                diagnostics["api_v1_readiness_completion_smoke_result"] = "failed"
                diagnostics["api_v1_readiness_completion_smoke_failure_reason"] = safe_reason
                diagnostics["api_v1_readiness_completion_smoke_exception_category"] = exception_category
                diagnostics["api_v1_readiness_completion_smoke_shape"] = "exception"
                diagnostics["api_v1_readiness_completion_smoke_exception_type"] = exception_diagnostics.get("exception_type")
                diagnostics["api_v1_readiness_completion_smoke_safe_summary"] = exception_diagnostics.get("sanitized_error_summary")
                if "worker_diagnostics" in exception_diagnostics:
                    diagnostics["api_v1_readiness_completion_smoke_worker_diagnostics"] = exception_diagnostics["worker_diagnostics"]
                diagnostics["api_v1_readiness_repair_retry_attempted"] = False
                diagnostics["api_v1_readiness_recovery_succeeded"] = False
            diagnostics["api_v1_runtime_ready"] = bool(admitted)
            diagnostics["api_v1_readiness_result"] = "passed" if admitted else "failed"
            diagnostics["api_v1_readiness_error_code"] = (admission_error or {}).get("code") if not admitted else None
            diagnostics["api_v1_readiness_error_reason"] = (
                (admission_error or {}).get("internal_reason")
                or (admission_error or {}).get("reason")
            ) if not admitted else None
            self.model_manager.last_compute_diagnostics = diagnostics

        if not admitted:
            admission_code = (admission_error or {}).get("code") or "unknown"
            admission_reason = (
                (admission_error or {}).get("internal_reason")
                or (admission_error or {}).get("reason")
                or "unknown"
            )
            bridge_capability_missing = not diagnostics.get(
                "api_v1_readiness_tokenizer_render_bridge_capability_available"
            )
            bridge_admission_failure = (
                admission_reason == "runtime_template_tokenizer_bridge_unavailable"
                or (
                    admission_code == "compute_node_context_admission_unavailable"
                    and bridge_capability_missing
                )
            )
            qwen_bridge_missing = (
                diagnostics.get("api_v1_readiness_non_thinking_enforced")
                and bridge_admission_failure
            )
            message = (
                "Qwen API v1 context admission unavailable: "
                "runtime template/tokenizer bridge missing"
                if qwen_bridge_missing
                else (
                    "API v1 context admission readiness failed: "
                    f"{admission_code} reason={admission_reason}"
                )
            )
            setattr(self.model_manager, 'last_runtime_init_error', message)
            _log_error(message)
            return False

        setattr(self.model_manager, 'last_runtime_init_error', None)
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

    def process_relay_request_result(self, request_data: Dict[str, Any]) -> RelayProcessingResult:
        """Process relay payloads via registered protocol adapters."""

        for adapter in self.request_adapters:
            if adapter.can_process(request_data):
                return adapter.process(request_data)

        _log_error(
            f"No relay request adapter matched payload keys: {sorted(request_data.keys())}"
        )
        return RelayProcessingResult.submission_failed(safe_error_code="unsupported_relay_payload")

    def process_relay_request(self, request_data: Dict[str, Any]) -> bool:
        """Compatibility wrapper; True means encrypted response/error submission succeeded."""

        return bool(self.process_relay_request_result(request_data))

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
            registered_relays = getattr(self.relay_client, "_api_v1_registered_relays", set())
            should_unregister = isinstance(registered_relays, set) and bool(registered_relays)
            unregister_fn = getattr(self.relay_client, "unregister_from_relay", None)
            if callable(unregister_fn) and should_unregister:
                if not unregister_fn():
                    _log_warning("Relay unregister request failed during shutdown")
            elif callable(unregister_fn):
                _log_info("Skipping relay unregister because no API v1 registration succeeded")
        except Exception:
            _log_warning(
                "Relay unregister request raised during shutdown; continuing stop",
                exc_info=True,
            )
