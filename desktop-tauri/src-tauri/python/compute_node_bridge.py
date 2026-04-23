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


def _relay_response_summary(relay_response: Dict[str, Any]) -> str:
    """Return a compact summary string for relay registration diagnostics."""

    if not isinstance(relay_response, dict):
        return f"non-dict response type={type(relay_response).__name__}"

    keys = sorted(relay_response.keys())
    has_payload = all(
        key in relay_response for key in ("client_public_key", "chat_history", "cipherkey", "iv")
    )
    has_api_v1_payload = isinstance(relay_response.get("api_v1_request"), dict)
    has_heartbeat = "next_ping_in_x_seconds" in relay_response
    relay_error = _relay_error_message(relay_response)
    wait_seconds = relay_response.get("next_ping_in_x_seconds", "missing")

    return (
        f"keys={keys} has_legacy_payload={has_payload} has_api_v1_payload={has_api_v1_payload} "
        f"has_heartbeat={has_heartbeat} wait={wait_seconds} "
        f"error={relay_error or 'none'}"
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
            is_legacy_relay_payload,
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

    print("desktop.compute_node_bridge.model_init.start", file=sys.stderr)
    if not runtime.ensure_model_ready():
        emit(
            {
                "type": "error",
                "message": "failed to initialize model runtime",
                "active_relay_url": runtime.relay_client.relay_url,
            }
        )
        print("desktop.compute_node_bridge.model_init.failed", file=sys.stderr)
        return 1

    print("desktop.compute_node_bridge.model_init.ready", file=sys.stderr)
    diagnostics = compute_mode_diagnostics(runtime.model_manager)
    print(_runtime_diagnostics_summary(diagnostics), file=sys.stderr)
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
        }
    )

    try:
        while not stop_requested():
            relay_response = runtime.register_and_poll_once()
            active_relay_url = runtime.relay_client.relay_url
            legacy_payload = is_legacy_relay_payload(relay_response)
            api_v1_payload = isinstance(relay_response.get("api_v1_request"), dict)
            heartbeat_ack = "next_ping_in_x_seconds" in relay_response
            relay_error = _relay_error_message(relay_response)
            registered = relay_error is None and (legacy_payload or api_v1_payload or heartbeat_ack)

            print(
                "desktop.compute_node_bridge.relay_poll "
                f"relay={_sanitize_relay_target(active_relay_url)} registered={registered} "
                f"legacy_payload={legacy_payload} api_v1_payload={api_v1_payload} "
                f"heartbeat_ack={heartbeat_ack} "
                f"summary={_relay_response_summary(relay_response)}",
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
            elif legacy_payload or api_v1_payload:
                payload_kind = "api_v1" if api_v1_payload else "legacy"
                print(
                    "desktop.compute_node_bridge.process_request.start "
                    f"kind={payload_kind} stream={relay_response.get('stream') is True}",
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
                        "desktop.compute_node_bridge.process_request.ok",
                        file=sys.stderr,
                    )
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
