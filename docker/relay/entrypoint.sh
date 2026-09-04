#!/usr/bin/env sh
set -eu

PORT="${RELAY_PORT:-5010}"
HOST="${RELAY_HOST:-0.0.0.0}"
WORKERS="${RELAY_WORKERS:-1}"
THREADS="${RELAY_THREADS:-4}"
GRACEFUL_TIMEOUT="${RELAY_GRACEFUL_TIMEOUT:-30}"
TIMEOUT="${RELAY_TIMEOUT:-60}"
WORKER_TMP_DIR="${RELAY_WORKER_TMP_DIR:-/tmp}"
METRICS_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/tokenplace-prometheus}"

# This directory can survive a same-pod container restart. Clean only the exact,
# dedicated target before workers import prometheus_client. Refuse unsafe targets.
case "${METRICS_DIR}" in
  /*) ;;
  *) echo "PROMETHEUS_MULTIPROC_DIR must be absolute" >&2; exit 1 ;;
esac
case "${METRICS_DIR}" in
  /|/tmp|/var/tmp) echo "PROMETHEUS_MULTIPROC_DIR must be dedicated" >&2; exit 1 ;;
esac
if [ -L "${METRICS_DIR}" ]; then
  echo "PROMETHEUS_MULTIPROC_DIR must not be a symlink" >&2
  exit 1
fi
rm -rf -- "${METRICS_DIR}"
mkdir -p -- "${METRICS_DIR}"
export PROMETHEUS_MULTIPROC_DIR="${METRICS_DIR}"

# Ensure the worker scratch space exists when running with a read-only root filesystem.
if [ ! -d "${WORKER_TMP_DIR}" ]; then
  mkdir -p "${WORKER_TMP_DIR}"
fi

exec gunicorn \
  --config gunicorn_metrics.py \
  --bind "${HOST}:${PORT}" \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --graceful-timeout "${GRACEFUL_TIMEOUT}" \
  --timeout "${TIMEOUT}" \
  --worker-tmp-dir "${WORKER_TMP_DIR}" \
  --access-logfile '-' \
  --error-logfile '-' \
  relay:app
