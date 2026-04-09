#!/usr/bin/env python3
"""Desktop compute-node bridge for legacy relay /sink + /source flow."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.compute_node_runtime import (  # noqa: E402
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    first_env,
    format_relay_target,
    resolve_relay_port,
    resolve_relay_url,
)


def emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def emit_status(**payload: Any) -> None:
    emit({"type": "status", **payload})


def read_stdin_stop_signal(timeout_seconds: float = 0.1) -> bool:
    """Return True when a stop command has been sent on stdin."""

    import select

    readable, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
    if not readable:
        return False

    line = sys.stdin.readline()
    if line == "":
        return False

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False

    return payload.get("type") == "stop"


def bridge_mode_from_env(fallback: str | None = None) -> str:
    mode = first_env(["TOKEN_PLACE_DESKTOP_BRIDGE_MODE", "TOKENPLACE_DESKTOP_BRIDGE_MODE"])
    if mode:
        return mode
    if os.environ.get("TOKEN_PLACE_USE_FAKE_SIDECAR") == "1":
        return "mock"
    return fallback or "llama-cpp"


def run(args: argparse.Namespace) -> int:
    relay_url = resolve_relay_url(args.relay_url)
    relay_port = resolve_relay_port(args.relay_port, relay_url)

    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url=relay_url, relay_port=relay_port)
    )
    runtime.model_manager.model_path = args.model

    mode = bridge_mode_from_env(args.mode)
    relay_target = format_relay_target(relay_url, relay_port)
    last_error = ""

    emit_status(
        state="initializing",
        registered=False,
        running=False,
        relay_url=relay_target,
        backend_mode=mode,
        model_path=args.model,
        last_error="",
    )

    if not runtime.ensure_model_ready():
        last_error = "model runtime failed to initialize"
        emit_status(
            state="failed",
            registered=False,
            running=False,
            relay_url=relay_target,
            backend_mode=mode,
            model_path=args.model,
            last_error=last_error,
        )
        return 1

    emit_status(
        state="running",
        registered=False,
        running=True,
        relay_url=relay_target,
        backend_mode=mode,
        model_path=args.model,
        last_error="",
    )

    try:
        while True:
            if read_stdin_stop_signal(timeout_seconds=0):
                break

            relay_response = runtime.register_and_poll_once()
            sleep_seconds = float(relay_response.get("next_ping_in_x_seconds", 10))

            if "error" in relay_response:
                last_error = str(relay_response["error"])
                emit_status(
                    state="running",
                    registered=False,
                    running=True,
                    relay_url=runtime.relay_client.relay_url,
                    backend_mode=mode,
                    model_path=args.model,
                    last_error=last_error,
                )
            else:
                last_error = ""
                emit_status(
                    state="running",
                    registered=True,
                    running=True,
                    relay_url=runtime.relay_client.relay_url,
                    backend_mode=mode,
                    model_path=args.model,
                    last_error="",
                )

                required = {"client_public_key", "chat_history", "cipherkey", "iv"}
                if required.issubset(relay_response):
                    ok = runtime.process_relay_request(relay_response)
                    if not ok:
                        last_error = "failed to process relay request"
                        emit_status(
                            state="running",
                            registered=True,
                            running=True,
                            relay_url=runtime.relay_client.relay_url,
                            backend_mode=mode,
                            model_path=args.model,
                            last_error=last_error,
                        )

            if read_stdin_stop_signal(timeout_seconds=min(sleep_seconds, 0.5)):
                break

            if sleep_seconds > 0.5:
                remaining = sleep_seconds - 0.5
                while remaining > 0:
                    if read_stdin_stop_signal(timeout_seconds=min(remaining, 0.5)):
                        return 0
                    remaining -= 0.5
    finally:
        runtime.stop()

    emit_status(
        state="stopped",
        registered=False,
        running=False,
        relay_url=runtime.relay_client.relay_url,
        backend_mode=mode,
        model_path=args.model,
        last_error=last_error,
    )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop compute node bridge")
    parser.add_argument("--model", required=True)
    parser.add_argument("--relay-url", default="https://token.place")
    parser.add_argument("--relay-port", type=int, default=None)
    parser.add_argument("--mode", default="auto")
    args = parser.parse_args()

    try:
        return run(args)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover
        emit_status(
            state="failed",
            registered=False,
            running=False,
            relay_url=args.relay_url,
            backend_mode=bridge_mode_from_env(),
            model_path=args.model,
            last_error=str(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
