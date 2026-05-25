#!/usr/bin/env python3
"""Desktop compute-node bridge that reuses the shared compute runtime."""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

if __package__ in (None, ""):
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths

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

    def ensure_desktop_python_dependencies(_required_modules: list[str]) -> Dict[str, str]:
        return {"ok": "1", "missing": ""}

    def maybe_reexec_for_runtime_refresh(
        _runtime_setup: Dict[str, str], *, allow_reexec: bool = True
    ) -> None:
        return

ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)

try:
    from utils.llm.model_manager import _is_repo_llama_cpp_shim
except ModuleNotFoundError:
    def _is_repo_llama_cpp_shim(_module_path: Any) -> bool:
        return False

_stdin_lines: queue.Queue[str] = queue.Queue()
_stdin_reader_started = False
_stdin_reader_lock = threading.Lock()
EARLY_STARTUP_EXIT_ERROR = "compute-node bridge exited before emitting a startup event"
WARM_LOAD_DEFAULT = "1"
RUNTIME_PATH_DEFAULT = "bridge"


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

    parsed = urlsplit(relay_url.strip())
    if not parsed.scheme or not parsed.hostname:
        return "unknown"

    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", "", "", ""))


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
            return True


def emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _sleep_with_cancel(seconds: float) -> bool:
    deadline = time.time() + max(seconds, 0)
    while time.time() < deadline:
        if stop_requested():
            return True
        time.sleep(0.1)
    return stop_requested()


def _required_desktop_modules() -> list[str]:
    return ["psutil", "requests", "dotenv", "jsonschema"]


