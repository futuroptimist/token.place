#!/usr/bin/env python3
"""Fake llama.cpp sidecar for MVP and tests.

Outputs NDJSON events and supports prompt/model arguments.
"""

import argparse
import json
import sys
import time


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()

    emit({"type": "started"})
    if args.fail:
        emit({"type": "error", "code": "mock_failure", "message": "Mock sidecar failure"})
        return 1

    response = f"[mode={args.mode}] {args.prompt[::-1]}"
    for token in response.split(" "):
        emit({"type": "token", "text": token + " "})
        time.sleep(0.01)

    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
