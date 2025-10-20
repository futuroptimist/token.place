#!/usr/bin/env sh
set -euo pipefail

PORT="${RELAY_PORT:-5010}"
HOST="${RELAY_HOST:-0.0.0.0}"
WORKERS="${RELAY_WORKERS:-2}"
THREADS="${RELAY_THREADS:-1}"
GRACEFUL_TIMEOUT="${RELAY_GRACEFUL_TIMEOUT:-30}"
TIMEOUT="${RELAY_TIMEOUT:-60}"
WORKER_TMP_DIR="${RELAY_WORKER_TMP_DIR:-/tmp}"

# Ensure the worker scratch space exists when running with a read-only root filesystem.
if [ ! -d "${WORKER_TMP_DIR}" ]; then
  mkdir -p "${WORKER_TMP_DIR}"
fi

exec gunicorn \
  --bind "${HOST}:${PORT}" \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --graceful-timeout "${GRACEFUL_TIMEOUT}" \
  --timeout "${TIMEOUT}" \
  --worker-tmp-dir "${WORKER_TMP_DIR}" \
  --access-logfile '-' \
  --error-logfile '-' \
  relay:app
