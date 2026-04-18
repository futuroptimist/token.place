#!/usr/bin/env python3
"""Desktop compute-node bridge that reuses the shared compute runtime."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

if __package__ in (None, ""):
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths

try:
    from desktop_runtime_setup import ensure_desktop_llama_runtime, maybe_reexec_for_runtime_refresh
except ModuleNotFoundError:
    def ensure_desktop_llama_runtime(_mode: str) -> Dict[str, str]:
        return {
            "selected_backend": "cpu",
            "detected_device": "cpu",
            "runtime_action": "unavailable",
            "fallback_reason": "desktop_runtime_setup module missing",
        }

    def maybe_reexec_for_runtime_refresh(
        _runtime_setup: Dict[str, str], *, allow_reexec: bool = True
    ) -> None:
        return

ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)

_stdin_lines: queue.Queue[str] = queue.Queue()
_stdin_reader_started = False
_stdin_reader_lock = threading.Lock()
EARLY_STARTUP_EXIT_ERROR = "compute-node bridge exited before emitting a startup event"




def _log_bridge(stage: str, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    message = f"desktop.compute_node.bridge stage={stage}"
    if details:
        message = f"{message} {details}"
    print(message, file=sys.stderr)


def _relay_error_message(relay_response: Any) -> Optional[str]:
    if not isinstance(relay_response, dict):
        return f"invalid relay response type: {type(relay_response).__name__}"

    if "error" not in relay_response:
        return None

    error_payload = relay_response.get("error")
    if error_payload in (None, ""):
        return None

    if isinstance(error_payload, dict):
        nested = error_payload.get("message")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    if isinstance(error_payload, str):
        candidate = error_payload.strip()
        return candidate or "relay registration failed"

    return str(error_payload)


def _summarize_relay_response(relay_response: Any) -> str:
    if not isinstance(relay_response, dict):
        return f"type={type(relay_response).__name__}"

    interesting = {
        "next_ping_in_x_seconds": relay_response.get("next_ping_in_x_seconds"),
        "has_legacy_payload": bool(
            {"client_public_key", "chat_history", "cipherkey", "iv"}.issubset(
                relay_response
            )
        ),
        "keys": sorted(relay_response.keys()),
    }
    error_message = _relay_error_message(relay_response)
    if error_message:
        interesting["error"] = error_message
    return json.dumps(interesting, sort_keys=True)


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


def run(args: argparse.Namespace) -> int:
    _log_bridge(
        "startup",
        model=args.model,
        mode=args.mode,
        relay_url=args.relay_url,
        relay_port=args.relay_port,
    )
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

    try:
        from utils.compute_node_runtime import (
            apply_compute_mode,
            compute_mode_diagnostics,
            ComputeNodeRuntime,
            ComputeNodeRuntimeConfig,
            is_legacy_relay_payload,
            resolve_relay_port,
            resolve_relay_url,
        )
    except ModuleNotFoundError as exc:
        emit({"type": "error", "message": f"runtime unavailable: {exc}"})
        return 1

    relay_url = resolve_relay_url(args.relay_url, prefer_cli=True)
    relay_port = resolve_relay_port(args.relay_port, relay_url)
    _log_bridge("relay_target_resolved", relay_url=relay_url, relay_port=relay_port)

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(
            relay_url=relay_url,
            relay_port=relay_port,
            use_configured_relay_fallbacks=False,
        )
    )

    runtime.model_manager.model_path = args.model
    selected_mode = apply_compute_mode(runtime.model_manager, args.mode)
    _log_bridge("compute_mode_applied", requested_mode=selected_mode)

    _log_bridge("model_runtime_initializing", model_path=args.model)
    if not runtime.ensure_model_ready():
        emit(
            {
                "type": "error",
                "message": "failed to initialize model runtime",
                "active_relay_url": runtime.relay_client.relay_url,
            }
        )
        _log_bridge("model_runtime_failed", model_path=args.model)
        return 1

    _log_bridge("model_runtime_ready", model_path=args.model)
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
            "model_path": args.model,
            "last_error": None,
        }
    )

    try:
        while not stop_requested():
            try:
                relay_response = runtime.register_and_poll_once()
            except Exception as exc:  # pragma: no cover - defensive fallback
                relay_response = {"error": f"relay polling failed: {exc}", "next_ping_in_x_seconds": 1}
                _log_bridge(
                    "relay_poll_exception",
                    error=exc,
                    traceback=traceback.format_exc(limit=1).strip().replace("\n", " | "),
                )

            active_relay_url = runtime.relay_client.relay_url
            legacy_payload = is_legacy_relay_payload(relay_response)
            heartbeat_ack = isinstance(relay_response, dict) and (
                "next_ping_in_x_seconds" in relay_response
            )
            relay_error = _relay_error_message(relay_response)
            registered = relay_error is None and (legacy_payload or heartbeat_ack)

            _log_bridge(
                "relay_poll_result",
                relay_url=active_relay_url,
                registered=registered,
                summary=_summarize_relay_response(relay_response),
            )

            if not registered:
                if relay_error:
                    last_error = relay_error
                else:
                    last_error = (
                        "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                        "operator; update relay.py to repo HEAD"
                    )
            elif legacy_payload:
                _log_bridge("relay_request_received", relay_url=active_relay_url)
                processed = runtime.process_relay_request(relay_response)
                if not processed:
                    last_error = "failed to process relay request"
                    _log_bridge("relay_request_process_failed", relay_url=active_relay_url)
                else:
                    last_error = None
                    _log_bridge("relay_request_processed", relay_url=active_relay_url)
            else:
                last_error = None

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
                    "last_error": last_error,
                }
            )

            wait_seconds = 1.0
            if isinstance(relay_response, dict):
                try:
                    wait_seconds = float(relay_response.get("next_ping_in_x_seconds", 1))
                except (TypeError, ValueError):
                    wait_seconds = 1.0
            if _sleep_with_cancel(wait_seconds):
                _log_bridge("shutdown_requested")
                break
    finally:
        _log_bridge("runtime_stopping")
        runtime.stop()
        _log_bridge("runtime_stopped")

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
            "last_error": None,
        }
    )
    return 0


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
