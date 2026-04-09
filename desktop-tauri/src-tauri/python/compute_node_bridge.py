#!/usr/bin/env python3
"""Desktop compute-node bridge using the shared Python compute runtime."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.compute_node_runtime import (  # noqa: E402
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    format_relay_target,
    resolve_relay_port,
    resolve_relay_url,
)

_stdin_lines: queue.Queue[str] = queue.Queue()
_stdin_started = False
_stdin_lock = threading.Lock()


def _start_stdin_reader() -> None:
    global _stdin_started
    with _stdin_lock:
        if _stdin_started:
            return

        def _reader() -> None:
            while True:
                line = sys.stdin.readline()
                if line == "":
                    break
                _stdin_lines.put(line)

        threading.Thread(target=_reader, daemon=True).start()
        _stdin_started = True


def _stop_requested() -> bool:
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
        if payload.get("type") == "stop":
            return True


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _emit_status(
    *,
    registered: bool,
    relay_url: str,
    backend_mode: str,
    model_path: str,
    last_error: Optional[str],
) -> None:
    _emit(
        {
            "type": "status",
            "registered": registered,
            "relay_url": relay_url,
            "backend_mode": backend_mode,
            "model_path": model_path,
            "last_error": last_error,
        }
    )


def _apply_mode(runtime: ComputeNodeRuntime, mode: str) -> str:
    selected = (mode or "auto").lower()
    manager = runtime.model_manager
    if selected == "cpu":
        manager.default_n_gpu_layers = 0
    elif selected in {"metal", "cuda"}:
        manager.default_n_gpu_layers = -1
    return selected


def _configure_env_for_runtime(args: argparse.Namespace) -> tuple[str, Optional[int]]:
    relay_url = resolve_relay_url(args.relay_url)
    relay_port = resolve_relay_port(args.relay_port, relay_url)
    return relay_url, relay_port


def run(args: argparse.Namespace) -> int:
    relay_url, relay_port = _configure_env_for_runtime(args)
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(
            relay_url=relay_url,
            relay_port=relay_port,
        )
    )

    runtime.model_manager.model_path = args.model_path
    backend_mode = _apply_mode(runtime, args.mode)
    runtime.relay_client._streaming_enabled = bool(args.streaming)
    relay_target = format_relay_target(relay_url, relay_port)

    _emit_status(
        registered=False,
        relay_url=relay_target,
        backend_mode=backend_mode,
        model_path=args.model_path,
        last_error=None,
    )

    if not runtime.ensure_model_ready():
        _emit({"type": "error", "message": "model initialization failed"})
        _emit_status(
            registered=False,
            relay_url=relay_target,
            backend_mode=backend_mode,
            model_path=args.model_path,
            last_error="model initialization failed",
        )
        return 1

    registered = False
    last_error: Optional[str] = None

    try:
        while True:
            if _stop_requested():
                break

            sink_response = runtime.register_and_poll_once()
            registered = "error" not in sink_response
            if "error" in sink_response:
                last_error = str(sink_response.get("error"))
                _emit({"type": "error", "message": last_error})
            else:
                last_error = None
                required = {"client_public_key", "chat_history", "cipherkey", "iv"}
                if required.issubset(sink_response):
                    processed = runtime.process_relay_request(sink_response)
                    if not processed:
                        last_error = "request processing failed"
                        _emit({"type": "error", "message": last_error})

            _emit_status(
                registered=registered,
                relay_url=relay_target,
                backend_mode=backend_mode,
                model_path=args.model_path,
                last_error=last_error,
            )

            sleep_seconds = sink_response.get("next_ping_in_x_seconds", 10)
            try:
                delay = max(0.1, float(sleep_seconds))
            except (TypeError, ValueError):
                delay = 10.0

            stop_deadline = time.time() + delay
            while time.time() < stop_deadline:
                if _stop_requested():
                    break
                time.sleep(0.1)
            if _stop_requested():
                break
    finally:
        runtime.stop()
        _emit({"type": "stopped"})

    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="token.place desktop compute-node bridge")
    parser.add_argument("--relay-url", default="https://token.place")
    parser.add_argument("--relay-port", type=int, default=None)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--mode", default="auto", choices=["auto", "metal", "cuda", "cpu"])
    parser.add_argument("--streaming", type=int, default=0)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    args.streaming = bool(args.streaming)
    try:
        return run(args)
    except Exception as exc:
        _emit({"type": "error", "message": f"compute-node bridge failure: {exc}"})
        _emit({"type": "stopped"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
