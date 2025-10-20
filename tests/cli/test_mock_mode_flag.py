import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_cli_mock_mode_flag_enables_mock_llm():
    """The relay CLI should enable mock mode before importing API modules."""

    env = os.environ.copy()
    env.pop("USE_MOCK_LLM", None)
    env.setdefault("PYTHONUNBUFFERED", "1")

    command = [
        sys.executable,
        "relay.py",
        "--use_mock_llm",
        "--host",
        "127.0.0.1",
        "--port",
        "5065",
    ]

    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    output_lines: list[str] = []
    saw_mock_log = False
    saw_models_log = False

    selector = selectors.DefaultSelector()
    try:
        deadline = time.time() + 30
        assert process.stdout is not None  # mypy guard
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.time() < deadline:
            events = selector.select(timeout=0.5)
            if not events:
                if process.poll() is not None:
                    break
                continue

            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    continue

                output_lines.append(line)
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    message = line
                else:
                    message = payload.get("message", "")

                if "mock.llm.enabled" in message:
                    saw_mock_log = True
                if "API v1 Models module loaded with USE_MOCK_LLM=True" in message:
                    saw_models_log = True
                    break
            if saw_models_log:
                break

        assert saw_mock_log, "Expected mock.llm.enabled log in relay output"
        assert saw_models_log, (
            "API models module did not report USE_MOCK_LLM=True. Output was:\n" + "".join(output_lines)
        )
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

