"""Integration tests for relay CLI flags."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.integration
def test_cli_use_mock_llm_enables_mock_mode() -> None:
    """`--use_mock_llm` should activate mock mode before the app imports."""

    env = os.environ.copy()
    env.pop("USE_MOCK_LLM", None)
    env["TOKENPLACE_LOG_LEVEL"] = "ERROR"

    script = textwrap.dedent(
        """
        import os
        import sys

        sys.argv = ["relay.py"] + sys.argv[1:]

        import relay  # noqa: F401 - imported for side effects
        from api.v1 import models

        print(int(models.USE_MOCK_LLM))
        """
    ).strip()

    result = subprocess.run(
        [sys.executable, "-c", script, "--use_mock_llm"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    stdout = result.stdout.strip().splitlines()
    assert stdout, "CLI helper did not produce any output"
    assert stdout[-1] == "1"
