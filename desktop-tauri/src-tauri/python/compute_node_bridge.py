#!/usr/bin/env python3
"""Desktop compute-node bridge that reuses the shared compute runtime."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
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


def log_debug(message: str) -> None:
    """Emit bridge diagnostics to stderr for the desktop command-line window."""

    print(f"desktop.compute_node.bridge {message}", file=sys.stderr, flush=True)


def _sleep_with_cancel(seconds: float) -> bool:
    deadline = time.time() + max(seconds, 0)
    while time.time() < deadline:
        if stop_requested():
            return True
        time.sleep(0.1)
    return stop_requested()


def run(args: argparse.Namespace) -> int:
    log_debug(
        f"startup model={args.model} mode={args.mode} relay_url={args.relay_url} relay_port={args.relay_port}"
    )
    runtime_setup = ensure_desktop_llama_runtime(args.mode)
    maybe_reexec_for_runtime_refresh(runtime_setup)
    log_debug(
        "runtime_setup "
        f"mode={args.mode} "
        f"selected_backend={runtime_setup.get('selected_backend', 'cpu')} "
        f"device={runtime_setup.get('detected_device', 'cpu')} "
        f"action={runtime_setup.get('runtime_action', 'none')} "
        f"interpreter={runtime_setup.get('interpreter', sys.executable)} "
        f"llama_module_path={runtime_setup.get('llama_module_path', 'missing')} "
        f"fallback_reason={runtime_setup.get('fallback_reason') or 'none'}",
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
    log_debug(f"relay_target resolved_url={relay_url} resolved_port={relay_port}")

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(
            relay_url=relay_url,
            relay_port=relay_port,
            use_configured_relay_fallbacks=False,
        )
    )

    runtime.model_manager.model_path = args.model
    log_debug(f"model_path configured model={runtime.model_manager.model_path}")
    apply_compute_mode(runtime.model_manager, args.mode)
    log_debug(f"compute_mode_applied requested={args.mode}")

    if not runtime.ensure_model_ready():
        log_debug("model_ready failed")
        emit(
            {
                "type": "error",
                "message": "failed to initialize model runtime",
                "active_relay_url": runtime.relay_client.relay_url,
            }
        )
        return 1
    log_debug("model_ready success")

    diagnostics = compute_mode_diagnostics(runtime.model_manager)
    log_debug(
        "llama_diagnostics "
        f"requested_mode={diagnostics.get('requested_mode')} "
        f"effective_mode={diagnostics.get('effective_mode')} "
        f"backend_available={diagnostics.get('backend_available')} "
        f"backend_selected={diagnostics.get('backend_selected')} "
        f"backend_used={diagnostics.get('backend_used')} "
        f"offloaded_layers={diagnostics.get('offloaded_layers', diagnostics.get('n_gpu_layers'))} "
        f"kv_cache_device={diagnostics.get('kv_cache_device')} "
        f"fallback_reason={diagnostics.get('fallback_reason') or 'none'}"
    )
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
            log_debug("relay_poll begin")
            relay_response = runtime.register_and_poll_once()
            active_relay_url = runtime.relay_client.relay_url
            legacy_payload = is_legacy_relay_payload(relay_response)
            heartbeat_ack = "next_ping_in_x_seconds" in relay_response
            registered = "error" not in relay_response and (legacy_payload or heartbeat_ack)
            log_debug(
                "relay_poll result "
                f"active_relay_url={active_relay_url} "
                f"registered={registered} "
                f"legacy_payload={legacy_payload} "
                f"heartbeat_ack={heartbeat_ack} "
                f"keys={sorted(relay_response.keys())}"
            )

            if not registered:
                if "error" in relay_response:
                    last_error = str(relay_response.get("error", "relay registration failed"))
                    log_debug(f"relay_poll registration_error error={last_error}")
                else:
                    last_error = (
                        "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                        "operator; update relay.py to repo HEAD"
                    )
                    log_debug("relay_poll incompatible_response")
            elif legacy_payload:
                log_debug("relay_request processing legacy payload")
                processed = runtime.process_relay_request(relay_response)
                if not processed:
                    last_error = "failed to process relay request"
                    log_debug("relay_request processing_failed")
                else:
                    last_error = None
                    log_debug("relay_request processing_succeeded")
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

            wait_seconds = float(relay_response.get("next_ping_in_x_seconds", 1))
            log_debug(f"relay_poll sleep seconds={wait_seconds}")
            if _sleep_with_cancel(wait_seconds):
                log_debug("relay_poll cancellation_requested")
                break
    finally:
        log_debug("runtime.stop begin")
        runtime.stop()
        log_debug("runtime.stop complete")

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
        log_debug(f"args_normalized mode={args.mode}")
        return run(args)
    except Exception as exc:  # pragma: no cover - last resort failure handling
        log_debug(f"fatal_error error={exc}")
        emit({"type": "error", "message": f"{EARLY_STARTUP_EXIT_ERROR}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
