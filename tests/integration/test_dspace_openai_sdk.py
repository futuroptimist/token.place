"""Integration test verifying the OpenAI JavaScript SDK can talk to token.place."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.integration.relay_fixture import start_relay_with_mock

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_NODE_CMD = ["npx", "ts-node", "--project", str(REPO_ROOT / "tsconfig.json")]


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
    with start_relay_with_mock("OpenAI JS SDK integration test") as base_url:
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
