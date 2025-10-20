#!/usr/bin/env sh
set -euo pipefail

PORT="${RELAY_PORT:-5010}"
WORKERS="${RELAY_WORKERS:-2}"
THREADS="${RELAY_THREADS:-4}"
TIMEOUT="${RELAY_TIMEOUT:-120}"
GRACEFUL_TIMEOUT="${RELAY_GRACEFUL_TIMEOUT:-30}"

exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WORKERS}" \
  --worker-class gthread \
  --threads "${THREADS}" \
  --timeout "${TIMEOUT}" \
  --graceful-timeout "${GRACEFUL_TIMEOUT}" \
  --log-level info \
  --worker-tmp-dir /tmp \
  relay:app
