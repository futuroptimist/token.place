#!/usr/bin/env python3
import json
import sys
import time


def main() -> int:
    line = sys.stdin.readline()
    if not line:
        return 1
    req = json.loads(line)

    sys.stdout.write(json.dumps({"type": "started"}) + "\n")
    sys.stdout.flush()

    prompt = req.get("prompt", "")
    for chunk in [f"echo:{prompt}"[:5], f"echo:{prompt}"[5:]]:
        sys.stdout.write(json.dumps({"type": "token", "text": chunk}) + "\n")
        sys.stdout.flush()
        time.sleep(0.02)

    sys.stdout.write(json.dumps({"type": "done"}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
