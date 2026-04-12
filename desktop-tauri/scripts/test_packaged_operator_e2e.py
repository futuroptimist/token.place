#!/usr/bin/env python3
"""End-to-end regression for packaged desktop Python runtime journey."""

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


def create_packaged_layout(tmp_root: Path) -> tuple[Path, Path]:
    resources_root = tmp_root / "resources"
    python_dir = resources_root / "python"
    up_root = resources_root / "_up_" / "_up_"
    python_dir.mkdir(parents=True, exist_ok=True)
    up_root.mkdir(parents=True, exist_ok=True)

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

    shutil.copy2(REPO_ROOT / "config.py", up_root / "config.py")
    shutil.copy2(REPO_ROOT / "encrypt.py", up_root / "encrypt.py")
    shutil.copytree(REPO_ROOT / "utils", up_root / "utils", dirs_exist_ok=True)

    model_path = up_root / "mock.gguf"
    model_path.write_text("mock model artifact for packaged e2e\n", encoding="utf-8")

    return python_dir / "compute_node_bridge.py", python_dir / "inference_sidecar.py"


def read_ndjson_until(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
    predicate,
) -> tuple[list[dict[str, object]], str]:
    assert process.stdout is not None

    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    events: list[dict[str, object]] = []
    raw_output = ""
    buffered = ""
    deadline = time.time() + timeout_seconds

    try:
        while time.time() < deadline:
            timeout = max(0.0, min(0.25, deadline - time.time()))
            ready = selector.select(timeout=timeout)
            if not ready and process.poll() is not None:
                break

            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    continue
                text = chunk.decode("utf-8", errors="replace")
                raw_output += text
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
                    events.append(payload)
                    if predicate(payload):
                        return events, raw_output

        raise RuntimeError(f"timed out waiting for expected event; output={raw_output[-2000:]}")
    finally:
        selector.close()


def main() -> int:
    relay_port = reserve_free_port()
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"

    with tempfile.TemporaryDirectory(prefix="token-place-packaged-e2e-") as tmpdir:
        compute_bridge, inference_sidecar = create_packaged_layout(Path(tmpdir))
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

        compute_proc: subprocess.Popen[str] | None = None
        inference_proc: subprocess.Popen[str] | None = None
        try:
            wait_for_livez(relay, relay_port)

            compute_proc = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(compute_bridge),
                    "--model",
                    str(Path(tmpdir) / "resources" / "_up_" / "_up_" / "mock.gguf"),
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
                text=True,
            )
            assert compute_proc.stdin is not None

            compute_events, compute_output = read_ndjson_until(
                compute_proc,
                timeout_seconds=25,
                predicate=lambda payload: bool(payload.get("running"))
                and bool(payload.get("registered")),
            )

            if any(event.get("type") == "error" for event in compute_events):
                raise RuntimeError(f"compute bridge error event(s): {compute_events}")
            if "No module named 'utils'" in compute_output:
                raise RuntimeError(f"missing utils import in packaged runtime: {compute_output}")

            inference_proc = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(inference_sidecar),
                    "--model",
                    str(Path(tmpdir) / "resources" / "_up_" / "_up_" / "mock.gguf"),
                    "--mode",
                    "cpu",
                    "--prompt",
                    "Say hello from the packaged desktop e2e test.",
                ],
                cwd=tmpdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            inference_events, inference_output = read_ndjson_until(
                inference_proc,
                timeout_seconds=25,
                predicate=lambda payload: payload.get("type") == "done",
            )

            tokens = [
                str(event.get("text", ""))
                for event in inference_events
                if event.get("type") == "token" and event.get("text")
            ]
            if not "".join(tokens).strip():
                raise RuntimeError(
                    "expected non-empty inference token output; "
                    f"events={inference_events}; output={inference_output}"
                )

            compute_proc.stdin.write('{"type":"cancel"}\n')
            compute_proc.stdin.flush()
            compute_proc.wait(timeout=20)
            if compute_proc.returncode != 0:
                raise RuntimeError(f"compute bridge exited non-zero: {compute_output}")

            inference_proc.wait(timeout=20)
            if inference_proc.returncode != 0:
                raise RuntimeError(f"inference sidecar exited non-zero: {inference_output}")

        finally:
            if compute_proc is not None and compute_proc.poll() is None:
                compute_proc.kill()
            if inference_proc is not None and inference_proc.poll() is None:
                inference_proc.kill()
            if relay.poll() is None:
                relay.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
