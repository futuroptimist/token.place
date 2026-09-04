#!/usr/bin/env sh
set -eu

PORT="${RELAY_PORT:-5010}"
HOST="${RELAY_HOST:-0.0.0.0}"
WORKERS="${RELAY_WORKERS:-1}"
THREADS="${RELAY_THREADS:-4}"
GRACEFUL_TIMEOUT="${RELAY_GRACEFUL_TIMEOUT:-30}"
TIMEOUT="${RELAY_TIMEOUT:-60}"
WORKER_TMP_DIR="${RELAY_WORKER_TMP_DIR:-/tmp}"
METRICS_DIR="${PROMETHEUS_MULTIPROC_DIR:-}"

# PROMETHEUS_MULTIPROC_DIR is fixed by the image. Validate the exact dedicated
# leaf before destructive cleanup so an empty, relative, broad, or symlinked
# setting fails closed. A pod-lifetime emptyDir can survive container restarts.
case "${METRICS_DIR}" in
  /*/tokenplace-prometheus-multiproc) ;;
  *)
    echo "PROMETHEUS_MULTIPROC_DIR must be an absolute tokenplace-prometheus-multiproc directory" >&2
    exit 1
    ;;
esac
METRICS_PARENT=$(dirname "${METRICS_DIR}")
if [ ! -d "${METRICS_PARENT}" ] || [ -L "${METRICS_PARENT}" ] || [ -L "${METRICS_DIR}" ]; then
  echo "PROMETHEUS_MULTIPROC_DIR has an unsafe parent or target" >&2
  exit 1
fi
rm -rf -- "${METRICS_DIR:?}"
mkdir -- "${METRICS_DIR}"

# Ensure the worker scratch space exists when running with a read-only root filesystem.
if [ ! -d "${WORKER_TMP_DIR}" ]; then
  mkdir -p "${WORKER_TMP_DIR}"
fi

exec gunicorn \
  --config /app/docker/relay/gunicorn.conf.py \
  --bind "${HOST}:${PORT}" \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --graceful-timeout "${GRACEFUL_TIMEOUT}" \
  --timeout "${TIMEOUT}" \
  --worker-tmp-dir "${WORKER_TMP_DIR}" \
  --error-logfile '-' \
  relay:app
