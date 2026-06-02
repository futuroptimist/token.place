#!/usr/bin/env bash
# Shared desktop operator parity validation entry point for local release checks.
# CI invokes the underlying scripts directly so logs and platform-specific setup
# stay explicit in workflow output; this wrapper keeps local Windows/macOS/Linux
# command names aligned.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

MODE="${1:-local}"
case "$MODE" in
  local)
    run python desktop-tauri/scripts/test_packaged_operator_e2e.py
    run python desktop-tauri/scripts/test_desktop_relay_operator_parity_e2e.py
    if [[ "$(uname -s)" == "Darwin" ]]; then
      TOKENPLACE_REQUIRE_NO_RELAY_E2E=1 run python desktop-tauri/scripts/test_desktop_no_relay_autostart_e2e.py
    else
      run python desktop-tauri/scripts/test_desktop_no_relay_autostart_e2e.py
    fi
    ;;
  inspect)
    TOKEN_PLACE_INSPECT_ONLY=1 run python desktop-tauri/scripts/test_packaged_operator_e2e.py
    ;;
  runtime)
    shift
    run python desktop-tauri/scripts/verify_desktop_runtime.py "$@"
    ;;
  *)
    cat >&2 <<'USAGE'
Usage: desktop-tauri/scripts/validate_desktop_parity.sh [local|inspect|runtime --mode MODE --model PATH]

Modes:
  local    Run packaged-resource, API v1 relay parity, and Stop/Start lifecycle checks.
  inspect  Run dependency-isolated packaged bridge inspect smoke only.
  runtime  Forward remaining arguments to verify_desktop_runtime.py.
USAGE
    exit 2
    ;;
esac
