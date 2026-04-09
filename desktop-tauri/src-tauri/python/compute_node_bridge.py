#!/usr/bin/env python3
"""Desktop compute-node bridge for legacy relay /sink -> /source flow."""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests

from encrypt import encrypt_stream_chunk

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.compute_node_runtime import (  # noqa: E402
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    format_relay_target,
    resolve_relay_port,
)

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


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _emit_error(message: str) -> None:
    _emit({"type": "error", "message": message})


def _cancel_requested() -> bool:
    _start_stdin_reader()
    while True:
        try:
            line = _stdin_lines.get_nowait().strip()
        except queue.Empty:
            return False
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "cancel":
            return True


def _normalize_stream_chunk(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk

    for attr in ("to_dict", "model_dump", "dict"):
        handler = getattr(chunk, attr, None)
        if callable(handler):
            try:
                parsed = handler()
            except TypeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    if hasattr(chunk, "__dict__") and isinstance(chunk.__dict__, dict):
        return chunk.__dict__
    return {}


class ComputeNodeBridge:
    def __init__(
        self,
        runtime: ComputeNodeRuntime,
        *,
        stream_enabled: bool,
        mode: str,
        model_path: str,
    ):
        self.runtime = runtime
        self.stream_enabled = stream_enabled
        self.mode = mode
        self.model_path = model_path
        self.last_error = ""
        self.registered = False

    def _status(self) -> Dict[str, Any]:
        relay_client = self.runtime.relay_client
        return {
            "type": "status",
            "registered": self.registered,
            "running": True,
            "active_relay_url": relay_client.relay_url,
            "backend_mode": self.mode,
            "model_path": self.model_path,
            "stream_enabled": self.stream_enabled,
            "last_error": self.last_error,
        }

    def _post_stream_chunk(self, session_id: str, chunk: Dict[str, Any], final: bool = False) -> None:
        response = requests.post(
            f"{self.runtime.relay_client.relay_url}/stream/source",
            json={"session_id": session_id, "chunk": chunk, "final": final},
            headers=self.runtime.relay_client._auth_headers() or None,
            timeout=10,
        )
        response.raise_for_status()

    def _stream_request(self, request_data: Dict[str, Any]) -> bool:
        decrypted = self.runtime.crypto_manager.decrypt_message(request_data)
        if not isinstance(decrypted, list):
            return False

        llm = self.runtime.model_manager.get_llm_instance()
        if llm is None:
            return False

        completion = llm.create_chat_completion(
            messages=decrypted,
            max_tokens=self.runtime.model_manager.config.get("model.max_tokens", 512),
            temperature=self.runtime.model_manager.config.get("model.temperature", 0.7),
            top_p=self.runtime.model_manager.config.get("model.top_p", 0.9),
            stop=self.runtime.model_manager.config.get("model.stop_tokens", []),
            stream=True,
        )

        client_public_key_b64 = request_data["client_public_key"]
        session_id = request_data.get("stream_session_id")
        if not isinstance(session_id, str) or not session_id:
            return self.runtime.process_relay_request(request_data)

        client_key = base64.b64decode(client_public_key_b64)
        stream_session = None

        if isinstance(completion, dict):
            content = (
                completion.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if not content:
                return self.runtime.process_relay_request(request_data)
            ciphertext_dict, encrypted_key, stream_session = encrypt_stream_chunk(
                content.encode("utf-8"),
                client_key,
                session=stream_session,
            )
            payload = {
                "encrypted": True,
                "stream_session_id": session_id,
                "data": {
                    "chat_history": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
                    "iv": base64.b64encode(ciphertext_dict["iv"]).decode("utf-8"),
                    "cipherkey": base64.b64encode(encrypted_key).decode("utf-8") if encrypted_key else None,
                },
            }
            self._post_stream_chunk(session_id, payload, final=True)
            return True

        saw_chunk = False
        for raw_chunk in completion:
            parsed = _normalize_stream_chunk(raw_chunk)
            choices = parsed.get("choices") or []
            if not choices:
                continue
            delta = (choices[0] or {}).get("delta") or {}
            content = delta.get("content") if isinstance(delta, dict) else None
            if content:
                saw_chunk = True
                ciphertext_dict, encrypted_key, stream_session = encrypt_stream_chunk(
                    content.encode("utf-8"),
                    client_key,
                    session=stream_session,
                )
                encrypted_payload = {
                    "chat_history": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
                    "iv": base64.b64encode(ciphertext_dict["iv"]).decode("utf-8"),
                }
                if encrypted_key is not None:
                    encrypted_payload["cipherkey"] = base64.b64encode(encrypted_key).decode("utf-8")

                envelope = {
                    "encrypted": True,
                    "stream_session_id": session_id,
                    "data": encrypted_payload,
                }
                self._post_stream_chunk(session_id, envelope)

            if (choices[0] or {}).get("finish_reason"):
                break

        if saw_chunk:
            self._post_stream_chunk(session_id, {"event": "done"}, final=True)
            return True

        return self.runtime.process_relay_request(request_data)

    def process_sink_payload(self, sink_payload: Dict[str, Any]) -> bool:
        requests_to_process = []
        if all(
            field in sink_payload
            for field in ("client_public_key", "chat_history", "cipherkey", "iv")
        ):
            requests_to_process.append(sink_payload)

        batch = sink_payload.get("batch")
        if isinstance(batch, list):
            for entry in batch:
                if isinstance(entry, dict):
                    requests_to_process.append(entry)

        overall_success = True
        for request_data in requests_to_process:
            should_stream = bool(request_data.get("stream")) and self.stream_enabled
            if should_stream:
                success = self._stream_request(request_data)
            else:
                success = self.runtime.process_relay_request(request_data)
            overall_success = overall_success and success
        return overall_success

    def run(self) -> int:
        if not self.runtime.ensure_model_ready():
            _emit_error("model_not_ready")
            return 1

        _emit(self._status())
        while True:
            if _cancel_requested():
                self.runtime.stop()
                _emit({"type": "stopped"})
                return 0

            try:
                sink_payload = self.runtime.register_and_poll_once()
                self.registered = True
                if isinstance(sink_payload, dict):
                    if "error" in sink_payload:
                        self.last_error = str(sink_payload.get("error", ""))
                    else:
                        self.last_error = ""
                        self.process_sink_payload(sink_payload)
                    _emit(self._status())
                    sleep_seconds = sink_payload.get("next_ping_in_x_seconds", 1)
                else:
                    sleep_seconds = 1
            except Exception as exc:  # pragma: no cover
                self.last_error = str(exc)
                _emit_error(self.last_error)
                _emit(self._status())
                sleep_seconds = 2

            time.sleep(max(float(sleep_seconds), 0.05))


def _apply_compute_mode(model_manager: Any, mode: str) -> None:
    selected = (mode or "auto").lower()
    if selected == "cpu":
        model_manager.default_n_gpu_layers = 0
    elif selected in {"metal", "cuda"}:
        model_manager.default_n_gpu_layers = -1


def _build_runtime(relay_url: str, relay_port: Optional[int]) -> ComputeNodeRuntime:
    return ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(relay_url=relay_url, relay_port=relay_port),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop compute node bridge")
    parser.add_argument("--relay-url", required=True)
    parser.add_argument("--relay-port", type=int, default=None)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto", choices=["auto", "metal", "cuda", "cpu"])
    parser.add_argument("--stream-enabled", action="store_true")
    args = parser.parse_args()

    relay_port = resolve_relay_port(args.relay_port, args.relay_url)
    runtime = _build_runtime(args.relay_url, relay_port)

    runtime.model_manager.model_path = args.model
    _apply_compute_mode(runtime.model_manager, args.mode)

    target = format_relay_target(args.relay_url, relay_port)
    _emit({"type": "started", "relay_target": target})

    bridge = ComputeNodeBridge(
        runtime,
        stream_enabled=args.stream_enabled,
        mode=args.mode,
        model_path=args.model,
    )
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
