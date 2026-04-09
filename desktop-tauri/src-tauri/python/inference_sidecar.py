#!/usr/bin/env python3
"""NDJSON inference sidecar that reuses the shared Python model runtime."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


def emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def emit_error(code: str, message: str) -> int:
    emit({"type": "error", "code": code, "message": message})
    return 1


def canceled_requested() -> bool:
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


def _normalize_chunk(chunk: Any) -> Dict[str, Any]:
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


def _stream_content(completion: Iterable[Any]) -> Tuple[str, bool]:
    full_text = []
    emitted = False
    for raw_chunk in completion:
        if canceled_requested():
            emit({"type": "canceled"})
            return "", True

        chunk = _normalize_chunk(raw_chunk)
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = (choices[0] or {}).get("delta") or {}
        if not isinstance(delta, dict):
            continue

        text = delta.get("content")
        if text:
            full_text.append(text)
            emitted = True
            emit({"type": "token", "text": text})

        if (choices[0] or {}).get("finish_reason"):
            break

    return "".join(full_text) if emitted else "", False


def run(args: argparse.Namespace) -> int:
    if not os.path.exists(args.model):
        return emit_error("bad_model", "model path not found")

    try:
        from utils.llm.model_manager import get_model_manager
    except ModuleNotFoundError as exc:
        return emit_error(
            "runtime_unavailable",
            f"Missing Python dependency for local inference ({exc}).",
        )

    manager = get_model_manager()
    manager.model_path = args.model

    if os.getenv("TOKEN_PLACE_USE_FAKE_SIDECAR") == "1":
        manager.use_mock_llm = True

    llm = manager.get_llm_instance()
    if llm is None:
        return emit_error("bad_model", "unable to initialize model runtime")

    emit({"type": "started"})
    if canceled_requested():
        emit({"type": "canceled"})
        return 0

    messages = [{"role": "user", "content": args.prompt}]
    try:
        completion = llm.create_chat_completion(
            messages=messages,
            max_tokens=manager.config.get("model.max_tokens", 512),
            temperature=manager.config.get("model.temperature", 0.7),
            top_p=manager.config.get("model.top_p", 0.9),
            stop=manager.config.get("model.stop_tokens", []),
            stream=True,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime handling
        return emit_error("inference_failed", f"streaming completion failed: {exc}")

    if isinstance(completion, dict):
        text = ((completion.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if text:
            emit({"type": "token", "text": text})
    else:
        _, canceled = _stream_content(completion)
        if canceled:
            return 0

    emit({"type": "done"})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop inference sidecar")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:  # pragma: no cover - last resort error handling
        return emit_error("inference_failed", f"bridge failure: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
