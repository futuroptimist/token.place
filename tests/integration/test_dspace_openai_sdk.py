"""Integration test verifying the OpenAI JavaScript SDK can talk to token.place."""

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import requests

API_PORT = 5056
BASE_URL = f"http://localhost:{API_PORT}"
REPO_ROOT = Path(__file__).resolve().parents[2]
TS_NODE_CMD = ["npx", "ts-node", "--project", str(REPO_ROOT / "tsconfig.json")]


@contextmanager
def start_relay_with_mock() -> Iterator[None]:
    """Start the relay in mock-LLM mode for the duration of the test."""
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"
    cmd = [sys.executable, "relay.py", "--port", str(API_PORT)]
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
            try:
                response = requests.get(f"{BASE_URL}/v1/health", timeout=1)
                if response.status_code == 200:
                    break
            except Exception:
                pass
            finally:
                import time

                time.sleep(1)
        else:
            proc.terminate()
            raise RuntimeError("relay failed to start for OpenAI JS SDK integration test")

        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def ensure_js_sdk_dependencies_installed() -> None:
    """Install the JavaScript dependencies needed for the SDK test if missing."""

    # The OpenAI package is the critical dependency that signals `npm install` ran.
    openai_package = REPO_ROOT / "node_modules" / "openai"
    if openai_package.exists():
        return

    install_result = subprocess.run(
        ["npm", "install"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    if install_result.returncode != 0:
        pytest.fail(
            "Failed to install JavaScript dependencies for the OpenAI SDK test.\n"
            f"STDOUT:\n{install_result.stdout}\nSTDERR:\n{install_result.stderr}"
        )


@pytest.mark.integration
@pytest.mark.js
def test_openai_javascript_sdk_can_call_token_place(tmp_path: Path) -> None:
    """Run the TypeScript OpenAI SDK test against the local relay."""
    with start_relay_with_mock():
        ensure_js_sdk_dependencies_installed()

        env = os.environ.copy()
        env.setdefault("TOKEN_PLACE_BASE_URL", f"{BASE_URL}/v1")
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
