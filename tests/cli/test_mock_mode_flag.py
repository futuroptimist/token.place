from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_pythonpath(env: dict[str, str]) -> str:
    segments = [str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        segments.append(existing)
    return os.pathsep.join(segments)


@pytest.mark.integration
def test_cli_flag_enables_mock_mode() -> None:
    """`--use_mock_llm` should enable mock mode before importing the API stack."""

    script = """
import importlib
import json
import os
import sys

os.environ.pop("USE_MOCK_LLM", None)
sys.argv = ["relay.py", "--use_mock_llm"]

import relay  # noqa: F401  # Import triggers application setup

models = importlib.import_module("api.v1.models")
print(json.dumps({
    "env": os.environ.get("USE_MOCK_LLM"),
    "models": bool(models.USE_MOCK_LLM),
}))
"""

    env = os.environ.copy()
    env.pop("USE_MOCK_LLM", None)
    env["TOKENPLACE_LOG_LEVEL"] = "ERROR"
    env["PYTHONPATH"] = _build_pythonpath(env)

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(stdout_lines[-1])

    assert payload["env"] == "1"
    assert payload["models"] is True


@pytest.mark.integration
def test_cli_flag_defaults_to_real_mode() -> None:
    """Without `--use_mock_llm` the CLI should keep the real LLM wiring."""

    script = """
import importlib
import json
import os
import sys

os.environ.pop("USE_MOCK_LLM", None)
sys.argv = ["relay.py"]

import relay  # noqa: F401  # Import triggers application setup

models = importlib.import_module("api.v1.models")
print(json.dumps({
    "env": os.environ.get("USE_MOCK_LLM"),
    "models": bool(models.USE_MOCK_LLM),
}))
"""

    env = os.environ.copy()
    env.pop("USE_MOCK_LLM", None)
    env["TOKENPLACE_LOG_LEVEL"] = "ERROR"
    env["PYTHONPATH"] = _build_pythonpath(env)

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(stdout_lines[-1])

    assert payload["env"] is None
    assert payload["models"] is False
