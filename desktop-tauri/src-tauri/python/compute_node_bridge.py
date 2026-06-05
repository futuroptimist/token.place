#!/usr/bin/env python3
"""Desktop compute-node bridge that reuses the shared compute runtime."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import queue
import sys
import threading
import time
import uuid
from typing import Any, Dict, Optional
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

    def ensure_desktop_llama_runtime(_mode: str) -> Dict[str, str]:
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
        "active_relay_url": getattr(args, "relay_url", "https://token.place"),
        "requested_mode": _normalize_compute_mode_local(getattr(args, "mode", "auto")),
        "effective_mode": "pending",
        "backend_available": "pending",
        "backend_selected": "pending",
        "backend_used": "pending",
        "fallback_reason": None,
        "model_path": getattr(args, "model", ""),
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


def run(args: argparse.Namespace) -> int:
    bridge_session_id = _bridge_session_id_from_env()
    _reset_bridge_lifecycle_state(bridge_session_id)
    status_sequence = 0

    def emit_operator_event(payload: Dict[str, Any]) -> None:
        nonlocal status_sequence
        status_sequence += 1
        payload = dict(payload)
        payload.setdefault("operator_session_id", bridge_session_id)
        payload.setdefault("sequence", status_sequence)
        payload.setdefault("updated_at_ms", int(time.time() * 1000))
        emit(payload)

    def emit_startup_error(message: str) -> None:
        emit_operator_event(_structured_startup_error_payload(args, message))

    runtime_setup = ensure_desktop_llama_runtime(args.mode)
    maybe_reexec_for_runtime_refresh(runtime_setup)
    print(
        "desktop.runtime_setup "
        f"mode={args.mode} "
        f"selected_backend={runtime_setup.get('selected_backend', 'cpu')} "
        f"device={runtime_setup.get('detected_device', 'cpu')} "
        f"action={runtime_setup.get('runtime_action', 'none')} "
        f"interpreter={runtime_setup.get('interpreter', sys.executable)} "
        f"llama_module_path={runtime_setup.get('llama_module_path', 'missing')} "
        f"fallback_reason={runtime_setup.get('fallback_reason') or 'none'}",
        file=sys.stderr,
    )
    dependency_setup = ensure_desktop_python_dependencies()
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
    repo_llama_cpp_shim_imported = _is_repo_llama_cpp_shim(
        runtime_setup.get("llama_module_path", "")
    )
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

    relay_url = resolve_relay_url(args.relay_url, prefer_cli=True)
    relay_port = resolve_relay_port(args.relay_port, relay_url)
    print(
        "desktop.compute_node_bridge.start "
        f"operator_session_id={bridge_session_id} "
        f"model={args.model} mode={args.mode} "
        f"relay_url={_sanitize_relay_target(relay_url)} "
        f"relay_port={relay_port if relay_port is not None else 'none'}",
        file=sys.stderr,
    )
    print(
        "desktop.compute_node_bridge.relay_target.resolved "
        f"relay={_sanitize_relay_target(relay_url)}",
        file=sys.stderr,
    )

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(
            relay_url=relay_url,
            relay_port=relay_port,
            use_configured_relay_fallbacks=False,
        )
    )
    start_relay_session = getattr(runtime, "start_relay_session", None)
    if callable(start_relay_session):
        start_relay_session()
    else:
        relay_start = getattr(getattr(runtime, "relay_client", None), "start", None)
        if callable(relay_start):
            relay_start()
    print(
        "desktop.compute_node_bridge.relay_client.reset "
        f"operator_session_id={bridge_session_id} "
        f"relay={_sanitize_relay_target(runtime.relay_client.relay_url)} "
        f"key_fingerprint={_relay_key_fingerprint(runtime.relay_client)}",
        file=sys.stderr,
    )

    runtime.model_manager.model_path = args.model
    apply_compute_mode(runtime.model_manager, args.mode)
    try:
        runtime.model_manager.desktop_runtime_probe = dict(runtime_setup)
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
    poll_worker = _CancelablePollWorker(operator_session_id=bridge_session_id)
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
        fresh_registered = (
            running
            and relay_state == "ready"
            and registered
            and _registration_fresh(runtime.relay_client, active_relay_url)
        )
        diagnostics = _status_diagnostics(compute_mode_diagnostics(runtime.model_manager), relay_state)
        payload: Dict[str, Any] = {
            "type": event_type,
            "running": running,
            "registered": fresh_registered,
            "relay_runtime_state": relay_state,
            "active_relay_url": active_relay_url,
            "requested_mode": diagnostics.get("requested_mode"),
            "effective_mode": diagnostics.get("effective_mode"),
            "backend_available": diagnostics.get("backend_available"),
            "backend_selected": diagnostics.get("backend_selected"),
            "backend_used": diagnostics.get("backend_used"),
            "offloaded_layers": diagnostics.get("offloaded_layers", diagnostics.get("n_gpu_layers")),
            "kv_cache_device": diagnostics.get("kv_cache_device"),
            "fallback_reason": diagnostics.get("fallback_reason"),
            "interpreter": runtime_setup.get("interpreter", sys.executable),
            "llama_module_path": runtime_setup.get("llama_module_path", "missing"),
            "model_path": args.model,
            "last_error": current_last_error,
            "warm_load_state": warm_load_state,
            "warm_load_enabled": warm_load_enabled,
            "warm_load_duration_ms": warm_load_duration_ms,
            "runtime_path": runtime_path,
            "relay_runtime_path": relay_runtime_path,
        }
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
    ) -> bool:
        submit_error = getattr(runtime, "submit_api_v1_error_response", None)
        if not callable(submit_error):
            relay_client = getattr(runtime, "relay_client", None)
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
                emit_status_event(
                    registered=False,
                    active_relay_url=runtime.relay_client.relay_url,
                    current_last_error=last_error,
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
            if now - last_progress_log_at >= progress_emit_interval_seconds:
                last_progress_log_at = now
                duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
                print(
                    "desktop.compute_node_bridge.model_init.still_warming "
                    f"reason=pre_registration relay={_sanitize_relay_target(runtime.relay_client.relay_url)} "
                    f"state={warm_load_state} duration_ms={duration_ms} "
                    f"timeout_seconds={warm_load_deadline_seconds}",
                    file=sys.stderr,
                )
            if now - last_status_emit_at >= progress_emit_interval_seconds:
                last_status_emit_at = now
                emit_status_event(
                    registered=False,
                    active_relay_url=runtime.relay_client.relay_url,
                    current_last_error=last_error,
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

    poll_cancel_requested = False
    registration_succeeded = False

    def request_poll_cancel(active_relay_url: str) -> None:
        nonlocal poll_cancel_requested
        if poll_cancel_requested:
            return
        poll_cancel_requested = True
        relay_client = getattr(runtime, "relay_client", None)
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
        should_unregister = registration_succeeded or (
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

    try:
        if warm_runtime_before_registration():
            while not stop_requested():
                active_relay_url = runtime.relay_client.relay_url
                print(
                    "desktop.compute_node_bridge.api_v1_e2ee.register "
                    f"operator_session_id={bridge_session_id} "
                    f"relay={_sanitize_relay_target(active_relay_url)} "
                    f"key_fingerprint={_relay_key_fingerprint(runtime.relay_client)}",
                    file=sys.stderr,
                )
                relay_response = poll_worker.call(
                    runtime.register_and_poll_once,
                    stop_requested,
                    on_cancel=lambda relay=active_relay_url: request_poll_cancel(relay),
                )
                if relay_response is _POLL_CANCELLED:
                    print(
                        "desktop.compute_node_bridge.poll.cancelled "
                        f"relay={_sanitize_relay_target(active_relay_url)}",
                        file=sys.stderr,
                    )
                    break
                relay_response = relay_response if isinstance(relay_response, dict) else {}
                active_relay_url = runtime.relay_client.relay_url
                api_v1_payload = is_api_v1_relay_payload(relay_response)
                relay_error = _relay_error_message(relay_response)
                has_heartbeat = (
                    isinstance(relay_response, dict) and "next_ping_in_x_seconds" in relay_response
                )
                registered = (
                    relay_error is None
                    and (has_heartbeat or api_v1_payload)
                    and _registration_fresh(runtime.relay_client, active_relay_url)
                )
                if registered:
                    registration_succeeded = True
                wait_seconds = _safe_poll_wait_seconds(
                    relay_response, getattr(runtime.relay_client, "_request_timeout", 1)
                )
                request_id = (
                    relay_response.get("request_id")
                    if isinstance(relay_response, dict)
                    and isinstance(relay_response.get("request_id"), str)
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
                    f"key_fingerprint={_relay_key_fingerprint(runtime.relay_client)} "
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

                if not registered:
                    if relay_error is not None:
                        last_error = relay_error
                    else:
                        last_error = (
                            "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                            "operator; update relay.py to repo HEAD"
                        )
                elif api_v1_payload:
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
                    try:
                        processed = runtime.process_relay_request(relay_response)
                    except Exception as exc:
                        processed = False
                        last_error = "failed to process relay request"
                        print(
                            "desktop.compute_node_bridge.process_request.exception "
                            f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id} "
                            f"exc_type={type(exc).__name__}",
                            file=sys.stderr,
                        )
                    if not processed:
                        last_error = "failed to process relay request"
                        print(
                            "desktop.compute_node_bridge.process_request.failed "
                            f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                            file=sys.stderr,
                        )
                        submit_api_v1_error_response(
                            relay_response,
                            code="compute_node_process_failed",
                            message=last_error,
                            active_relay_url=active_relay_url,
                            request_id=request_id,
                        )
                    else:
                        last_error = None
                        print(
                            "desktop.compute_node_bridge.api_v1_e2ee.response_submitted "
                            f"relay={_sanitize_relay_target(active_relay_url)} "
                            f"request_id={request_id}",
                            file=sys.stderr,
                        )
                else:
                    if has_heartbeat:
                        last_error = None
                    else:
                        last_error = (
                            "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                            "operator; update relay.py to repo HEAD"
                        )

                emit_status_event(
                    registered=registered,
                    active_relay_url=active_relay_url,
                    current_last_error=last_error,
                )

                if api_v1_payload:
                    wait_seconds = 0.0
                    print(
                        "desktop.compute_node_bridge.api_v1_e2ee.work_processed_next_poll_immediate "
                        f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                        file=sys.stderr,
                    )

                if _sleep_with_cancel(wait_seconds):
                    print(
                        "desktop.compute_node_bridge.stop_requested "
                        f"relay={_sanitize_relay_target(active_relay_url)} request_id={request_id}",
                        file=sys.stderr,
                    )
                    break
    except KeyboardInterrupt:
        pass
    finally:
        active_relay_url = getattr(getattr(runtime, "relay_client", None), "relay_url", relay_url)
        print(
            "desktop.compute_node_bridge.stop "
            f"operator_session_id={bridge_session_id} "
            f"relay={_sanitize_relay_target(active_relay_url)}",
            file=sys.stderr,
        )
        request_poll_cancel(active_relay_url)
        poll_worker.shutdown()
        print(
            "desktop.compute_node_bridge.poll.worker_stopped "
            f"operator_session_id={bridge_session_id} "
            f"relay={_sanitize_relay_target(active_relay_url)}",
            file=sys.stderr,
        )
        # Leave any still-running warm-load thread alone.  It is daemonized so it
        # cannot keep process shutdown alive, and bridge state has already moved
        # on to stop/failure handling.
        runtime.stop()
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
    return 1 if warm_load_fatal else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop compute-node bridge")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--relay-url", default="https://token.place")
    parser.add_argument("--relay-port", type=int, default=None)
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
