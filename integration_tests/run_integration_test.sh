#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$SCRIPT_DIR/run_dspace_integration.sh"

if [[ ! -x "$RUNNER" ]]; then
  echo "Expected $RUNNER to be present and executable" >&2
  exit 1
fi

if [[ "${RUN_DSPACE_INTEGRATION:-0}" == "1" ]]; then
  exec "$RUNNER" "$@"
else
  echo "Skipping DSPACE integration harness (set RUN_DSPACE_INTEGRATION=1 to enable)."
  exit 0
fi
