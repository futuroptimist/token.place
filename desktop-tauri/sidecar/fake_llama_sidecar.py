#!/usr/bin/env python3
import argparse
import json
import sys
import time


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--backend", required=True)
    args = parser.parse_args()

    line = sys.stdin.readline()
    if not line:
        emit({"type": "error", "code": "missing_input", "message": "missing infer payload"})
        return 1

    payload = json.loads(line)
    prompt = payload.get("prompt", "")
    emit({"type": "started", "backend": args.backend})

    words = prompt.split(" ")
    for index, word in enumerate(words):
        emit({"type": "token", "text": word + (" " if index < len(words) - 1 else "")})
        time.sleep(0.02)

    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
