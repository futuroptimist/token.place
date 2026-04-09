#!/usr/bin/env python3
"""NDJSON inference bridge for desktop local inference via shared Python runtime."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator


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


def _normalize_stream_chunk(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk

    for attr in ("to_dict", "model_dump", "dict"):
        handler = getattr(chunk, attr, None)
        if callable(handler):
            try:
                normalized = handler()
            except TypeError:
                continue
            if isinstance(normalized, dict):
                return normalized

    if hasattr(chunk, "__dict__") and isinstance(chunk.__dict__, dict):
        return chunk.__dict__

    return {}


def _iter_text_deltas(stream: Iterable[Any]) -> Iterator[str]:
    for raw_chunk in stream:
        chunk = _normalize_stream_chunk(raw_chunk)
        if not chunk:
            continue

        choices = chunk.get("choices") or []
        if not choices:
            continue

        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue

        content_piece = delta.get("content")
        if content_piece:
            yield content_piece


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="token.place desktop inference bridge")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--token-delay-ms", type=float, default=0.0)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        emit({"type": "error", "code": "bad_model", "message": "model path not found"})
        return 2

    try:
        from utils.llm.model_manager import get_model_manager

        manager = get_model_manager()
        manager.model_path = str(model_path)
        if args.mode.lower() == "cpu":
            manager.default_n_gpu_layers = 0
        elif args.mode.lower() in {"cuda", "metal", "auto"}:
            manager.default_n_gpu_layers = -1

        llm = manager.get_llm_instance()
        if llm is None:
            emit(
                {
                    "type": "error",
                    "code": "init_failed",
                    "message": "unable to initialize model runtime",
                }
            )
            return 1

        emit({"type": "started"})

        completion = llm.create_chat_completion(
            messages=[{"role": "user", "content": args.prompt}],
            max_tokens=manager.config.get("model.max_tokens", 512),
            temperature=manager.config.get("model.temperature", 0.7),
            top_p=manager.config.get("model.top_p", 0.9),
            stop=manager.config.get("model.stop_tokens", []),
            stream=True,
        )

        for text in _iter_text_deltas(completion):
            if canceled_requested():
                emit({"type": "canceled"})
                return 0
            emit({"type": "token", "text": text})
            if args.token_delay_ms > 0:
                time.sleep(args.token_delay_ms / 1000.0)

        if canceled_requested():
            emit({"type": "canceled"})
            return 0

        emit({"type": "done"})
        return 0
    except ModuleNotFoundError as exc:
        emit(
            {
                "type": "error",
                "code": "missing_dependency",
                "message": (
                    "Missing Python dependency for local inference "
                    f"({exc}). Run `pip install -r requirements.txt`."
                ),
            }
        )
        return 1
    except Exception as exc:  # pragma: no cover - defensive bridge error handling
        emit({"type": "error", "code": "inference_failed", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
