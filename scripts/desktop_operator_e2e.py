#!/usr/bin/env python3
"""End-to-end smoke test for desktop compute-node bridge against a live relay."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def wait_for_relay(url: str, timeout_s: float = 20.0) -> None:
    import requests

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            response = requests.get(f"{url}/health", timeout=1)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"relay did not become ready at {url} within {timeout_s} seconds")


def run_bridge(relay_url: str, timeout_s: float = 30.0) -> None:
    bridge = Path("desktop-tauri/src-tauri/python/compute_node_bridge.py")
    if not bridge.is_file():
        raise FileNotFoundError(f"missing bridge script: {bridge}")

    with tempfile.NamedTemporaryFile(suffix=".gguf") as model_file:
        env = os.environ.copy()
        env.setdefault("USE_MOCK_LLM", "1")
        env.setdefault("TOKEN_PLACE_ENV", "development")

        process = subprocess.Popen(
            [
                sys.executable,
                str(bridge),
                "--model",
                model_file.name,
                "--mode",
                "cpu",
                "--relay-url",
                relay_url,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        started = False
        registered = False
        errors: list[str] = []
        deadline = time.time() + timeout_s

        try:
            while time.time() < deadline:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue

                payload = json.loads(line)
                event_type = payload.get("type")
                if event_type == "started":
                    started = True
                if payload.get("registered") is True:
                    registered = True
                    break
                if event_type == "error" or payload.get("last_error"):
                    errors.append(payload.get("message") or payload.get("last_error") or "unknown")
                    break

            if process.stdin:
                process.stdin.write('{"type":"cancel"}\n')
                process.stdin.flush()

            process.wait(timeout=10)
        except Exception:
            process.kill()
            process.wait(timeout=5)
            raise

        stderr = process.stderr.read() if process.stderr else ""

        if not started:
            raise AssertionError(
                f"bridge did not emit started event; exit={process.returncode}; stderr={stderr}"
            )
        if not registered:
            raise AssertionError(
                "bridge never reached registered=true; "
                f"errors={errors}; exit={process.returncode}; stderr={stderr}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    wait_for_relay(args.relay_url)
    run_bridge(args.relay_url)
    print("desktop operator bridge e2e passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
