#!/usr/bin/env python3
"""Tiny NDJSON sidecar used by the Tauri MVP for local streaming and cancellation tests."""

import argparse
import json
import os
import select
import sys
import time


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def canceled_requested() -> bool:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return False
    line = sys.stdin.readline().strip()
    if not line:
        return False
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return False
    return msg.get("type") == "cancel"


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
