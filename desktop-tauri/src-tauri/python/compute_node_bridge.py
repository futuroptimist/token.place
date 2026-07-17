#!/usr/bin/env python3
"""Desktop compute-node bridge that reuses the shared compute runtime."""

from __future__ import annotations

import argparse
import concurrent.futures
import inspect
import json
import math
import os
import queue
import re
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

if __package__ in (None, ""):
    # Use os.path here so a polluted PYTHONPATH cannot make a third-party
    # pathlib backport crash before path_bootstrap repairs sys.path.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)
from pathlib import Path

try:
    from desktop_runtime_setup import (
        desktop_gpu_runtime_failure_message,
        ensure_desktop_llama_runtime,
        ensure_desktop_python_dependencies,
        maybe_reexec_for_runtime_refresh,
    )
except ModuleNotFoundError:
    def desktop_gpu_runtime_failure_message(_mode: str, _runtime_setup: Dict[str, str]) -> None:
        return None

    def ensure_desktop_llama_runtime(_mode: str, **_kwargs: Any) -> Dict[str, str]:
        return {
            "selected_backend": "cpu",
            "detected_device": "cpu",
            "runtime_action": "unavailable",
            "fallback_reason": "desktop_runtime_setup module missing",
        }

    def ensure_desktop_python_dependencies(*, repo_root: Optional[Path] = None) -> Dict[str, str]:
        return {
            "ok": "false",
            "action": "desktop_runtime_setup module missing",
            "missing": "unknown",
            "interpreter": sys.executable,
            "import_root": str(repo_root) if repo_root is not None else "unknown",
            "detail": "desktop_runtime_setup module missing",
        }

    def maybe_reexec_for_runtime_refresh(
        _runtime_setup: Dict[str, str], *, allow_reexec: bool = True
    ) -> None:
        return

def _is_repo_llama_cpp_shim(module_path: Any) -> bool:
    try:
        from utils.llm.model_manager import _is_repo_llama_cpp_shim as _shim_detector
    except ModuleNotFoundError:
        return False
    return _shim_detector(module_path)

_stdin_lines: queue.Queue[str] = queue.Queue()
_stdin_reader_started = False
_stdin_reader_lock = threading.Lock()
_stop_requested_latched = threading.Event()
EARLY_STARTUP_EXIT_ERROR = "compute-node bridge exited before emitting a startup event"
WARM_LOAD_DEFAULT = "1"
RUNTIME_PATH_DEFAULT = "bridge"
API_V1_WARM_LOAD_WAIT_DEFAULT_SECONDS = 120.0
PRE_REGISTRATION_PROGRESS_INTERVAL_SECONDS = 30.0
PRE_REGISTRATION_STATUS_INTERVAL_SECONDS = 5.0
RECOVERY_ATTEMPTS_DEFAULT = 2
RECOVERY_BACKOFF_DEFAULT_SECONDS = 0.25


def _drain_stale_stdin_cancel_messages() -> int:
    """Discard queued cancel controls before a fresh operator session starts."""

    drained = 0
    while True:
        try:
            line = _stdin_lines.get_nowait()
        except queue.Empty:
            return drained
        if isinstance(line, str) and line.strip():
            drained += 1


def _reset_bridge_lifecycle_state(operator_session_id: str) -> None:
    """Reset process-local stop/cancel state for a first-class fresh session."""

    _stop_requested_latched.clear()
    drained = _drain_stale_stdin_cancel_messages()
    print(
        "desktop.compute_node_bridge.lifecycle.reset "
        f"operator_session_id={operator_session_id} "
        f"stale_cancel_messages_drained={drained}",
        file=sys.stderr,
    )

_POLL_CANCELLED = object()


class _DaemonWarmLoadFuture:
    """Run warm-load work on a daemon thread so wedged init cannot keep the process alive."""

    def __init__(self, fn: Any) -> None:
        self._future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
        self._thread = threading.Thread(
            target=self._run,
            args=(fn,),
            name="tokenplace-warm-load",
            daemon=True,
        )
        self._thread.start()

    def _run(self, fn: Any) -> None:
        if not self._future.set_running_or_notify_cancel():
            return
        try:
            self._future.set_result(fn())
        except BaseException as exc:
            self._future.set_exception(exc)

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: Optional[float] = None) -> Any:
        return self._future.result(timeout=timeout)


class _CancelablePollWorker:
    """Run one relay poll at a time while the bridge keeps checking for cancel."""

    def __init__(self, *, operator_session_id: str = "unknown") -> None:
        self._tasks: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._operator_session_id = operator_session_id
        self._thread = threading.Thread(
            target=self._run,
            name="tokenplace-relay-poll",
            daemon=True,
        )
        self._thread.start()
        print(
            "desktop.compute_node_bridge.poll.worker_created "
            f"operator_session_id={self._operator_session_id}",
            file=sys.stderr,
        )

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            fn, result_queue = task
            try:
                result_queue.put((True, fn()))
            except BaseException as exc:  # pragma: no cover - exercised via call() re-raise
                result_queue.put((False, exc))

    def call(
        self,
        fn: Any,
        should_cancel: Any,
        *,
        poll_interval: float = 0.1,
        on_cancel: Optional[Any] = None,
    ) -> Any:
        if self._closed:
            return _POLL_CANCELLED
        result_queue: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        self._tasks.put((fn, result_queue))
        cancel_notified = False
        while True:
            try:
                ok, value = result_queue.get(timeout=poll_interval)
            except queue.Empty:
                if should_cancel():
                    if not cancel_notified and callable(on_cancel):
                        cancel_notified = True
                        on_cancel()
                    return _POLL_CANCELLED
                continue
            if ok:
                return value
            raise value

    def shutdown(self) -> None:
        self._closed = True
        print(
            "desktop.compute_node_bridge.poll.worker_close_requested "
            f"operator_session_id={self._operator_session_id}",
            file=sys.stderr,
        )
        try:
            self._tasks.put_nowait(None)
        except queue.Full:  # pragma: no cover - unbounded queue is not expected to fill
            pass


def _registration_fresh(relay_client: Any, relay_url: str) -> bool:
    """Return whether bridge UI should report the relay registration as current."""

    is_fresh = getattr(relay_client, "api_v1_registration_fresh", None)
    if callable(is_fresh):
        try:
            return bool(is_fresh(relay_url))
        except TypeError:
            return bool(is_fresh())
    return False


def _cached_poll_wait_seconds(relay_client: Any, relay_url: str, default: float) -> float:
    """Return the relay-advertised long-poll wait used by the active client."""

    hints_by_relay = getattr(relay_client, "_api_v1_relay_wait_hints", {})
    hints = hints_by_relay.get(relay_url, {}) if isinstance(hints_by_relay, dict) else {}
    wait_seconds = hints.get("poll_wait_seconds", default) if isinstance(hints, dict) else default
    if isinstance(wait_seconds, bool):
        return default
    try:
        normalised_wait = float(wait_seconds)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(normalised_wait) or normalised_wait < 0:
        return default
    return normalised_wait


def _api_v1_warm_load_wait_seconds(default: float = API_V1_WARM_LOAD_WAIT_DEFAULT_SECONDS) -> float:
    """Return bounded API v1 warm-load wait before fail-closed response submission."""

    raw_value = os.environ.get("TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS")
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _api_v1_recovery_attempts(default: int = RECOVERY_ATTEMPTS_DEFAULT) -> int:
    raw_value = os.environ.get("TOKENPLACE_DESKTOP_API_V1_RECOVERY_ATTEMPTS")
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return value


def _api_v1_recovery_backoff_seconds(
    default: float = RECOVERY_BACKOFF_DEFAULT_SECONDS,
) -> float:
    raw_value = os.environ.get("TOKENPLACE_DESKTOP_API_V1_RECOVERY_BACKOFF_SECONDS")
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _relay_error_message(relay_response: Dict[str, Any]) -> Optional[str]:
    """Return a normalized relay error message if the response includes one."""

    raw_error = relay_response.get("error")
    if raw_error is None:
        return None
    if isinstance(raw_error, str):
        normalized = raw_error.strip()
        return normalized or None
    if not raw_error:
        return None
    return str(raw_error)


def _sanitize_relay_target(relay_url: Any) -> str:
    """Return a redacted relay target that never includes userinfo/query/fragment."""

    if not isinstance(relay_url, str):
        return "unknown"

    try:
        parsed = urlsplit(relay_url.strip())
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError:
        return "unknown"

    if not parsed.scheme or not hostname:
        return "unknown"
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parsed_port}" if parsed_port is not None else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))


def _relay_key_fingerprint(relay_client: Any) -> str:
    """Return a safe short fingerprint for the compute-node relay key."""

    crypto_manager = getattr(relay_client, "crypto_manager", None)
    public_key = getattr(crypto_manager, "public_key_b64", None)
    if not isinstance(public_key, str) or not public_key:
        return "unknown"
    fingerprint = getattr(relay_client, "_api_v1_public_key_fingerprint", None)
    if callable(fingerprint):
        try:
            return str(fingerprint(public_key))
        except Exception:
            return "unknown"
    return f"{public_key[:8]}...{public_key[-4:]}" if len(public_key) > 12 else "unknown"


def _safe_poll_wait_seconds(relay_response: Dict[str, Any], default: float = 1) -> float:
    """Return a finite non-negative relay poll wait interval."""

    fallback = (
        default
        if isinstance(default, (int, float)) and not isinstance(default, bool)
        else 1
    )
    fallback = (
        float(fallback)
        if math.isfinite(float(fallback)) and float(fallback) >= 0
        else 1.0
    )

    if not isinstance(relay_response, dict):
        return fallback

    wait_seconds = relay_response.get("next_ping_in_x_seconds")
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
        return fallback

    wait_seconds = float(wait_seconds)
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        return fallback
    return wait_seconds


