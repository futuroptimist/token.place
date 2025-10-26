"""Regression tests for the git send-email validation hook."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

SCRIPT_PATH = Path(__file__).resolve().parents[2] / 'hooks' / 'sendemail-validate.sample'


def _run_hook(function: str, content: str) -> Tuple[int, str, str]:
    """Invoke a hook function with temporary file content."""

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir) / 'input.txt'
        temp_path.write_text(content, encoding='utf-8')

        cmd = f". '{SCRIPT_PATH}' && {function} '{temp_path}'"
        env = os.environ.copy()
        env['TOKEN_PLACE_VALIDATE_IMPORT_ONLY'] = '1'

        result = subprocess.run(
            ['bash', '-c', cmd],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr


def test_validate_cover_letter_rejects_placeholders():
    """Cover letters with placeholder tokens should be rejected."""

    code, _stdout, stderr = _run_hook(
        'validate_cover_letter',
        "Subject: Test\n\nPlease review this change. TODO before sending.",
    )

    assert code != 0, "Expected validate_cover_letter to fail for placeholder tokens"
    assert 'placeholder token' in stderr


def test_validate_cover_letter_accepts_clean_content():
    """Normal cover letters without placeholder tokens should pass."""

    code, stdout, stderr = _run_hook(
        'validate_cover_letter',
        "Subject: Ready for review\n\nThis patch series updates the crypto docs.",
    )

    assert code == 0, f"Hook failed unexpectedly: stdout={stdout!r}, stderr={stderr!r}"
