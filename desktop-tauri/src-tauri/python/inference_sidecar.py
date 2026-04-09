#!/usr/bin/env python3
"""NDJSON inference sidecar backed by the shared Python model runtime."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

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


def _normalise_stream_chunk(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk

    for attr in ("to_dict", "model_dump", "dict"):
        handler = getattr(chunk, attr, None)
        if callable(handler):
            try:
                normalised = handler()
            except TypeError:
                continue
            if isinstance(normalised, dict):
                return normalised

    if hasattr(chunk, "__dict__") and isinstance(chunk.__dict__, dict):
        return chunk.__dict__

    return {}


def _iter_text_deltas(completion: Any) -> Iterable[str]:
    if isinstance(completion, dict):
        content = ((completion.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(content, str) and content:
            yield content
        return

    for raw_chunk in completion:
        chunk = _normalise_stream_chunk(raw_chunk)
        if not chunk:
            continue

        choices = chunk.get("choices") or []
        if not choices:
            continue

        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield content

        if choice.get("finish_reason"):
            break


def _apply_mode_overrides(manager: Any, mode: str) -> None:
    if mode == "cpu":
        manager.default_n_gpu_layers = 0


def run_inference(
    model_path: str,
    prompt: str,
    mode: str,
    *,
    emit_fn: Callable[[Dict[str, Any]], None],
    canceled_fn: Callable[[], bool],
) -> int:
    if not os.path.exists(model_path):
        emit_fn({"type": "error", "code": "bad_model", "message": "model path not found"})
        return 2

    try:
        from utils.llm.model_manager import get_model_manager
    except ModuleNotFoundError as exc:
        emit_fn(
            {
                "type": "error",
                "code": "missing_dependency",
                "message": f"Missing Python dependency ({exc})",
            }
        )
        return 2

    manager = get_model_manager()
    manager.model_path = model_path
    _apply_mode_overrides(manager, mode)

    llm = manager.get_llm_instance()
    if llm is None:
        emit_fn(
            {
                "type": "error",
                "code": "init_failed",
                "message": "unable to initialize model runtime",
            }
        )
        return 2

    emit_fn({"type": "started"})

    messages = [{"role": "user", "content": prompt}]
    try:
        completion = llm.create_chat_completion(
            messages=messages,
            max_tokens=manager.config.get("model.max_tokens", 512),
            temperature=manager.config.get("model.temperature", 0.7),
            top_p=manager.config.get("model.top_p", 0.9),
            stop=manager.config.get("model.stop_tokens", []),
            stream=True,
        )

        for text in _iter_text_deltas(completion):
            if canceled_fn():
                emit_fn({"type": "canceled"})
                return 0
            emit_fn({"type": "token", "text": text})

        if canceled_fn():
            emit_fn({"type": "canceled"})
            return 0

        emit_fn({"type": "done"})
        return 0
    except Exception as exc:  # pragma: no cover - defensive sidecar bridge handling
        emit_fn({"type": "error", "code": "inference_failed", "message": str(exc)})
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop inference sidecar")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()

    return run_inference(
        args.model,
        args.prompt,
        args.mode.lower(),
        emit_fn=emit,
        canceled_fn=canceled_requested,
    )


if __name__ == "__main__":
    raise SystemExit(main())