def _relay_response_summary(
    relay_response: Dict[str, Any], *, api_v1_payload: bool = False, wait_seconds: float = 1
) -> str:
    """Return a compact metadata-only summary for relay registration diagnostics."""

    if not isinstance(relay_response, dict):
        return f"non-dict response type={type(relay_response).__name__}"

    keys = sorted(relay_response.keys())
    has_heartbeat = "next_ping_in_x_seconds" in relay_response
    relay_error = _relay_error_message(relay_response)
    request_id = relay_response.get("request_id")
    safe_request_id = request_id if isinstance(request_id, str) and request_id else "none"
    error_kind = relay_response.get("relay_error_kind")
    http_status = relay_response.get("http_status")

    if error_kind == "cloudflare_pre_app_rejection":
        diagnostic = relay_response.get("relay_http_diagnostic")
        headers = diagnostic.get("headers", {}) if isinstance(diagnostic, dict) else {}
        return (
            "kind=cloudflare_pre_app_rejection "
            f"status={http_status or 'unknown'} cf_ray={headers.get('cf-ray', 'none')} "
            f"server={headers.get('server', 'none')} wait={wait_seconds} error={relay_error or 'none'}"
        )
    if error_kind == "relay_json_error":
        return (
            "kind=relay_json_error "
            f"status={http_status or 'unknown'} request_id={safe_request_id} "
            f"wait={wait_seconds} error={relay_response.get('relay_error') or relay_error or 'none'}"
        )
    if error_kind == "http_status_no_json_body":
        return (
            "kind=http_status_no_json_body "
            f"status={http_status or 'unknown'} request_id={safe_request_id} "
            f"wait={wait_seconds} error={relay_error or 'none'}"
        )
    if isinstance(relay_error, str) and "timed out" in relay_error.lower():
        return (
            "kind=request_timeout "
            f"request_id={safe_request_id} wait={wait_seconds} error={relay_error}"
        )

    return (
        f"keys={keys} api_v1_payload={api_v1_payload} "
        f"heartbeat={has_heartbeat} request_id={safe_request_id} "
        f"wait={wait_seconds} error={relay_error or 'none'}"
    )


def _runtime_diagnostics_summary(diagnostics: Dict[str, Any]) -> str:
    """Return a compact runtime diagnostics summary for stderr logging."""

    return (
        "desktop.compute_node_bridge.runtime_state "
        f"requested_mode={diagnostics.get('requested_mode')} "
        f"effective_mode={diagnostics.get('effective_mode')} "
        f"backend_selected={diagnostics.get('backend_selected')} "
        f"backend_used={diagnostics.get('backend_used')} "
        f"backend_available={diagnostics.get('backend_available')} "
        f"fallback_reason={diagnostics.get('fallback_reason') or 'none'} "
        f"offloaded_layers={diagnostics.get('offloaded_layers', diagnostics.get('n_gpu_layers'))} "
        f"kv_cache_device={diagnostics.get('kv_cache_device') or 'unknown'}"
    )


def _env_enabled(name: str, default: str = "0") -> bool:
    value = os.getenv(name, default)
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _runtime_path_from_env() -> str:
    value = os.getenv("TOKENPLACE_DESKTOP_RUNTIME_PATH", RUNTIME_PATH_DEFAULT)
    if value.strip().lower() == "sidecar":
        return "sidecar"
    return "bridge"


def _normalize_compute_mode_local(mode: Any) -> str:
    supported_modes = {"auto", "cpu", "gpu", "hybrid"}
    selected = (mode or "auto").strip().lower()
    normalized = {"cuda": "gpu", "metal": "gpu"}.get(selected, selected)
    return normalized if normalized in supported_modes else "auto"


def _start_stdin_reader() -> None:
    global _stdin_reader_started
    with _stdin_reader_lock:
        if _stdin_reader_started:
            return

        def _reader() -> None:
            while True:
                line = sys.stdin.readline()
                if line == "":
                    break
                _stdin_lines.put(line)

        threading.Thread(target=_reader, daemon=True).start()
        _stdin_reader_started = True


def stop_requested() -> bool:
    if _stop_requested_latched.is_set():
        return True

    _start_stdin_reader()
    while True:
        try:
            line = _stdin_lines.get_nowait().strip()
        except queue.Empty:
            return False
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "cancel":
            _stop_requested_latched.set()
            return True


def emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


_SAFE_READINESS_DIAGNOSTIC_KEYS = {
    "api_v1_readiness_result",
    "api_v1_readiness_error_code",
    "api_v1_readiness_error_reason",
    "api_v1_readiness_yarn_requested_context_tokens",
    "api_v1_readiness_yarn_original_context_tokens",
    "api_v1_readiness_yarn_context_multiplier",
    "api_v1_readiness_yarn_rope_freq_scale",
    "api_v1_readiness_yarn_ext_factor_overridden",
    "api_v1_readiness_yarn_rope_scaling_type_source",
    "api_v1_readiness_yarn_configuration_valid",
    "api_v1_readiness_completion_smoke_result",
    "api_v1_readiness_completion_smoke_failure_reason",
    "api_v1_readiness_completion_smoke_error_code",
    "api_v1_readiness_completion_smoke_safe_summary",
    "api_v1_readiness_completion_smoke_exception_category",
    "api_v1_readiness_completion_smoke_exception_type",
    "api_v1_readiness_completion_smoke_rejected_generation_kwarg",
    "api_v1_readiness_completion_smoke_rejected_option",
    "api_v1_readiness_completion_smoke_attempted_generation_kwargs",
    "api_v1_readiness_completion_smoke_attempted_plain_completion_methods",
    "api_v1_readiness_completion_smoke_method",
    "api_v1_readiness_completion_smoke_generation_exception_category",
    "api_v1_readiness_completion_smoke_result_shape",
    "api_v1_readiness_completion_smoke_plain_completion_create_completion_callable",
    "api_v1_readiness_completion_smoke_plain_completion_llama_call_callable",
    "api_v1_readiness_completion_smoke_plain_completion_signature_inspectable",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs",
    "api_v1_readiness_completion_smoke_plain_completion_reset_after_failure_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_token_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_methods",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_categories",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_exception_types",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_safe_summaries",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_rejected_kwargs",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_result_shapes",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_tokenization_variants",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_count",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_attempted",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_supported",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_succeeded",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_rejected_kwarg",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_category",
    "api_v1_readiness_completion_smoke_plain_completion_eval_return_code",
    "api_v1_readiness_completion_smoke_plain_completion_first_failure_method",
    "api_v1_readiness_completion_smoke_plain_completion_backend_failure_category",
    "api_v1_readiness_completion_smoke_plain_completion_backend_state_sticky",
    "api_v1_readiness_completion_smoke_plain_completion_backend_recreation_required",
    "api_v1_readiness_completion_smoke_plain_completion_metal_error_category",
    "api_v1_readiness_completion_smoke_plain_completion_metal_command_buffer_status",
    "api_v1_readiness_qwen_64k_runtime_profile_id",
    "api_v1_readiness_qwen_64k_runtime_profile_attempt_ids",
    "api_v1_readiness_qwen_64k_runtime_profile_recovery_count",
    "api_v1_readiness_qwen_64k_runtime_profile_flash_attn",
    "api_v1_readiness_qwen_64k_runtime_profile_offload_kqv",
    "api_v1_readiness_qwen_64k_runtime_profile_type_k",
    "api_v1_readiness_qwen_64k_runtime_profile_type_v",
    "api_v1_readiness_qwen_64k_runtime_profile_n_batch",
    "api_v1_readiness_qwen_64k_runtime_profile_n_ubatch",
    "api_v1_readiness_qwen_64k_runtime_profile_result",
    "api_v1_readiness_qwen_64k_runtime_profile_failure_category",

    "api_v1_readiness_completion_smoke_qwen_api_v1_non_thinking_template_fallback",
}
_SAFE_READINESS_DIAGNOSTIC_STRING_RE = re.compile(r"^[A-Za-z0-9_.:/@,+\-]{0,256}$")


