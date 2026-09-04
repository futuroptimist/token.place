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

# Validate and clean the dedicated leaf in one process so no unresolved path is
# passed to a later destructive command. A pod-lifetime emptyDir can survive
# container restarts.
python - "${METRICS_DIR}" <<'PY'
import os
import shutil
import sys

metrics_dir = sys.argv[1]
parent = os.path.dirname(metrics_dir)
if (
    not os.path.isabs(metrics_dir)
    or os.path.normpath(metrics_dir) != metrics_dir
    or os.path.basename(metrics_dir) != "tokenplace-prometheus-multiproc"
    or not os.path.isdir(parent)
    or os.path.realpath(parent) != parent
    or os.path.islink(metrics_dir)
    or (os.path.lexists(metrics_dir) and not os.path.isdir(metrics_dir))
):
    sys.stderr.write("PROMETHEUS_MULTIPROC_DIR must be a canonical dedicated directory\n")
    raise SystemExit(1)

if os.path.isdir(metrics_dir):
    shutil.rmtree(metrics_dir)
os.mkdir(metrics_dir)
PY

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
