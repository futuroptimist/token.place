#!/usr/bin/env python3
"""Tiny NDJSON sidecar used by the Tauri MVP for local streaming and cancellation tests."""

import argparse
import json
import os
import queue
import sys
import threading
import time

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


def emit(payload):
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()

    if not os.path.exists(args.model):
        emit({"type": "error", "code": "bad_model", "message": "model path not found"})
        return 2

    emit({"type": "started"})
    for token in args.prompt.split():
        if canceled_requested():
            emit({"type": "canceled"})
            return 0
        emit({"type": "token", "text": token + " "})
        time.sleep(0.05)

    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
