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

# Ensure the worker scratch space exists when running with a read-only root filesystem.
if [ ! -d "${WORKER_TMP_DIR}" ]; then
  mkdir -p "${WORKER_TMP_DIR}"
fi

# This exact dedicated directory may survive an application-container restart in
# a pod-lifetime emptyDir. Refuse ambiguous or unsafe targets before cleanup.
case "${METRICS_DIR}" in
  ""|/|.|..) echo "unsafe PROMETHEUS_MULTIPROC_DIR" >&2; exit 1 ;;
esac
if [ -L "${METRICS_DIR}" ]; then
  echo "PROMETHEUS_MULTIPROC_DIR must not be a symlink" >&2
  exit 1
fi
mkdir -p "${METRICS_DIR}"
if [ ! -d "${METRICS_DIR}" ]; then
  echo "PROMETHEUS_MULTIPROC_DIR is not a directory" >&2
  exit 1
fi
find "${METRICS_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
export PROMETHEUS_MULTIPROC_DIR="${METRICS_DIR}"

exec gunicorn \
  --config "${TOKENPLACE_GUNICORN_CONFIG:-docker/relay/gunicorn.conf.py}" \
  --bind "${HOST}:${PORT}" \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --graceful-timeout "${GRACEFUL_TIMEOUT}" \
  --timeout "${TIMEOUT}" \
  --worker-tmp-dir "${WORKER_TMP_DIR}" \
  --access-logfile '-' \
  --error-logfile '-' \
  relay:app
