#!/usr/bin/env python3
"""End-to-end regression for packaged operator Python bridge imports."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_livez(relay: subprocess.Popen[str], port: int, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/livez", timeout=1) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - retry loop
            last_error = exc

        if relay.poll() is not None:
            stderr = relay.stderr.read() if relay.stderr else ""
            stdout = relay.stdout.read() if relay.stdout else ""
            raise RuntimeError(
                f"relay exited early with code {relay.returncode}; stdout={stdout}; stderr={stderr}"
            )
        time.sleep(0.25)

    raise RuntimeError(f"relay did not become live on port {port}: {last_error}")


def create_packaged_layout(tmp_root: Path) -> Path:
    resources_root = tmp_root / "resources"
    python_dir = resources_root / "python"
    python_dir.mkdir(parents=True, exist_ok=True)

    for filename in (
        "compute_node_bridge.py",
        "inference_sidecar.py",
        "model_bridge.py",
        "path_bootstrap.py",
    ):
        shutil.copy2(
            REPO_ROOT / "desktop-tauri" / "src-tauri" / "python" / filename,
            python_dir / filename,
        )

    shutil.copy2(REPO_ROOT / "config.py", resources_root / "config.py")
    shutil.copy2(REPO_ROOT / "encrypt.py", resources_root / "encrypt.py")
    shutil.copytree(REPO_ROOT / "utils", resources_root / "utils", dirs_exist_ok=True)

    return python_dir / "compute_node_bridge.py"


def main() -> int:
    relay_port = reserve_free_port()
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"

    with tempfile.TemporaryDirectory(prefix="token-place-packaged-e2e-") as tmpdir:
        bridge_script = create_packaged_layout(Path(tmpdir))

        relay = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(REPO_ROOT / "relay.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(relay_port),
                "--use_mock_llm",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        bridge: subprocess.Popen[bytes] | None = None
        bridge_output = ""
        selector: selectors.BaseSelector | None = None
        try:
            wait_for_livez(relay, relay_port)
            bridge = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(bridge_script),
                    "--model",
                    "mock.gguf",
                    "--mode",
                    "cpu",
                    "--relay-url",
                    f"http://127.0.0.1:{relay_port}",
                ],
                cwd=tmpdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )

            assert bridge.stdout is not None
            assert bridge.stdin is not None

            os.set_blocking(bridge.stdout.fileno(), False)
            selector = selectors.DefaultSelector()
            selector.register(bridge.stdout, selectors.EVENT_READ)

            saw_started = False
            buffered = ""
            deadline = time.time() + 20

            while time.time() < deadline:
                timeout = max(0.0, min(0.25, deadline - time.time()))
                events = selector.select(timeout=timeout)
                if not events and bridge.poll() is not None:
                    break

                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        continue
                    text = chunk.decode("utf-8", errors="replace")
                    bridge_output += text
                    buffered += text

                    while "\n" in buffered:
                        line, buffered = buffered.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if payload.get("type") == "error":
                            raise RuntimeError(f"bridge emitted error event: {payload}")
                        if payload.get("type") == "started" and payload.get("running") is True:
                            saw_started = True
                            bridge.stdin.write(b'{"type":"cancel"}\n')
                            bridge.stdin.flush()
                            break

                if saw_started:
                    break

            if not saw_started:
                raise RuntimeError(
                    "bridge did not emit started/running event; output="
                    f"{bridge_output[-2000:]}"
                )

            bridge.wait(timeout=20)
            if bridge.returncode != 0:
                raise RuntimeError(f"bridge exited non-zero ({bridge.returncode}): {bridge_output}")

        finally:
            if selector is not None:
                selector.close()
            if bridge is not None and bridge.poll() is None:
                bridge.kill()
            if relay.poll() is None:
                relay.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