def _safe_readiness_diagnostics(model_manager: Any) -> Dict[str, Any]:
    diagnostics = getattr(model_manager, "last_compute_diagnostics", None)
    if not isinstance(diagnostics, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key in _SAFE_READINESS_DIAGNOSTIC_KEYS:
        if key not in diagnostics:
            continue
        value = diagnostics.get(key)
        if isinstance(value, bool) or value is None:
            safe[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            safe[key] = value
        elif isinstance(value, str):
            bounded = value[:256]
            if _SAFE_READINESS_DIAGNOSTIC_STRING_RE.fullmatch(bounded):
                safe[key] = bounded
    return safe


def _emit_safe_readiness_diagnostics_stderr(model_manager: Any) -> None:
    diagnostics = _safe_readiness_diagnostics(model_manager)
    prefix = "desktop.compute_node_bridge.api_v1_readiness.safe_diagnostics"
    if not diagnostics:
        print(f"{prefix} unavailable=true", file=sys.stderr)
        return

    fields: List[str] = []
    for key in sorted(diagnostics):
        value = diagnostics[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif value is None:
            rendered = "null"
        else:
            rendered = str(value)
        fields.append(f"{key}={rendered}")
    print(f"{prefix} {' '.join(fields)}", file=sys.stderr)

def _relay_runtime_state(
    warm_load_state: str, *, running: bool, warm_load_enabled: bool = True
) -> str:
    if warm_load_state == "failed":
        return "failed"
    if not running:
        return "stopped"
    if not warm_load_enabled:
        return "ready"
    if warm_load_state == "not_started":
        return "starting"
    return warm_load_state


def _status_diagnostics(diagnostics: Dict[str, Any], relay_runtime_state: str) -> Dict[str, Any]:
    if relay_runtime_state != "ready":
        return {
            "requested_mode": diagnostics.get("requested_mode"),
            "effective_mode": "pending",
            "backend_available": "pending",
            "backend_selected": "pending",
            "backend_used": "pending",
            "offloaded_layers": diagnostics.get("offloaded_layers", diagnostics.get("n_gpu_layers")),
            "kv_cache_device": diagnostics.get("kv_cache_device"),
            "fallback_reason": None,
        }
    return diagnostics


def _bridge_session_id_from_env() -> str:
    value = os.getenv("TOKENPLACE_COMPUTE_NODE_SESSION_ID", "").strip()
    return value or uuid.uuid4().hex


def _ensure_desktop_llama_runtime_for_context(
    mode: str,
    context_tier: str,
    *,
    cancellation_predicate: Optional[Any] = None,
    heartbeat: Optional[Any] = None,
) -> Dict[str, str]:
    try:
        parameters = inspect.signature(ensure_desktop_llama_runtime).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: Dict[str, Any] = {}
    accepts_var_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if "context_tier" in parameters or accepts_var_kwargs:
        kwargs["context_tier"] = context_tier
    if "cancellation_predicate" in parameters or accepts_var_kwargs:
        kwargs["cancellation_predicate"] = cancellation_predicate
    if "heartbeat" in parameters or accepts_var_kwargs:
        kwargs["heartbeat"] = heartbeat
    return ensure_desktop_llama_runtime(mode, **kwargs)

def _startup_context_tier(args: argparse.Namespace) -> str:
    raw_context_tier = getattr(args, "context_tier", "8k-fast")
    if raw_context_tier in {"8k-fast", "64k-full"}:
        return raw_context_tier
    return "8k-fast"


def _load_context_profile_helpers() -> Tuple[Any, Any]:
    """Import context-profile helpers after baseline dependency preflight."""

    from utils.context_profiles import apply_context_profile, normalize_context_tier

    return apply_context_profile, normalize_context_tier


def _sanitize_context_profile_import_error(exc: BaseException) -> str:
    """Return bounded, non-sensitive context-profile import failure details."""

    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    detail = re.sub(
        r"(?i)\b(model_path|prompt|ciphertext|decrypted|key)\s*=\s*\S+",
        "<redacted>",
        detail,
    )
    if len(detail) > 240:
        detail = f"{detail[:237]}..."
    return detail


def _structured_provisioning_payload(args: argparse.Namespace, *, phase: str, started_at: float) -> Dict[str, Any]:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "type": "started",
        "running": True,
        "registered": False,
        "registered_relay_count": 0,
        "registered_relay_urls": [],
        "active_relay_urls": [],
        "relay_runtime_state": "provisioning",
        "runtime_provisioning_state": "provisioning",
        "startup_phase": phase,
        "startup_elapsed_ms": elapsed_ms,
        "startup_deadline_ms": None,
        "active_relay_url": _normalize_relay_urls(getattr(args, "relay_url", "https://token.place"), getattr(args, "relay_urls", None))[0],
        "requested_mode": _normalize_compute_mode_local(getattr(args, "mode", "auto")),
        "effective_mode": "pending",
        "backend_available": "pending",
        "backend_selected": "pending",
        "backend_used": "pending",
        "fallback_reason": None,
        "runtime_action": "provisioning",
        "offloaded_layers": 0,
        "kv_cache_device": "cpu",
        "context_tier": _startup_context_tier(args),
        "model_path": str(getattr(args, "model", "")),
        "log_file_path": os.environ.get("TOKENPLACE_OPERATOR_LOG_FILE", "unknown") or "unknown",
        "last_error": None,
        "warm_load_state": "provisioning",
        "warm_load_enabled": _env_enabled("TOKENPLACE_DESKTOP_WARM_LOAD", WARM_LOAD_DEFAULT),
        "warm_load_duration_ms": None,
        "runtime_path": _runtime_path_from_env(),
        "relay_runtime_path": "bridge",
        "worker_state": "provisioning",
        "worker_generation": 0,
        "worker_restart_count": 0,
        "worker_alive": False,
        "use_mock_llm": False,
        "llama_repo_stub_imported": False,
        "last_worker_error_code": None,
        "last_worker_exit_code": None,
        "last_worker_restart_at_ms": None,
        "readiness_diagnostics": {
            "runtime_provisioning_state": "provisioning",
            "startup_phase": phase,
            "startup_elapsed_ms": elapsed_ms,
        },
    }

def _structured_startup_error_payload(
    args: argparse.Namespace,
    message: str,
    *,
    operator_session_id: Optional[str] = None,
    sequence: Optional[int] = None,
    updated_at_ms: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "type": "error",
        "running": False,
        "registered": False,
        "relay_runtime_state": "failed",
        "active_relay_url": _normalize_relay_urls(
            getattr(args, "relay_url", "https://token.place"),
            getattr(args, "relay_urls", None),
        )[0],
        "requested_mode": _normalize_compute_mode_local(getattr(args, "mode", "auto")),
        "effective_mode": "pending",
        "backend_available": "pending",
        "backend_selected": "pending",
        "backend_used": "pending",
        "fallback_reason": None,
        "error_code": getattr(args, "startup_error_code", "desktop_compute_node_startup_failed"),
        "context_tier": _startup_context_tier(args),
        "interpreter": sys.executable,
        "import_root": os.environ.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT", "unknown") or "unknown",
        "log_file_path": os.environ.get("TOKENPLACE_OPERATOR_LOG_FILE", "unknown") or "unknown",
        "last_error": message,
        "message": message,
        "warm_load_state": "failed",
        "warm_load_enabled": _env_enabled("TOKENPLACE_DESKTOP_WARM_LOAD", WARM_LOAD_DEFAULT),
        "warm_load_duration_ms": None,
        "runtime_path": _runtime_path_from_env(),
        "relay_runtime_path": "bridge",
    }
    if operator_session_id is not None:
        payload["operator_session_id"] = operator_session_id
    if sequence is not None:
        payload["sequence"] = sequence
    if updated_at_ms is not None:
        payload["updated_at_ms"] = updated_at_ms
    return payload


def _sleep_with_cancel(seconds: float) -> bool:
    deadline = time.time() + max(seconds, 0)
    while time.time() < deadline:
        if stop_requested():
            return True
        time.sleep(0.1)
    return stop_requested()


def _expand_relay_url_candidate(candidate: Any) -> List[str]:
    """Expand one CLI relay-url value without logging secrets or payloads."""

    if not isinstance(candidate, str):
        return []
    trimmed = candidate.strip()
    if not trimmed:
        return []
    if trimmed.startswith("["):
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str)]
    if "," in trimmed:
        return [part for part in (item.strip() for item in trimmed.split(",")) if part]
    return [trimmed]


