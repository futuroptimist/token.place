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

ensure_runtime_import_paths(__file__)

_stdin_lines: queue.Queue[str] = queue.Queue()
_stdin_reader_started = False
_stdin_reader_lock = threading.Lock()


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
    try:
        from utils.compute_node_runtime import (
            apply_compute_mode,
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

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(
            relay_url=relay_url,
            relay_port=relay_port,
            use_configured_relay_fallbacks=False,
        )
    )

    runtime.model_manager.model_path = args.model
    resolved_mode = apply_compute_mode(runtime.model_manager, args.mode)

    if not runtime.ensure_model_ready():
        emit(
            {
                "type": "error",
                "message": "failed to initialize model runtime",
                "active_relay_url": runtime.relay_client.relay_url,
            }
        )
        return 1

    last_error: Optional[str] = None
    emit(
        {
            "type": "started",
            "running": True,
            "registered": False,
            "active_relay_url": runtime.relay_client.relay_url,
            "backend_mode": resolved_mode,
            "model_path": args.model,
            "last_error": None,
        }
    )

    try:
        while not stop_requested():
            relay_response = runtime.register_and_poll_once()
            active_relay_url = runtime.relay_client.relay_url
            legacy_payload = is_legacy_relay_payload(relay_response)
            heartbeat_ack = "next_ping_in_x_seconds" in relay_response
            registered = "error" not in relay_response and (legacy_payload or heartbeat_ack)

            if not registered:
                if "error" in relay_response:
                    last_error = str(relay_response.get("error", "relay registration failed"))
                else:
                    last_error = (
                        "relay appears unreachable, old, or incompatible with desktop-v0.1.0 "
                        "operator; update relay.py to repo HEAD"
                    )
            elif legacy_payload:
                processed = runtime.process_relay_request(relay_response)
                if not processed:
                    last_error = "failed to process relay request"
                else:
                    last_error = None
            else:
                last_error = None

            emit(
                {
                    "type": "status",
                    "running": True,
                    "registered": registered,
                    "active_relay_url": active_relay_url,
                    "backend_mode": resolved_mode,
                    "model_path": args.model,
                    "last_error": last_error,
                }
            )

            wait_seconds = float(relay_response.get("next_ping_in_x_seconds", 1))
            if _sleep_with_cancel(wait_seconds):
                break
    finally:
        runtime.stop()

    emit(
        {
            "type": "stopped",
            "running": False,
            "registered": False,
            "active_relay_url": runtime.relay_client.relay_url,
            "backend_mode": resolved_mode,
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
        emit({"type": "error", "message": f"bridge failure: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
