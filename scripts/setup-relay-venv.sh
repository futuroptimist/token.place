#!/usr/bin/env bash
# Create a virtual environment and install relay-only Python dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if command -v python3.12 >/dev/null 2>&1; then
    PYTHON=python3.12
elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON=python3.11
else
    PYTHON=python3
fi

echo "Using interpreter: $($PYTHON --version)"

if [[ ! -d .venv ]]; then
    "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r config/requirements_relay.txt

cat <<EOF

Relay virtual environment is ready.

  source .venv/bin/activate
  python relay.py

Health check (default port 5010):

  curl http://127.0.0.1:5010/api/v1/health

EOF
