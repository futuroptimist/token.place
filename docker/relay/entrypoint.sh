#!/usr/bin/env sh
set -eu

PORT="${RELAY_PORT:-5010}"
HOST="${RELAY_HOST:-0.0.0.0}"
WORKERS="${RELAY_WORKERS:-1}"
THREADS="${RELAY_THREADS:-4}"
GRACEFUL_TIMEOUT="${RELAY_GRACEFUL_TIMEOUT:-30}"
TIMEOUT="${RELAY_TIMEOUT:-60}"
WORKER_TMP_DIR="${RELAY_WORKER_TMP_DIR:-/tmp}"
METRICS_DIR="/tmp/tokenplace-prometheus-multiproc"

# The release relay is intentionally single-process because its queue and lease
# state is memory-backed. Disable prometheus_client multiprocess mode and remove
# stale files left by older containers in the pod-lifetime scratch directory.
if [ "${WORKERS}" != "1" ]; then
  echo "relay requires RELAY_WORKERS=1 for memory-backed state and metrics" >&2
  exit 1
fi
if [ -L "${METRICS_DIR}" ]; then
  echo "refusing unsafe Prometheus metrics directory symlink" >&2
  exit 1
fi
mkdir -p "${METRICS_DIR}"
find "${METRICS_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
unset PROMETHEUS_MULTIPROC_DIR

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
