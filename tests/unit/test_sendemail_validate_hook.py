"""Regression tests for the git send-email validation hook."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

import pytest
PATCH_TEMPLATE_HEADER = """From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: token.place Hook Tester <hooks@token.place>
Date: Thu, 10 Feb 2025 12:00:00 +0000
Subject: {subject}

{body}
"""

PATCH_DIFF_SECTION = """---
 docs/example.txt | 1 +
 1 file changed, 1 insertion(+)
 create mode 100644 docs/example.txt

diff --git a/docs/example.txt b/docs/example.txt
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/docs/example.txt
@@ -0,0 +1 @@
{hunk_line}
"""

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


def _run_patch(content: str) -> Tuple[int, str, str]:
    """Invoke validate_patch with provided patch content."""

    return _run_hook('validate_patch', content)


def test_validate_cover_letter_rejects_placeholders():
    """Cover letters with placeholder tokens should be rejected."""

    code, _stdout, stderr = _run_hook(
        'validate_cover_letter',
        "Subject: Test\n\nPlease review this change. todo before sending.",
    )

    assert code != 0, "Expected validate_cover_letter to fail for placeholder tokens"
    assert 'placeholder token (TODO) detected in cover letter at line 3' in stderr


def test_validate_cover_letter_accepts_clean_content():
    """Normal cover letters without placeholder tokens should pass."""

    code, stdout, stderr = _run_hook(
        'validate_cover_letter',
        "Subject: Ready for review\n\nThis patch series updates the crypto docs.",
    )

    assert code == 0, f"Hook failed unexpectedly: stdout={stdout!r}, stderr={stderr!r}"


def test_validate_patch_requires_signed_off_by_trailer():
    """Patches must include a Signed-off-by trailer."""

    patch = (
        PATCH_TEMPLATE_HEADER.format(
            subject='[PATCH] Add docs note',
            body='Add docs note without a trailer.\n',
        )
        + PATCH_DIFF_SECTION.format(hunk_line='+Example addition\n')
    )

    code, _stdout, stderr = _run_patch(patch)

    assert code != 0
    assert 'Signed-off-by' in stderr


def test_validate_patch_rejects_placeholder_tokens():
    """Added TODO-style tokens should be rejected."""

    patch = (
        PATCH_TEMPLATE_HEADER.format(
            subject='[PATCH] Add reminder comment',
            body='Add reminder comment.\n\nSigned-off-by: Hook Tester <hooks@token.place>\n',
        )
        + PATCH_DIFF_SECTION.format(hunk_line='+TODO: fill this in later\n')
    )

    code, _stdout, stderr = _run_patch(patch)

    assert code != 0
    assert 'placeholder token (TODO) detected in patch body at line 21' in stderr


@pytest.mark.parametrize('placeholder_token', ['TODO', 'FIXME', 'WIP', 'TBD'])
def test_validate_patch_rejects_placeholder_commit_messages(placeholder_token: str):
    """Commit messages with placeholder markers should fail validation."""

    patch = (
        PATCH_TEMPLATE_HEADER.format(
            subject='[PATCH] Add reminder comment',
            body=f'{placeholder_token}: replace with details.\n\n'
            'Signed-off-by: Hook Tester <hooks@token.place>\n',
        )
        + PATCH_DIFF_SECTION.format(hunk_line='+Example addition\n')
    )

    code, _stdout, stderr = _run_patch(patch)

    assert code != 0
    assert f'placeholder token ({placeholder_token}) detected in patch message at line 6' in stderr


def test_validate_patch_rejects_placeholder_subjects():
    """Patch subjects with placeholder tokens should fail immediately."""

    patch = (
        PATCH_TEMPLATE_HEADER.format(
            subject='[PATCH] TODO: update docs',
            body='Add docs update.\n\nSigned-off-by: Hook Tester <hooks@token.place>\n',
        )
        + PATCH_DIFF_SECTION.format(hunk_line='+Example addition\n')
    )

    code, _stdout, stderr = _run_patch(patch)

    assert code != 0
    assert 'placeholder token (TODO) detected in patch subject' in stderr


def test_validate_patch_accepts_clean_patch():
    """A well-formed patch should pass validation."""

    patch = (
        PATCH_TEMPLATE_HEADER.format(
            subject='[PATCH] Add example content',
            body='Document the new behaviour.\n\nSigned-off-by: Hook Tester <hooks@token.place>\n',
        )
        + PATCH_DIFF_SECTION.format(hunk_line='+Example addition\n')
    )

    code, stdout, stderr = _run_patch(patch)

    assert code == 0, f"validate_patch failed: stdout={stdout!r}, stderr={stderr!r}"


def test_validate_series_rejects_wip_subjects():
    """Series validation should catch WIP subjects across patches."""

    patch = (
        PATCH_TEMPLATE_HEADER.format(
            subject='[PATCH] Document new behaviour',
            body='Prototype change.\n\nSigned-off-by: Hook Tester <hooks@token.place>\n',
        )
        + PATCH_DIFF_SECTION.format(hunk_line='+Example addition\n')
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        patch_path = Path(tmpdir) / 'patch.patch'
        patch_path.write_text(patch, encoding='utf-8')

        subjects_file = Path(tmpdir) / 'subjects.txt'

        env = os.environ.copy()
        env['TOKEN_PLACE_VALIDATE_IMPORT_ONLY'] = '1'
        env['TOKEN_PLACE_SUBJECTS_FILE'] = str(subjects_file)

        patch_result = subprocess.run(
            [
                'bash',
                '-c',
                f". '{SCRIPT_PATH}' && validate_patch '{patch_path}'",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        assert (
            patch_result.returncode == 0
        ), f"validate_patch failed unexpectedly: stderr={patch_result.stderr!r}"

        subjects_file.write_text('WIP: experimental change\n', encoding='utf-8')

        series_result = subprocess.run(
            [
                'bash',
                '-c',
                f". '{SCRIPT_PATH}' && validate_series",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    assert series_result.returncode != 0
    assert 'series subject' in series_result.stderr