def run(args: argparse.Namespace) -> int:
    dependency_check = ensure_desktop_python_dependencies(_required_desktop_modules())
    if dependency_check.get("ok") != "1":
        missing = dependency_check.get("missing", "unknown")
        interpreter = dependency_check.get("interpreter", sys.executable)
        prefix = dependency_check.get("prefix", sys.prefix)
        emit({
            "type": "status",
            "running": False,
            "registered": False,
            "last_error": (
                "desktop Python dependency preflight failed before startup event: "
                f"missing [{missing}] interpreter={interpreter} prefix={prefix}"
            ),
        })
        return 1

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
        emit({"type": "error", "message": gpu_runtime_error})
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
        emit({"type": "error", "message": f"runtime unavailable: {exc}"})
        return 1

    relay_url = resolve_relay_url(args.relay_url, prefer_cli=True)
    relay_port = resolve_relay_port(args.relay_port, relay_url)
    print(
        "desktop.compute_node_bridge.start "
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

    runtime.model_manager.model_path = args.model
    apply_compute_mode(runtime.model_manager, args.mode)

    warm_load_enabled = _env_enabled("TOKENPLACE_DESKTOP_WARM_LOAD", WARM_LOAD_DEFAULT)
    runtime_path = _runtime_path_from_env()
    dual_runtime_enabled = _env_enabled("TOKENPLACE_DESKTOP_DUAL_RUNTIME", "0")
    bridge_runtime_allowed = runtime_path == "bridge" or dual_runtime_enabled
    warm_load_state = "not_started"
    warm_load_started_at = 0.0
    warm_load_duration_ms: Optional[int] = None
    warm_load_failed: Optional[str] = None
    warm_load_fatal = False

    def emit_status_event(*, registered: bool, active_relay_url: str, current_last_error: Optional[str]) -> None:
        diagnostics = compute_mode_diagnostics(runtime.model_manager)
        emit(
            {
                "type": "status",
                "running": True,
                "registered": registered,
                "active_relay_url": active_relay_url,
                "requested_mode": diagnostics.get("requested_mode"),
                "effective_mode": diagnostics.get("effective_mode"),
                "backend_available": diagnostics.get("backend_available"),
                "backend_selected": diagnostics.get("backend_selected"),
                "backend_used": diagnostics.get("backend_used"),
                "offloaded_layers": diagnostics.get(
                    "offloaded_layers", diagnostics.get("n_gpu_layers")
                ),
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
            }
        )

    def ensure_runtime_ready(reason: str, *, active_relay_url: str) -> bool:
        nonlocal warm_load_state, warm_load_started_at, warm_load_duration_ms, warm_load_failed
        if warm_load_state == "ready":
            return True
        if warm_load_state == "warming":
            return False
        warm_load_state = "warming"
        warm_load_failed = None
        warm_load_duration_ms = None
        warm_load_started_at = time.perf_counter()
        emit_status_event(
            registered=True,
            active_relay_url=active_relay_url,
            current_last_error=last_error,
        )
        print(
            "desktop.compute_node_bridge.model_init.start "
            f"reason={reason} state={warm_load_state}",
            file=sys.stderr,
        )
        ready = runtime.ensure_api_v1_runtime_ready()
        warm_load_duration_ms = int((time.perf_counter() - warm_load_started_at) * 1000)
        if not ready:
            warm_load_state = "failed"
            warm_load_failed = "failed to initialize API v1 model runtime"
            print(
                "desktop.compute_node_bridge.model_init.failed "
                f"reason={reason} state={warm_load_state} duration_ms={warm_load_duration_ms}",
                file=sys.stderr,
            )
            return False
        warm_load_state = "ready"
        print(
            "desktop.compute_node_bridge.model_init.ready "
            f"reason={reason} state={warm_load_state} duration_ms={warm_load_duration_ms}",
            file=sys.stderr,
        )
        print(_runtime_diagnostics_summary(compute_mode_diagnostics(runtime.model_manager)), file=sys.stderr)
        return True

    def fail_on_warm_load_error(*, active_relay_url: str) -> None:
        nonlocal last_error, warm_load_fatal
        last_error = warm_load_failed or "failed to initialize API v1 model runtime"
        emit_status_event(
            registered=True,
            active_relay_url=active_relay_url,
            current_last_error=last_error,
        )
        emit(
            {
                "type": "error",
                "message": last_error,
                "active_relay_url": runtime.relay_client.relay_url,
                "warm_load_state": warm_load_state,
                "warm_load_duration_ms": warm_load_duration_ms,
                "runtime_path": runtime_path,
            }
        )
        warm_load_fatal = True

    diagnostics = compute_mode_diagnostics(runtime.model_manager)
    last_error: Optional[str] = None
    emit(
        {
            "type": "started",
            "running": True,
            "registered": False,
            "active_relay_url": runtime.relay_client.relay_url,
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
            "llama_repo_stub_imported": repo_llama_cpp_shim_imported,
            "use_mock_llm": bool(getattr(runtime.model_manager, "use_mock_llm", False)),
            "model_path": args.model,
            "last_error": None,
            "warm_load_state": warm_load_state,
            "warm_load_enabled": warm_load_enabled,
            "runtime_path": runtime_path,
        }
    )
    if runtime_path == "sidecar" and dual_runtime_enabled:
        print(
            "desktop.compute_node_bridge.runtime_path.dual_mode_enabled "
            "runtime_path=sidecar dual_runtime_enabled=True",
            file=sys.stderr,
        )

    try:
        while not stop_requested():
            print("desktop.compute_node_bridge.api_v1_e2ee.register", file=sys.stderr)
            relay_response = runtime.register_and_poll_once()
            active_relay_url = runtime.relay_client.relay_url
            api_v1_payload = is_api_v1_relay_payload(relay_response)
            relay_error = _relay_error_message(relay_response)
            has_heartbeat = (
                isinstance(relay_response, dict) and "next_ping_in_x_seconds" in relay_response
            )
            registered = relay_error is None and (has_heartbeat or api_v1_payload)
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

            if registered and api_v1_payload and warm_load_enabled and warm_load_state != "ready":
                if not bridge_runtime_allowed:
                    warm_load_state = "not_started"
                    print(
                        "desktop.compute_node_bridge.model_init.skipped "
                        f"reason=api_v1_request runtime_path={runtime_path} "
                        f"dual_runtime_enabled={dual_runtime_enabled} state={warm_load_state}",
                        file=sys.stderr,
                    )
                elif not ensure_runtime_ready("api_v1_request", active_relay_url=active_relay_url):
                    fail_on_warm_load_error(active_relay_url=active_relay_url)
                    break

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
                    f"desktop.compute_node_bridge.request_route runtime_path={runtime_path}",
                    file=sys.stderr,
                )
                print(
                    "desktop.compute_node_bridge.process_request",
                    file=sys.stderr,
                )
                print(
                    "desktop.compute_node_bridge.api_v1_e2ee.work_received",
                    file=sys.stderr,
                )
                processed = runtime.process_relay_request(relay_response)
                if not processed:
                    last_error = "failed to process relay request"
                    print(
                        "desktop.compute_node_bridge.process_request.failed",
                        file=sys.stderr,
                    )
                else:
                    last_error = None
                    print(
                        "desktop.compute_node_bridge.api_v1_e2ee.response_submitted",
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

            if registered and warm_load_enabled and warm_load_state == "not_started":
                if not bridge_runtime_allowed:
                    warm_load_state = "not_started"
                    print(
                        "desktop.compute_node_bridge.model_init.skipped "
                        f"reason=post_registration runtime_path={runtime_path} "
                        f"dual_runtime_enabled={dual_runtime_enabled} state={warm_load_state}",
                        file=sys.stderr,
                    )
                else:
                    if not ensure_runtime_ready("post_registration", active_relay_url=active_relay_url):
                        fail_on_warm_load_error(active_relay_url=active_relay_url)
                        break
            emit_status_event(
                registered=registered,
                active_relay_url=active_relay_url,
                current_last_error=last_error,
            )

            if _sleep_with_cancel(wait_seconds):
                break
    finally:
        print("desktop.compute_node_bridge.stop", file=sys.stderr)
        runtime.stop()

    diagnostics = compute_mode_diagnostics(runtime.model_manager)
    emit(
        {
            "type": "stopped",
            "running": False,
            "registered": False,
            "active_relay_url": runtime.relay_client.relay_url,
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
            "last_error": last_error,
            "warm_load_state": warm_load_state,
            "warm_load_enabled": warm_load_enabled,
            "warm_load_duration_ms": warm_load_duration_ms,
            "runtime_path": runtime_path,
        }
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
        from utils.compute_node_runtime import normalize_compute_mode

        args.mode = normalize_compute_mode(args.mode)
        return run(args)
    except Exception as exc:  # pragma: no cover - last resort failure handling
        emit({"type": "error", "message": f"{EARLY_STARTUP_EXIT_ERROR}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