def _normalize_relay_urls(*raw_relay_url_groups: Any) -> List[str]:
    """Return a trimmed, de-duplicated relay list from argparse or tests."""

    candidates: List[Any] = []
    for raw_relay_urls in raw_relay_url_groups:
        if raw_relay_urls is None:
            continue
        if isinstance(raw_relay_urls, str):
            candidates.extend(_expand_relay_url_candidate(raw_relay_urls))
        elif isinstance(raw_relay_urls, (list, tuple)):
            for candidate in raw_relay_urls:
                candidates.extend(_expand_relay_url_candidate(candidate))

    normalized: List[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        trimmed = candidate.strip()
        if trimmed and trimmed not in normalized:
            normalized.append(trimmed)

    return normalized or ["https://token.place"]

def run(args: argparse.Namespace) -> int:
    bridge_session_id = _bridge_session_id_from_env()
    _reset_bridge_lifecycle_state(bridge_session_id)
    startup_started_at = time.monotonic()
    inherited_sequence = os.environ.get("TOKENPLACE_OPERATOR_EVENT_SEQUENCE", "0") if os.environ.get("TOKENPLACE_COMPUTE_NODE_SESSION_ID") else "0"
    try:
        status_sequence = int(inherited_sequence or "0")
    except (TypeError, ValueError):
        status_sequence = 0
    emit_lock = threading.Lock()

    def emit_operator_event(payload: Dict[str, Any]) -> None:
        nonlocal status_sequence
        with emit_lock:
            status_sequence += 1
            payload = dict(payload)
            payload.setdefault("operator_session_id", bridge_session_id)
            payload.setdefault("sequence", status_sequence)
            os.environ["TOKENPLACE_OPERATOR_EVENT_SEQUENCE"] = str(status_sequence)
            payload.setdefault("updated_at_ms", int(time.time() * 1000))
            emit(payload)

    def emit_startup_error(message: str) -> None:
        emit_operator_event(_structured_startup_error_payload(args, message))

    def emit_provisioning(phase: str, extra: Optional[Dict[str, Any]] = None) -> None:
        safe_extra: Dict[str, Any] = {}
        if extra:
            safe_extra = {k: v for k, v in extra.items() if k in {"startup_elapsed_ms", "startup_deadline_ms", "runtime_provisioning_state", "startup_phase"}}
            phase = str(safe_extra.get("startup_phase") or phase)
        payload = _structured_provisioning_payload(args, phase=phase, started_at=startup_started_at)
        if safe_extra:
            payload.update(safe_extra)
            payload.setdefault("readiness_diagnostics", {}).update(safe_extra)
        emit_operator_event(payload)

    original_model_arg = str(args.model)
    model_path_was_relative = not os.path.isabs(original_model_arg)
    if model_path_was_relative:
        args.model = os.path.abspath(original_model_arg)
    parent_model_path_exists = os.path.exists(args.model)

    emit_provisioning("dependency_check")
    try:
        dependency_setup = ensure_desktop_python_dependencies(cancellation_predicate=stop_requested, heartbeat=lambda extra: emit_provisioning("dependency_install", extra))
    except TypeError as exc:
        if "unexpected keyword argument" in str(exc):
            dependency_setup = ensure_desktop_python_dependencies()
        else:
            raise
    if dependency_setup.get("ok") != "true":
        missing = dependency_setup.get("missing") or "unknown"
        detail = dependency_setup.get("detail") or dependency_setup.get("action") or "dependency bootstrap failed"
        emit_startup_error(
            "desktop runtime dependency preflight failed "
            f"(interpreter={dependency_setup.get('interpreter', sys.executable)} "
            f"import_root={dependency_setup.get('import_root', 'unknown')} "
            f"missing={missing}): {detail}"
        )
        return 1

    emit_provisioning("runtime_probe")
    try:
        runtime_setup = _ensure_desktop_llama_runtime_for_context(
            args.mode,
            _startup_context_tier(args),
            cancellation_predicate=stop_requested,
            heartbeat=lambda extra: emit_provisioning("runtime_install", extra),
        )
    except TypeError as exc:
        if "unexpected keyword argument" in str(exc):
            runtime_setup = _ensure_desktop_llama_runtime_for_context(args.mode, _startup_context_tier(args))
        else:
            raise
    emit_provisioning("runtime_verification")
    if runtime_setup.get("runtime_action") in {
        "installed_cuda_reexec",
        "installed_metal_reexec",
        "installed_gpu_reexec",
    }:
        emit_provisioning("reexec")
    maybe_reexec_for_runtime_refresh(runtime_setup)
    print(
        "desktop.runtime_setup "
        f"mode={args.mode} "
        f"selected_backend={runtime_setup.get('selected_backend', 'cpu')} "
        f"device={runtime_setup.get('detected_device', 'cpu')} "
        f"action={runtime_setup.get('runtime_action', 'none')} "
        f"llama_cpp_python_version={runtime_setup.get('llama_cpp_python_version', 'unknown')} "
        f"llama_cpp_python_installed_version={runtime_setup.get('llama_cpp_python_installed_version', 'unknown')} "
        f"llama_cpp_python_required_version={runtime_setup.get('llama_cpp_python_required_version', 'unknown')} "
        f"llama_cpp_python_version_match={runtime_setup.get('llama_cpp_python_version_match', 'unknown')} "
        f"interpreter={runtime_setup.get('interpreter', sys.executable)} "
        f"python_version={runtime_setup.get('python_version', 'unknown')} "
        f"prefix={runtime_setup.get('prefix', runtime_setup.get('interpreter_prefix', 'unknown'))} "
        f"base_prefix={runtime_setup.get('base_prefix', 'unknown')} "
        f"dependency_target={runtime_setup.get('dependency_target', 'unknown')} "
        f"pip={runtime_setup.get('pip_version', 'unknown')} "
        f"install_command={runtime_setup.get('install_command_summary', 'none')} "
        f"install_backend={runtime_setup.get('install_backend', 'none')} "
        f"cmake_args={runtime_setup.get('cmake_args', 'none')} "
        f"pip_stdout_tail={runtime_setup.get('pip_stdout_tail', 'none')} "
        f"pip_stderr_tail={runtime_setup.get('pip_stderr_tail', 'none')} "
        f"fallback_reason={runtime_setup.get('fallback_reason') or 'none'}",
        file=sys.stderr,
    )
    repo_llama_cpp_shim_imported = runtime_setup.get("runtime_action") == "shadowed_repo_llama_cpp"
    print(
        "desktop.runtime_setup.import_guard "
        f"repo_llama_cpp_shim_imported={repo_llama_cpp_shim_imported}",
        file=sys.stderr,
    )

    gpu_runtime_error = desktop_gpu_runtime_failure_message(args.mode, runtime_setup)
    if gpu_runtime_error:
        emit_startup_error(gpu_runtime_error)
        return 1

    try:
        apply_context_profile, normalize_context_tier = _load_context_profile_helpers()
    except Exception as exc:
        setattr(args, "startup_error_code", "context_profiles_unavailable")
        emit_startup_error(
            "context profiles unavailable: "
            f"{_sanitize_context_profile_import_error(exc)}"
        )
        return 1

    try:
        from utils.compute_node_runtime import (
            apply_compute_mode,
            compute_mode_diagnostics,
            ComputeNodeRuntime,
            ComputeNodeRuntimeConfig,
            is_api_v1_relay_payload,
            resolve_relay_port,
            resolve_relay_url,
        )
    except ModuleNotFoundError as exc:
        emit_startup_error(f"runtime unavailable: {exc}")
        return 1

    args.context_tier = normalize_context_tier(getattr(args, "context_tier", "8k-fast"))

    relay_urls = _normalize_relay_urls(
        getattr(args, "relay_url", None),
        getattr(args, "relay_urls", None),
    )
    relay_url = resolve_relay_url(relay_urls[0], prefer_cli=True)
    relay_urls = [relay_url, *relay_urls[1:]]
    relay_port = resolve_relay_port(args.relay_port, relay_url)
    print(
        "desktop.compute_node_bridge.start "
        f"operator_session_id={bridge_session_id} "
        f"model_path_was_relative={model_path_was_relative} parent_model_path_exists={parent_model_path_exists} "
        f"mode={args.mode} context_tier={args.context_tier} "
        f"relay_count={len(relay_urls)} "
        f"relay_url={_sanitize_relay_target(relay_url)} "
        f"relay_port={relay_port if relay_port is not None else 'none'}",
        file=sys.stderr,
    )
    for configured_relay_url in relay_urls:
        print(
            "desktop.compute_node_bridge.relay_target.resolved "
            f"relay={_sanitize_relay_target(configured_relay_url)}",
            file=sys.stderr,
        )

    def make_runtime(target_relay_url: str, *, shared_runtime: Optional[Any] = None) -> Any:
        target_relay_port = resolve_relay_port(args.relay_port, target_relay_url)
        config = ComputeNodeRuntimeConfig(
            relay_url=target_relay_url,
            relay_port=target_relay_port,
            use_configured_relay_fallbacks=False,
            relay_urls=(target_relay_url,),
        )
        if shared_runtime is None:
            try:
                return ComputeNodeRuntime(config, cancellation_predicate=stop_requested)
            except TypeError:
                return ComputeNodeRuntime(config)
        try:
            return ComputeNodeRuntime(
                config,
                crypto_manager=getattr(shared_runtime, "crypto_manager", None),
                model_manager=getattr(shared_runtime, "model_manager", None),
                cancellation_predicate=stop_requested,
            )
        except TypeError:
            relay_runtime = ComputeNodeRuntime(config)
            if hasattr(shared_runtime, "model_manager"):
                relay_runtime.model_manager = shared_runtime.model_manager
            if hasattr(shared_runtime, "crypto_manager"):
                relay_runtime.crypto_manager = shared_runtime.crypto_manager
            return relay_runtime

    emit_provisioning("model_preflight")
    runtime = make_runtime(relay_url)
    runtimes = [runtime] + [make_runtime(url, shared_runtime=runtime) for url in relay_urls[1:]]
    for relay_runtime in runtimes:
        start_relay_session = getattr(relay_runtime, "start_relay_session", None)
        if callable(start_relay_session):
            start_relay_session()
        else:
            relay_start = getattr(getattr(relay_runtime, "relay_client", None), "start", None)
            if callable(relay_start):
                relay_start()
        print(
            "desktop.compute_node_bridge.relay_client.reset "
            f"operator_session_id={bridge_session_id} "
            f"relay={_sanitize_relay_target(relay_runtime.relay_client.relay_url)} "
            f"key_fingerprint={_relay_key_fingerprint(relay_runtime.relay_client)}",
            file=sys.stderr,
        )

    runtime.model_manager.model_path = args.model
    runtime.model_manager.parent_model_path_exists = parent_model_path_exists
    runtime.model_manager.model_path_was_relative = model_path_was_relative
    context_profile = apply_context_profile(runtime.model_manager, args.context_tier)
    apply_compute_mode(runtime.model_manager, args.mode)
    try:
        private_runtime_setup = dict(runtime_setup)
        raw_private_probe = os.environ.get("TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON", "").strip()
        if raw_private_probe:
            try:
                parsed_private_probe = json.loads(raw_private_probe)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_private_probe = None
            if isinstance(parsed_private_probe, dict):
                identity = parsed_private_probe.get("llama_module_identity")
                if isinstance(identity, str):
                    private_runtime_setup["llama_module_identity"] = identity
        runtime.model_manager.desktop_runtime_probe = private_runtime_setup
    except Exception:
        pass

    warm_load_enabled = _env_enabled("TOKENPLACE_DESKTOP_WARM_LOAD", WARM_LOAD_DEFAULT)
    runtime_path = _runtime_path_from_env()
    dual_runtime_enabled = _env_enabled("TOKENPLACE_DESKTOP_DUAL_RUNTIME", "0")
    relay_runtime_path = "bridge"
    warm_load_state = "not_started"
    warm_load_started_at = 0.0
    warm_load_duration_ms: Optional[int] = None
    warm_load_failed: Optional[str] = None
    warm_load_fatal = False
    warm_load_future: Optional[_DaemonWarmLoadFuture] = None
    relay_status_lock = threading.Lock()
    configured_relay_urls = [relay_runtime.relay_client.relay_url for relay_runtime in runtimes]
    relay_status_map: Dict[str, Dict[str, Any]] = {
        canonical_relay_url: {
            "relay_url": canonical_relay_url,
            "registered": False,
            "relay_runtime_state": "starting",
            "last_error": None,
            "last_request_id": None,
            "request_count": 0,
        }
        for canonical_relay_url in configured_relay_urls
    }
    inference_lock = threading.Lock()
    poll_failure_fatal = False

    def update_relay_status(relay_url_value: str, **updates: Any) -> None:
        with relay_status_lock:
            relay_status = relay_status_map.setdefault(
                relay_url_value,
                {
                    "relay_url": relay_url_value,
                    "registered": False,
                    "relay_runtime_state": "starting",
                    "last_error": None,
                    "last_request_id": None,
                    "request_count": 0,
                },
            )
            relay_status.update(updates)

    def increment_relay_request_count(relay_url_value: str) -> int:
        with relay_status_lock:
            relay_status = relay_status_map.setdefault(
                relay_url_value,
                {
                    "relay_url": relay_url_value,
                    "registered": False,
                    "relay_runtime_state": "starting",
                    "last_error": None,
                    "last_request_id": None,
                    "request_count": 0,
                },
            )
            request_count = int(relay_status.get("request_count") or 0) + 1
            relay_status["request_count"] = request_count
            return request_count

    def relay_status_snapshot() -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
        with relay_status_lock:
            statuses = [
                dict(relay_status_map[url])
                for url in configured_relay_urls
                if url in relay_status_map
            ]
        registered_count = sum(1 for status in statuses if status.get("registered") is True)
        errors = [status for status in statuses if status.get("last_error")]
        if len(errors) == 1 and len(statuses) == 1:
            error_summary = str(errors[0].get("last_error"))
        else:
            error_summary = "; ".join(
                f"{_sanitize_relay_target(status.get('relay_url'))}: {status.get('last_error')}"
                for status in errors
            )
        return statuses, registered_count, error_summary or None

    def worker_lifecycle_status() -> Dict[str, Any]:
        status_fn = getattr(runtime.model_manager, "worker_lifecycle_status", None)
        if callable(status_fn):
            try:
                status = status_fn()
                if isinstance(status, dict):
                    return dict(status)
            except Exception:
                pass
        return {
            "worker_state": "ready" if warm_load_state == "ready" else ("recovering" if warm_load_state == "recovering" else ("failed" if warm_load_state == "failed" else "starting")),
            "worker_generation": None,
            "worker_restart_count": None,
            "worker_alive": warm_load_state == "ready",
            "last_worker_error_code": None,
            "last_worker_exit_code": None,
            "last_worker_restart_at_ms": None,
        }

    def build_status_payload(
        *,
        event_type: str,
        running: bool,
        registered: bool,
        active_relay_url: str,
        current_last_error: Optional[str],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        relay_state = _relay_runtime_state(
            warm_load_state, running=running, warm_load_enabled=warm_load_enabled
        )
        payload_relay_state = (extra or {}).get("relay_runtime_state", relay_state)
        statuses, registered_count, relay_errors = relay_status_snapshot()
        fresh_registered = (
            running
            and payload_relay_state in {"ready", "processing"}
            and registered_count > 0
        )
        diagnostics = _status_diagnostics(compute_mode_diagnostics(runtime.model_manager), relay_state)
        configured_count = len(configured_relay_urls)
        registered_relay_urls = [
            status["relay_url"] for status in statuses if status.get("registered") is True
        ]
        payload: Dict[str, Any] = {
            "type": event_type,
            "running": running,
            "registered": fresh_registered,
            "relay_runtime_state": relay_state,
            "active_relay_url": active_relay_url,
            "configured_relay_urls": list(configured_relay_urls),
            "relay_statuses": statuses,
            "registered_relay_count": registered_count,
            "configured_relay_count": configured_count,
            "registered_relay_urls": registered_relay_urls,
            "active_relay_urls": registered_relay_urls,
            "requested_mode": diagnostics.get("requested_mode"),
            "effective_mode": diagnostics.get("effective_mode"),
            "backend_available": diagnostics.get("backend_available"),
            "backend_selected": diagnostics.get("backend_selected"),
            "backend_used": diagnostics.get("backend_used"),
            "offloaded_layers": diagnostics.get("offloaded_layers", diagnostics.get("n_gpu_layers")),
            "kv_cache_device": diagnostics.get("kv_cache_device"),
            "fallback_reason": diagnostics.get("fallback_reason"),
            "interpreter": runtime_setup.get("interpreter", sys.executable),
            "dependency_target": runtime_setup.get("dependency_target", "unknown"),
            "python_version": runtime_setup.get("python_version", "unknown"),
            "prefix": runtime_setup.get("prefix", runtime_setup.get("interpreter_prefix", "unknown")),
            "base_prefix": runtime_setup.get("base_prefix", "unknown"),
            "pip_version": runtime_setup.get("pip_version", "unknown"),
            "runtime_action": runtime_setup.get("runtime_action", "none"),
            "runtime_selected_backend": runtime_setup.get("selected_backend", "cpu"),
            "install_command_summary": runtime_setup.get("install_command_summary"),
            "install_backend": runtime_setup.get("install_backend"),
            "cmake_args": runtime_setup.get("cmake_args"),
            "pip_stdout_tail": runtime_setup.get("pip_stdout_tail"),
            "pip_stderr_tail": runtime_setup.get("pip_stderr_tail"),
            "model_path_was_relative": model_path_was_relative,
            "parent_model_path_exists": parent_model_path_exists,
            "child_model_path_exists": getattr(runtime.model_manager, "child_model_path_exists", False),
            "context_tier": context_profile.profile_id,
            "context_window_tokens": context_profile.total_context_tokens,
            "last_error": relay_errors or current_last_error,
            "warm_load_state": warm_load_state,
            "warm_load_enabled": warm_load_enabled,
            "warm_load_duration_ms": warm_load_duration_ms,
            "runtime_path": runtime_path,
            "relay_runtime_path": relay_runtime_path,
        }
        payload.update(_safe_readiness_diagnostics(runtime.model_manager))
        payload.update(worker_lifecycle_status())
        if extra:
            payload.update(extra)
        return payload

    def emit_status_event(*, registered: bool, active_relay_url: str, current_last_error: Optional[str]) -> None:
        emit_operator_event(
            build_status_payload(
                event_type="status",
                running=True,
                registered=registered,
                active_relay_url=active_relay_url,
                current_last_error=current_last_error,
            )
        )

    def submit_api_v1_error_response(
        relay_response: Dict[str, Any],
        *,
        code: str,
        message: str,
        active_relay_url: str,
        request_id: str,
        relay_runtime: Optional[Any] = None,
    ) -> bool:
        response_runtime = relay_runtime or runtime
        submit_error = getattr(response_runtime, "submit_api_v1_error_response", None)
        if not callable(submit_error):
            relay_client = getattr(response_runtime, "relay_client", None)
            submit_error = getattr(relay_client, "submit_api_v1_error_response", None)
        if not callable(submit_error):
            print(
                "desktop.compute_node_bridge.api_v1_e2ee.error_response.unavailable "
                f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
                f"code={code}",
                file=sys.stderr,
            )
            return False
        submitted = bool(submit_error(relay_response, code=code, message=message))
        print(
            "desktop.compute_node_bridge.api_v1_e2ee.error_response.submitted "
            f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
            f"code={code} submitted={submitted}",
            file=sys.stderr,
        )
        return submitted

    def ensure_runtime_ready(
        reason: str,
        *,
        active_relay_url: str,
        block: bool = False,
        request_id: str = "none",
        block_timeout_seconds: Optional[float] = None,
    ) -> bool:
        nonlocal warm_load_state, warm_load_started_at, warm_load_duration_ms, warm_load_failed
        nonlocal warm_load_future
        if warm_load_state == "ready":
            return True
        if warm_load_state == "failed":
            return False
        if warm_load_state == "not_started":
            warm_load_state = "warming"
            warm_load_failed = None
            warm_load_duration_ms = None
            warm_load_started_at = time.perf_counter()
            warm_load_future = _DaemonWarmLoadFuture(runtime.ensure_api_v1_runtime_ready)
            emit_status_event(
                registered=False,
                active_relay_url=active_relay_url,
                current_last_error=last_error,
            )
            print(
                "desktop.compute_node_bridge.model_init.start "
                f"reason={reason} relay={_sanitize_relay_target(active_relay_url)} "
                f"request_id={request_id} state={warm_load_state}",
                file=sys.stderr,
            )
        if warm_load_future is None:
            return False
        if block and not warm_load_future.done():
            timeout = block_timeout_seconds
            try:
                ready = bool(warm_load_future.result(timeout=timeout))
            except concurrent.futures.TimeoutError:
                warm_load_duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
                return False
            except Exception as exc:
                ready = False
                warm_load_state = "failed"
                warm_load_failed = (
                    getattr(runtime.model_manager, 'last_runtime_init_error', None)
                    or "failed to initialize API v1 model runtime"
                )
                warm_load_duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
                print(
                    "desktop.compute_node_bridge.api_v1_e2ee.runtime_wait.exception "
                    f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
                    f"state={warm_load_state} duration_ms={warm_load_duration_ms} "
                    f"exc_type={type(exc).__name__}",
                    file=sys.stderr,
                )
        elif warm_load_future.done():
            try:
                ready = bool(warm_load_future.result())
            except Exception as exc:
                ready = False
                warm_load_state = "failed"
                warm_load_failed = (
                    getattr(runtime.model_manager, 'last_runtime_init_error', None)
                    or "failed to initialize API v1 model runtime"
                )
                warm_load_duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
                print(
                    "desktop.compute_node_bridge.model_init.exception "
                    f"reason={reason} relay={_sanitize_relay_target(active_relay_url)} "
                    f"request_id={request_id} state={warm_load_state} "
                    f"duration_ms={warm_load_duration_ms} exc_type={type(exc).__name__}",
                    file=sys.stderr,
                )
        else:
            return False
        warm_load_duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
        if not ready:
            warm_load_state = "failed"
            warm_load_failed = (
                getattr(runtime.model_manager, 'last_runtime_init_error', None)
                or "failed to initialize API v1 model runtime"
            )
            print(
                "desktop.compute_node_bridge.model_init.failed "
                f"reason={reason} relay={_sanitize_relay_target(active_relay_url)} "
                f"request_id={request_id} state={warm_load_state} "
                f"duration_ms={warm_load_duration_ms}",
                file=sys.stderr,
            )
            if block:
                print(
                    "desktop.compute_node_bridge.api_v1_e2ee.runtime_wait.failed "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"request_id={request_id} state={warm_load_state}",
                    file=sys.stderr,
                )
            return False
        warm_load_state = "ready"
        print(
            "desktop.compute_node_bridge.model_init.ready "
            f"reason={reason} relay={_sanitize_relay_target(active_relay_url)} "
            f"request_id={request_id} state={warm_load_state} "
            f"duration_ms={warm_load_duration_ms}",
            file=sys.stderr,
        )
        if block:
            print(
                "desktop.compute_node_bridge.api_v1_e2ee.runtime_wait.ready "
                f"relay={_sanitize_relay_target(active_relay_url)} "
                f"request_id={request_id} state={warm_load_state}",
                file=sys.stderr,
            )
        print(_runtime_diagnostics_summary(compute_mode_diagnostics(runtime.model_manager)), file=sys.stderr)
        return True

    def fail_on_warm_load_error(*, active_relay_url: str) -> None:
        nonlocal last_error, warm_load_fatal
        last_error = warm_load_failed or "failed to initialize API v1 model runtime"
        _emit_safe_readiness_diagnostics_stderr(runtime.model_manager)
        emit_status_event(
            registered=False,
            active_relay_url=active_relay_url,
            current_last_error=last_error,
        )
        emit_operator_event(
            build_status_payload(
                event_type="error",
                running=False,
                registered=False,
                active_relay_url=runtime.relay_client.relay_url,
                current_last_error=last_error,
                extra={"message": last_error},
            )
        )
        warm_load_fatal = True

    last_error: Optional[str] = None
    emit_operator_event(
        build_status_payload(
            event_type="started",
            running=True,
            registered=False,
            active_relay_url=runtime.relay_client.relay_url,
            current_last_error=None,
            extra={
                "llama_repo_stub_imported": repo_llama_cpp_shim_imported,
                "use_mock_llm": bool(getattr(runtime.model_manager, "use_mock_llm", False)),
            },
        )
    )
    if runtime_path == "sidecar":
        print(
            "desktop.compute_node_bridge.runtime_path.relay_uses_bridge "
            f"runtime_path={runtime_path} relay_runtime_path={relay_runtime_path} "
            f"dual_runtime_enabled={dual_runtime_enabled}",
            file=sys.stderr,
        )

    def warm_runtime_before_registration() -> bool:
        nonlocal warm_load_duration_ms, warm_load_failed, warm_load_state
        nonlocal last_error
        if not warm_load_enabled:
            print(
                "desktop.compute_node_bridge.model_init.skipped "
                "reason=warm_load_disabled relay_runtime_path=bridge",
                file=sys.stderr,
            )
            return True
        warm_load_deadline_seconds = _api_v1_warm_load_wait_seconds()
        print(
            "desktop.compute_node_bridge.registration.gate_wait_start "
            f"relay={_sanitize_relay_target(runtime.relay_client.relay_url)} "
            f"runtime_path={runtime_path} relay_runtime_path={relay_runtime_path} "
            f"timeout_seconds={warm_load_deadline_seconds}",
            file=sys.stderr,
        )
        ensure_runtime_ready(
            "pre_registration",
            active_relay_url=runtime.relay_client.relay_url,
            block=False,
        )
        wait_started_at = time.monotonic()
        last_progress_log_at = wait_started_at
        last_status_emit_at = wait_started_at
        progress_emit_interval_seconds = PRE_REGISTRATION_PROGRESS_INTERVAL_SECONDS
        status_emit_interval_seconds = PRE_REGISTRATION_STATUS_INTERVAL_SECONDS
        while warm_load_state == "warming":
            elapsed_seconds = time.monotonic() - wait_started_at
            remaining_seconds = warm_load_deadline_seconds - elapsed_seconds
            if remaining_seconds <= 0:
                warm_load_state = "failed"
                current_runtime_error = getattr(
                    runtime.model_manager, 'last_runtime_init_error', None
                )
                warm_load_failed = (
                    current_runtime_error
                    or "API v1 relay runtime warm-load timed out after "
                    f"{warm_load_deadline_seconds:g}s"
                )
                warm_load_duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
                last_error = warm_load_failed
                # Do not cancel the warm-load Future here: the underlying daemon
                # thread cannot be forcibly stopped, and cancelling the Future can
                # race with the worker setting its result/exception.  The failed
                # warm-load state above makes the bridge ignore any late completion.
                print(
                    "desktop.compute_node_bridge.registration.gate_wait_timeout "
                    f"relay={_sanitize_relay_target(runtime.relay_client.relay_url)} "
                    f"state={warm_load_state} duration_ms={warm_load_duration_ms} "
                    f"timeout_seconds={warm_load_deadline_seconds}",
                    file=sys.stderr,
                )
                emit_operator_event(
                    build_status_payload(
                        event_type="status",
                        running=True,
                        registered=False,
                        active_relay_url=runtime.relay_client.relay_url,
                        current_last_error=last_error,
                        extra={
                            "runtime_provisioning_state": "provisioning",
                            "startup_phase": "warm_load",
                            "startup_elapsed_ms": warm_load_duration_ms,
                            "startup_deadline_ms": int(warm_load_deadline_seconds * 1000),
                        },
                    )
                )
                fail_on_warm_load_error(active_relay_url=runtime.relay_client.relay_url)
                return False
            if ensure_runtime_ready(
                "pre_registration",
                active_relay_url=runtime.relay_client.relay_url,
                block=True,
                block_timeout_seconds=min(0.1, remaining_seconds),
            ):
                break
            now = time.monotonic()
            duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
            if now - last_progress_log_at >= progress_emit_interval_seconds:
                last_progress_log_at = now
                print(
                    "desktop.compute_node_bridge.model_init.still_warming "
                    f"reason=pre_registration relay={_sanitize_relay_target(runtime.relay_client.relay_url)} "
                    f"state={warm_load_state} duration_ms={duration_ms} "
                    f"timeout_seconds={warm_load_deadline_seconds}",
                    file=sys.stderr,
                )
            if now - last_status_emit_at >= status_emit_interval_seconds:
                last_status_emit_at = now
                emit_operator_event(
                    build_status_payload(
                        event_type="status",
                        running=True,
                        registered=False,
                        active_relay_url=runtime.relay_client.relay_url,
                        current_last_error=last_error,
                        extra={
                            "runtime_provisioning_state": "provisioning",
                            "startup_phase": "warm_load",
                            "startup_elapsed_ms": duration_ms,
                            "startup_deadline_ms": int(warm_load_deadline_seconds * 1000),
                        },
                    )
                )
            if stop_requested():
                return False
            time.sleep(0.01)
        if warm_load_state == "failed":
            fail_on_warm_load_error(active_relay_url=runtime.relay_client.relay_url)
            return False
        ready = warm_load_state == "ready"
        print(
            "desktop.compute_node_bridge.registration.gate_wait_done "
            f"relay={_sanitize_relay_target(runtime.relay_client.relay_url)} "
            f"state={warm_load_state} ready={ready}",
            file=sys.stderr,
        )
        return ready

    relay_poll_workers = {
        relay_runtime.relay_client.relay_url: _CancelablePollWorker(
            operator_session_id=bridge_session_id
        )
        for relay_runtime in runtimes
    }
    poll_cancel_requested_by_relay: Dict[str, bool] = {}
    registration_succeeded_by_relay: Dict[str, bool] = {}
    poll_threads: List[threading.Thread] = []
    recovery_lock = threading.Lock()
    recovery_done = threading.Event()
    recovery_done.set()
    recovery_fatal = False

    def request_poll_cancel(relay_runtime: Any, active_relay_url: str) -> None:
        if poll_cancel_requested_by_relay.get(active_relay_url):
            return
        poll_cancel_requested_by_relay[active_relay_url] = True
        relay_client = getattr(relay_runtime, "relay_client", None)
        print(
            "desktop.compute_node_bridge.poll.cancel_requested "
            f"relay={_sanitize_relay_target(active_relay_url)} "
            f"key_fingerprint={_relay_key_fingerprint(relay_client)}",
            file=sys.stderr,
        )
        relay_stop = getattr(relay_client, "stop", None)
        if callable(relay_stop):
            try:
                relay_stop()
            except Exception as exc:
                print(
                    "desktop.compute_node_bridge.relay.stop_failed "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                    f"exc_type={type(exc).__name__}",
                    file=sys.stderr,
                )
        unregister = getattr(relay_client, "unregister_from_relay", None)
        registered_relays = getattr(relay_client, "_api_v1_registered_relays", None)
        should_unregister = registration_succeeded_by_relay.get(active_relay_url, False) or (
            isinstance(registered_relays, set) and bool(registered_relays)
        )
        if callable(unregister) and should_unregister:
            print(
                "desktop.compute_node_bridge.unregister.attempted "
                f"relay={_sanitize_relay_target(active_relay_url)} "
                f"key_fingerprint={_relay_key_fingerprint(relay_client)}",
                file=sys.stderr,
            )
            try:
                unregistered = bool(unregister())
            except Exception as exc:
                print(
                    "desktop.compute_node_bridge.unregister.failed "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                    f"exc_type={type(exc).__name__}",
                    file=sys.stderr,
                )
            else:
                unregister_event = (
                    "desktop.compute_node_bridge.unregister.succeeded"
                    if unregistered
                    else "desktop.compute_node_bridge.unregister.failed"
                )
                print(
                    f"{unregister_event} "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                    f"success={unregistered}",
                    file=sys.stderr,
                )
        elif callable(unregister):
            print(
                "desktop.compute_node_bridge.unregister.skipped "
                f"relay={_sanitize_relay_target(active_relay_url)} "
                f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                "reason=not_registered",
                file=sys.stderr,
            )

    def mark_all_relays_unregistered(
        *,
        relay_runtime_state: str,
        error: Optional[str],
        request_id: Optional[str] = None,
    ) -> None:
        for relay_runtime in runtimes:
            relay_url_value = getattr(
                getattr(relay_runtime, "relay_client", None),
                "relay_url",
                relay_url,
            )
            update_relay_status(
                relay_url_value,
                registered=False,
                relay_runtime_state=relay_runtime_state,
                last_error=error,
                last_request_id=request_id if request_id and request_id != "none" else None,
            )

    def best_effort_unadvertise_all_relays() -> None:
        for relay_runtime in runtimes:
            relay_url_value = getattr(
                getattr(relay_runtime, "relay_client", None),
                "relay_url",
                relay_url,
            )
            relay_client = getattr(relay_runtime, "relay_client", None)
            unregister = getattr(relay_client, "unregister_from_relay", None)
            registered_relays = getattr(relay_client, "_api_v1_registered_relays", None)
            was_registered = registration_succeeded_by_relay.get(relay_url_value, False) or (
                isinstance(registered_relays, set) and bool(registered_relays)
            )
            if callable(unregister) and was_registered:
                print(
                    "desktop.compute_node_bridge.recovery.unregister.attempted "
                    f"relay={_sanitize_relay_target(relay_url_value)} "
                    f"key_fingerprint={_relay_key_fingerprint(relay_client)}",
                    file=sys.stderr,
                )
                try:
                    unregistered = bool(unregister())
                except Exception as exc:
                    print(
                        "desktop.compute_node_bridge.recovery.unregister.failed "
                        f"relay={_sanitize_relay_target(relay_url_value)} "
                        f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                        f"exc_type={type(exc).__name__}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "desktop.compute_node_bridge.recovery.unregister.succeeded "
                        f"relay={_sanitize_relay_target(relay_url_value)} "
                        f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                        f"success={unregistered}",
                        file=sys.stderr,
                    )
            elif callable(unregister):
                print(
                    "desktop.compute_node_bridge.recovery.unregister.skipped "
                    f"relay={_sanitize_relay_target(relay_url_value)} "
                    f"key_fingerprint={_relay_key_fingerprint(relay_client)} "
                    "reason=not_registered",
                    file=sys.stderr,
                )
            else:
                relay_stop = getattr(relay_client, "stop", None)
                if callable(relay_stop):
                    try:
                        relay_stop()
                    except Exception as exc:
                        print(
                            "desktop.compute_node_bridge.recovery.stop_failed "
                            f"relay={_sanitize_relay_target(relay_url_value)} "
                            f"exc_type={type(exc).__name__}",
                            file=sys.stderr,
                        )

    def recover_shared_runtime(active_relay_url: str, request_id: str) -> bool:
        """Coordinate fail-closed recovery for the one shared model_manager."""

        nonlocal warm_load_state, warm_load_failed, last_error, recovery_fatal

        if recovery_lock.acquire(blocking=False):
            coordinator = True
            recovery_done.clear()
        else:
            coordinator = False

        if not coordinator:
            print(
                "desktop.compute_node_bridge.recovery.join_existing "
                f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                file=sys.stderr,
            )
            while True:
                recovery_done.wait(timeout=0.05)
                if recovery_done.is_set() and not recovery_lock.locked():
                    break
                if stop_requested():
                    return False
            return warm_load_state == "ready"

        try:
            attempts = _api_v1_recovery_attempts()
            backoff_seconds = _api_v1_recovery_backoff_seconds()
            last_error = "shared model runtime recovery in progress"
            warm_load_state = "recovering"
            mark_all_relays_unregistered(
                relay_runtime_state="recovering",
                error=last_error,
                request_id=request_id,
            )
            emit_operator_event(
                build_status_payload(
                    event_type="status",
                    running=True,
                    registered=False,
                    active_relay_url=active_relay_url,
                    current_last_error=last_error,
                    extra={"relay_runtime_state": "recovering"},
                )
            )
            worker_status = worker_lifecycle_status()
            print(
                "desktop.compute_node_bridge.recovery.start "
                f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
                f"safe_error_code={worker_status.get('last_worker_error_code') or 'runtime_recovery'} "
                f"worker_generation={worker_status.get('worker_generation', 'unknown')} "
                f"worker_restart_count={worker_status.get('worker_restart_count', 'unknown')} "
                f"attempts={attempts} backoff_seconds={backoff_seconds:g}",
                file=sys.stderr,
            )
            best_effort_unadvertise_all_relays()
            for attempt in range(1, attempts + 1):
                if stop_requested():
                    print(
                        "desktop.compute_node_bridge.recovery.cancelled "
                        f"attempt={attempt} request_id={request_id}",
                        file=sys.stderr,
                    )
                    return False
                print(
                    "desktop.compute_node_bridge.recovery.attempt "
                    f"attempt={attempt} request_id={request_id}",
                    file=sys.stderr,
                )
                try:
                    if bool(runtime.ensure_api_v1_runtime_ready()):
                        warm_load_state = "ready"
                        warm_load_failed = None
                        last_error = None
                        for relay_runtime in runtimes:
                            relay_start = getattr(relay_runtime, "start_relay_session", None)
                            if callable(relay_start):
                                relay_start()
                            else:
                                client_start = getattr(
                                    getattr(relay_runtime, "relay_client", None), "start", None
                                )
                                if callable(client_start):
                                    client_start()
                        mark_all_relays_unregistered(relay_runtime_state="ready", error=None)
                        emit_operator_event(
                            build_status_payload(
                                event_type="status",
                                running=True,
                                registered=False,
                                active_relay_url=active_relay_url,
                                current_last_error=None,
                                extra={"relay_runtime_state": "ready"},
                            )
                        )
                        worker_status = worker_lifecycle_status()
                        print(
                            "desktop.compute_node_bridge.recovery.succeeded "
                            f"attempt={attempt} request_id={request_id} "
                            f"safe_error_code={worker_status.get('last_worker_error_code') or 'none'} "
                            f"worker_generation={worker_status.get('worker_generation', 'unknown')} "
                            f"worker_restart_count={worker_status.get('worker_restart_count', 'unknown')}",
                            file=sys.stderr,
                        )
                        return True
                except Exception as exc:
                    print(
                        "desktop.compute_node_bridge.recovery.attempt_exception "
                        f"attempt={attempt} request_id={request_id} "
                        f"exc_type={type(exc).__name__}",
                        file=sys.stderr,
                    )
                if attempt < attempts and _sleep_with_cancel(backoff_seconds):
                    print(
                        "desktop.compute_node_bridge.recovery.cancelled_during_backoff "
                        f"attempt={attempt} request_id={request_id}",
                        file=sys.stderr,
                    )
                    return False

            warm_load_state = "failed"
            warm_load_failed = "shared model runtime recovery exhausted; restart the desktop compute node"
            last_error = warm_load_failed
            recovery_fatal = True
            mark_all_relays_unregistered(
                relay_runtime_state="failed",
                error=last_error,
                request_id=request_id,
            )
            emit_operator_event(
                build_status_payload(
                    event_type="error",
                    running=False,
                    registered=False,
                    active_relay_url=active_relay_url,
                    current_last_error=last_error,
                    extra={"relay_runtime_state": "failed", "message": last_error},
                )
            )
            worker_status = worker_lifecycle_status()
            print(
                "desktop.compute_node_bridge.recovery.exhausted "
                f"request_id={request_id} action=restart_desktop_compute_node "
                f"safe_error_code={worker_status.get('last_worker_error_code') or 'recovery_exhausted'} "
                f"worker_generation={worker_status.get('worker_generation', 'unknown')} "
                f"worker_restart_count={worker_status.get('worker_restart_count', 'unknown')} "
                f"exit_code={worker_status.get('last_worker_exit_code')}",
                file=sys.stderr,
            )
            return False
        finally:
            recovery_done.set()
            recovery_lock.release()

    def poll_relay_loop(relay_runtime: Any) -> None:
        nonlocal last_error, poll_failure_fatal, warm_load_state
        active_relay_url = relay_runtime.relay_client.relay_url
        worker = relay_poll_workers[active_relay_url]
        while not stop_requested():
            active_relay_url = relay_runtime.relay_client.relay_url
            if warm_load_state == "recovering":
                update_relay_status(
                    active_relay_url,
                    registered=False,
                    relay_runtime_state="recovering",
                    last_error=last_error,
                )
                if recovery_done.wait(timeout=0.05):
                    continue
                continue
            if warm_load_state == "failed":
                update_relay_status(
                    active_relay_url,
                    registered=False,
                    relay_runtime_state="failed",
                    last_error=last_error,
                )
                emit_status_event(
                    registered=False,
                    active_relay_url=active_relay_url,
                    current_last_error=last_error,
                )
                break
            print(
                "desktop.compute_node_bridge.api_v1_e2ee.register "
                f"operator_session_id={bridge_session_id} "
                f"relay={_sanitize_relay_target(active_relay_url)} "
                f"key_fingerprint={_relay_key_fingerprint(relay_runtime.relay_client)}",
                file=sys.stderr,
            )
            try:
                relay_response = worker.call(
                    relay_runtime.register_and_poll_once,
                    stop_requested,
                    on_cancel=lambda relay=active_relay_url, rt=relay_runtime: request_poll_cancel(rt, relay),
                )
            except KeyboardInterrupt:
                break
            except Exception as exc:
                relay_last_error = f"relay poll failed: {type(exc).__name__}"
                if str(exc):
                    relay_last_error = f"{relay_last_error}: {exc}"
                last_error = relay_last_error
                poll_failure_fatal = True
                update_relay_status(
                    active_relay_url,
                    registered=False,
                    relay_runtime_state="failed",
                    last_error=relay_last_error,
                )
                print(
                    "desktop.compute_node_bridge.poll.exception "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"exc_type={type(exc).__name__}",
                    file=sys.stderr,
                )
                emit_operator_event(
                    build_status_payload(
                        event_type="error",
                        running=True,
                        registered=False,
                        active_relay_url=active_relay_url,
                        current_last_error=last_error,
                        extra={
                            "relay_runtime_state": "failed",
                            "message": last_error,
                        },
                    )
                )
                break
            if relay_response is _POLL_CANCELLED:
                print(
                    "desktop.compute_node_bridge.poll.cancelled "
                    f"relay={_sanitize_relay_target(active_relay_url)}",
                    file=sys.stderr,
                )
                break
            relay_response = relay_response if isinstance(relay_response, dict) else {}
            active_relay_url = relay_runtime.relay_client.relay_url
            api_v1_payload = is_api_v1_relay_payload(relay_response)
            relay_error = _relay_error_message(relay_response)
            has_heartbeat = "next_ping_in_x_seconds" in relay_response
            registered = (
                relay_error is None
                and (has_heartbeat or api_v1_payload)
                and _registration_fresh(relay_runtime.relay_client, active_relay_url)
            )
            if warm_load_state == "failed":
                registered = False
            if registered:
                registration_succeeded_by_relay[active_relay_url] = True
            wait_seconds = _safe_poll_wait_seconds(
                relay_response, getattr(relay_runtime.relay_client, "_request_timeout", 1)
            )
            request_id = (
                relay_response.get("request_id")
                if isinstance(relay_response.get("request_id"), str)
                else "none"
            )
            summary = _relay_response_summary(
                relay_response, api_v1_payload=api_v1_payload, wait_seconds=wait_seconds
            )

            registration_event = (
                "desktop.compute_node_bridge.registration.succeeded"
                if registered
                else "desktop.compute_node_bridge.registration.pending"
            )
            print(
                f"{registration_event} "
                f"operator_session_id={bridge_session_id} "
                f"relay={_sanitize_relay_target(active_relay_url)} "
                f"key_fingerprint={_relay_key_fingerprint(relay_runtime.relay_client)} "
                f"request_id={request_id}"
                + (f" error={relay_error}" if relay_error else ""),
                file=sys.stderr,
            )
            print(
                "desktop.compute_node_bridge.relay_poll "
                f"relay={_sanitize_relay_target(active_relay_url)} registered={registered} "
                f"api_v1_payload={api_v1_payload} heartbeat={has_heartbeat} "
                f"request_id={request_id} wait={wait_seconds} summary={summary}",
                file=sys.stderr,
            )
            print(
                "desktop.compute_node_bridge.api_v1_e2ee.poll "
                f"relay={_sanitize_relay_target(active_relay_url)} registered={registered} "
                f"api_v1_payload={api_v1_payload} heartbeat={has_heartbeat} "
                f"request_id={request_id} wait={wait_seconds} summary={summary}",
                file=sys.stderr,
            )

            relay_last_error: Optional[str] = None
            relay_state = _relay_runtime_state(
                warm_load_state, running=True, warm_load_enabled=warm_load_enabled
            )
            if not registered:
                relay_last_error = relay_error or (
                    "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                    "operator; update relay.py to repo HEAD"
                )
                last_error = relay_last_error
            elif api_v1_payload:
                update_relay_status(
                    active_relay_url,
                    registered=registered,
                    relay_runtime_state="processing",
                    last_error=None,
                    last_request_id=request_id,
                    request_count=increment_relay_request_count(active_relay_url),
                )
                print(
                    f"desktop.compute_node_bridge.request_route runtime_path={runtime_path} "
                    f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                    file=sys.stderr,
                )
                print(
                    "desktop.compute_node_bridge.process_request "
                    f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                    file=sys.stderr,
                )
                print(
                    "desktop.compute_node_bridge.api_v1_e2ee.work_received "
                    f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                    file=sys.stderr,
                )
                emit_operator_event(
                    build_status_payload(
                        event_type="status",
                        running=True,
                        registered=registered,
                        active_relay_url=active_relay_url,
                        current_last_error=last_error,
                        extra={"relay_runtime_state": "processing"},
                    )
                )
                process_result = None
                try:
                    with inference_lock:
                        process_relay_request_result = getattr(
                            relay_runtime,
                            "process_relay_request_result",
                            None,
                        )
                        if callable(process_relay_request_result):
                            process_result = process_relay_request_result(relay_response)
                        else:
                            processed_bool = relay_runtime.process_relay_request(relay_response)
                            process_result = {
                                "inference_succeeded": bool(processed_bool),
                                "submitted": bool(processed_bool),
                                "safe_error_code": None,
                                "runtime_healthy": True,
                                "recovery_attempted": False,
                                "recovery_succeeded": False,
                            }
                except Exception as exc:
                    process_result = {
                        "inference_succeeded": False,
                        "submitted": False,
                        "safe_error_code": "compute_node_process_failed",
                        "runtime_healthy": False,
                        "recovery_attempted": True,
                        "recovery_succeeded": False,
                    }
                    relay_last_error = "failed to process relay request"
                    last_error = relay_last_error
                    print(
                        "desktop.compute_node_bridge.process_request.exception "
                        f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
                        f"exc_type={type(exc).__name__}",
                        file=sys.stderr,
                    )
                inference_succeeded = bool(
                    getattr(process_result, "inference_succeeded", False)
                    if not isinstance(process_result, dict)
                    else process_result.get("inference_succeeded")
                )
                submitted = bool(
                    getattr(process_result, "submitted", bool(process_result))
                    if not isinstance(process_result, dict)
                    else process_result.get("submitted")
                )
                safe_error_code = (
                    getattr(process_result, "safe_error_code", None)
                    if not isinstance(process_result, dict)
                    else process_result.get("safe_error_code")
                )
                runtime_healthy = bool(
                    getattr(process_result, "runtime_healthy", True)
                    if not isinstance(process_result, dict)
                    else process_result.get("runtime_healthy", True)
                )
                recovery_attempted = bool(
                    getattr(process_result, "recovery_attempted", False)
                    if not isinstance(process_result, dict)
                    else process_result.get("recovery_attempted", False)
                )
                recovery_succeeded = bool(
                    getattr(process_result, "recovery_succeeded", False)
                    if not isinstance(process_result, dict)
                    else process_result.get("recovery_succeeded", False)
                )
                if not inference_succeeded:
                    relay_last_error = "failed to process relay request"
                    if safe_error_code:
                        relay_last_error = f"relay request failed: {safe_error_code}"
                    last_error = relay_last_error
                    print(
                        "desktop.compute_node_bridge.process_request.failed "
                        f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
                        f"safe_error_code={safe_error_code or 'none'} submitted={submitted} "
                        f"runtime_healthy={runtime_healthy} "
                        f"recovery_attempted={recovery_attempted} "
                        f"recovery_succeeded={recovery_succeeded}",
                        file=sys.stderr,
                    )
                    if submitted:
                        print(
                            "desktop.compute_node_bridge.api_v1_e2ee.error_envelope_submitted "
                            f"relay={_sanitize_relay_target(active_relay_url)} "
                            f"request_id={request_id} safe_error_code={safe_error_code or 'unknown'}",
                            file=sys.stderr,
                        )
                    elif runtime_healthy:
                        submit_api_v1_error_response(
                            relay_response,
                            code="compute_node_process_failed",
                            message=last_error,
                            active_relay_url=active_relay_url,
                            request_id=request_id,
                            relay_runtime=relay_runtime,
                        )
                    else:
                        print(
                            "desktop.compute_node_bridge.api_v1_e2ee.error_response.skipped "
                            f"relay={_sanitize_relay_target(active_relay_url)} "
                            f"request_id={request_id} reason=shared_runtime_recovery",
                            file=sys.stderr,
                        )
                    if not runtime_healthy:
                        registered = False
                        if recovery_succeeded:
                            relay_state = "ready"
                            warm_load_state = "ready"
                        elif recovery_attempted:
                            relay_state = "recovering"
                            recovered = recover_shared_runtime(active_relay_url, request_id)
                            if recovered:
                                relay_state = "ready"
                            else:
                                relay_state = "failed" if warm_load_state == "failed" else "recovering"
                        else:
                            relay_state = "failed"
                            warm_load_state = "failed"
                else:
                    last_error = None
                    print(
                        "desktop.compute_node_bridge.api_v1_e2ee.response_submitted "
                        f"relay={_sanitize_relay_target(active_relay_url)} "
                        f"request_id={request_id}",
                        file=sys.stderr,
                    )
                wait_seconds = 0.0
                print(
                    "desktop.compute_node_bridge.api_v1_e2ee.work_processed_next_poll_immediate "
                    f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                    file=sys.stderr,
                )
            else:
                if has_heartbeat:
                    last_error = None
                else:
                    relay_last_error = (
                        "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                        "operator; update relay.py to repo HEAD"
                    )
                    last_error = relay_last_error

            update_relay_status(
                active_relay_url,
                registered=registered,
                relay_runtime_state=relay_state,
                last_error=relay_last_error,
                last_request_id=request_id if request_id != "none" else None,
            )
            emit_status_event(
                registered=registered,
                active_relay_url=active_relay_url,
                current_last_error=last_error,
            )
            if _sleep_with_cancel(wait_seconds):
                print(
                    "desktop.compute_node_bridge.stop_requested "
                    f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                    file=sys.stderr,
                )
                break

    try:
        if warm_runtime_before_registration():
            for relay_runtime in runtimes:
                thread = threading.Thread(
                    target=poll_relay_loop,
                    args=(relay_runtime,),
                    name=f"tokenplace-relay-poller-{_sanitize_relay_target(relay_runtime.relay_client.relay_url)}",
                    daemon=True,
                )
                poll_threads.append(thread)
                thread.start()
            while any(thread.is_alive() for thread in poll_threads):
                for thread in poll_threads:
                    thread.join(timeout=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for relay_runtime in runtimes:
            active_relay_url = getattr(getattr(relay_runtime, "relay_client", None), "relay_url", relay_url)
            print(
                "desktop.compute_node_bridge.stop "
                f"operator_session_id={bridge_session_id} "
                f"relay={_sanitize_relay_target(active_relay_url)}",
                file=sys.stderr,
            )
            request_poll_cancel(relay_runtime, active_relay_url)
        for worker in relay_poll_workers.values():
            worker.shutdown()
        for thread in poll_threads:
            thread.join(timeout=1)
        for relay_runtime in runtimes:
            active_relay_url = getattr(getattr(relay_runtime, "relay_client", None), "relay_url", relay_url)
            print(
                "desktop.compute_node_bridge.poll.worker_stopped "
                f"operator_session_id={bridge_session_id} "
                f"relay={_sanitize_relay_target(active_relay_url)}",
                file=sys.stderr,
            )
            try:
                relay_runtime.stop()
            except Exception as exc:
                print(
                    "desktop.compute_node_bridge.runtime.stop_failed "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"exc_type={type(exc).__name__}",
                    file=sys.stderr,
                )
            update_relay_status(
                active_relay_url,
                registered=False,
                relay_runtime_state="stopped",
            )
            print(
                "desktop.compute_node_bridge.stopped_idle "
                f"operator_session_id={bridge_session_id} "
                f"relay={_sanitize_relay_target(active_relay_url)}",
                file=sys.stderr,
            )

    emit_operator_event(
        build_status_payload(
            event_type="stopped",
            running=False,
            registered=False,
            active_relay_url=runtime.relay_client.relay_url,
            current_last_error=last_error,
        )
    )
    return 1 if warm_load_fatal or poll_failure_fatal or recovery_fatal else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop compute-node bridge")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--relay-url", action="append", default=None)
    parser.add_argument(
        "--relay-urls",
        action="append",
        default=None,
        help="JSON array or comma-separated relay URL list (can be repeated)",
    )
    parser.add_argument("--relay-port", type=int, default=None)
    parser.add_argument("--context-tier", default="8k-fast")
    args = parser.parse_args()

    try:
        args.mode = _normalize_compute_mode_local(args.mode)
        return run(args)
    except Exception as exc:  # pragma: no cover - last resort failure handling
        message = f"{EARLY_STARTUP_EXIT_ERROR}: {exc}"
        emit(
            _structured_startup_error_payload(
                args,
                message,
                operator_session_id=_bridge_session_id_from_env(),
                sequence=1,
                updated_at_ms=int(time.time() * 1000),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
