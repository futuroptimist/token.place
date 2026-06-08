"""Integration test verifying the OpenAI JavaScript SDK can talk to token.place."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_NODE_CMD = ["npx", "ts-node", "--project", str(REPO_ROOT / "tsconfig.json")]


def _allocate_loopback_port() -> int:
    """Reserve and release an ephemeral loopback port for the relay fixture."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _captured_process_output(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    return stdout or "", stderr or ""


def _format_relay_failure(message: str, proc: subprocess.Popen[str]) -> str:
    stdout, stderr = _captured_process_output(proc)
    return (
        f"{message}\n"
        f"returncode={proc.returncode}\n"
        f"STDOUT:\n{stdout[-4000:]}\n"
        f"STDERR:\n{stderr[-4000:]}"
    )


@contextmanager
def start_relay_with_mock() -> Iterator[str]:
    """Start the relay in mock-LLM mode for the duration of the test."""
    port = _allocate_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"
    cmd = [
        sys.executable,
        "relay.py",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--use_mock_llm",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        for _ in range(15):
            returncode = proc.poll()
            if returncode is not None:
                raise RuntimeError(
                    _format_relay_failure(
                        "relay exited before becoming healthy for OpenAI JS SDK integration test",
                        proc,
                    )
                )
            try:
                response = requests.get(f"{base_url}/v1/health", timeout=1)
                if response.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            proc.terminate()
            raise RuntimeError(
                _format_relay_failure(
                    "relay failed to start for OpenAI JS SDK integration test", proc
                )
            )

        yield base_url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def ensure_js_sdk_dependencies_installed() -> None:
    """Install the JavaScript dependencies needed for the SDK test if missing."""

    # The OpenAI package is the critical dependency that signals `npm install` ran.
    openai_package = REPO_ROOT / "node_modules" / "openai"
    if openai_package.exists():
        return

    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
    env.setdefault("npm_config_audit", "false")
    env.setdefault("npm_config_fund", "false")

    commands = [["npm", "ci"], ["npm", "install"]]
    errors: list[str] = []

    for command in commands:
        install_result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        if install_result.returncode == 0:
            return

        errors.append(
            "Failed command: "
            + " ".join(command)
            + "\nSTDOUT:\n"
            + install_result.stdout
            + "\nSTDERR:\n"
            + install_result.stderr
        )

    pytest.fail(
        "Failed to install JavaScript dependencies for the OpenAI SDK test.\n"
        + "\n\n".join(errors)
    )


@pytest.mark.integration
@pytest.mark.js
def test_openai_javascript_sdk_can_call_token_place(tmp_path: Path) -> None:
    """Run the TypeScript OpenAI SDK test against the local relay."""
    with start_relay_with_mock() as base_url:
        ensure_js_sdk_dependencies_installed()

        env = os.environ.copy()
        env.setdefault("TOKEN_PLACE_BASE_URL", f"{base_url}/v1")
        env.setdefault("TOKEN_PLACE_API_KEY", "test")
        env.setdefault("TOKEN_PLACE_MODEL", "gpt-5-chat-latest")

        cmd = [
            *TS_NODE_CMD,
            str(REPO_ROOT / "tests" / "test_openai_js_sdk.ts"),
        ]

        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        if result.returncode != 0:
            pytest.fail(
                "OpenAI JavaScript SDK test failed:\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        assert "OpenAI JavaScript SDK integration test passed" in result.stdout
