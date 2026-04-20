#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python -m pip install -r config/requirements_codex_verification.txt

if ! python -m playwright install --with-deps chromium; then
  echo "playwright --with-deps failed; retrying without system deps"
  python -m playwright install chromium
fi
