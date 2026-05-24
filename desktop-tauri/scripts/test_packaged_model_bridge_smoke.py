#!/usr/bin/env python3
"""Packaged model-bridge smoke test for dependency-isolated startup behavior."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='token-place-model-bridge-smoke-') as tmpdir:
        resources_root = Path(tmpdir) / 'resources'
        python_dir = resources_root / 'python'
        python_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(REPO_ROOT / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py', python_dir / 'model_bridge.py')
        shutil.copy2(REPO_ROOT / 'desktop-tauri' / 'src-tauri' / 'python' / 'path_bootstrap.py', python_dir / 'path_bootstrap.py')
        shutil.copy2(REPO_ROOT / 'config.py', resources_root / 'config.py')

        env = os.environ.copy()
        env['PYTHONNOUSERSITE'] = '1'
        env['PYTHONPATH'] = ''

        result = subprocess.run(
            [sys.executable, str(python_dir / 'model_bridge.py'), 'inspect'],
            cwd=tmpdir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f'inspect failed: rc={result.returncode}; stdout={result.stdout}; stderr={result.stderr}')

        payload = json.loads(result.stdout.strip())
        if payload.get('ok') is not True:
            raise RuntimeError(f'inspect returned error payload: {payload}')
        if 'Missing Python dependency for model downloads' in result.stdout:
            raise RuntimeError(f'unexpected startup dependency error: {result.stdout}')
        if "No module named 'psutil'" in result.stdout:
            raise RuntimeError(f'unexpected psutil import error surfaced: {result.stdout}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
