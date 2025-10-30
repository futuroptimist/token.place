#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${TOKEN_PLACE_DSPACE_WORKDIR:-$SCRIPT_DIR}"
TOKEN_PLACE_DIR="$WORK_DIR/token.place"
DSPACE_DIR="$WORK_DIR/dspace"
CLIENT_DIR="$WORK_DIR/token.place-client"
CLIENT_TEMPLATE_DIR="$SCRIPT_DIR/token_place_client_template"
PY_ENV_DIR="$TOKEN_PLACE_DIR/env"
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

run_cmd() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf 'DRY-RUN:'
    for part in "$@"; do
      printf ' %q' "$part"
    done
    printf '\n'
  else
    "$@"
  fi
}

log_step() {
  echo "$1"
}

ensure_repo() {
  local name="$1"
  local url="$2"
  local dest="$3"
  shift 3 || true
  local extra_args=("$@")

  if [[ ! -d "$dest" ]]; then
    log_step "Cloning $name repository..."
    run_cmd git clone "${extra_args[@]}" "$url" "$dest"
  else
    log_step "$name repository already present. Fetching latest changes..."
    run_cmd git -C "$dest" fetch --tags --prune
    run_cmd git -C "$dest" pull --ff-only || true
  fi
}

setup_python_env() {
  log_step "Setting up Python virtual environment..."
  if [[ ! -d "$PY_ENV_DIR" ]]; then
    run_cmd python -m venv "$PY_ENV_DIR"
  fi
  if [[ $DRY_RUN -eq 0 ]]; then
    # shellcheck source=/dev/null
    source "$PY_ENV_DIR/bin/activate"
  else
    printf 'DRY-RUN: source %s\n' "$PY_ENV_DIR/bin/activate"
  fi
  run_cmd "$PY_ENV_DIR/bin/pip" install -r "$TOKEN_PLACE_DIR/config/requirements_server.txt"
  run_cmd "$PY_ENV_DIR/bin/pip" install -r "$TOKEN_PLACE_DIR/config/requirements_relay.txt"
  run_cmd "$PY_ENV_DIR/bin/pip" install -r "$TOKEN_PLACE_DIR/requirements.txt"
}

setup_dspace_env() {
  log_step "Installing DSPACE dependencies..."
  run_cmd npm --prefix "$DSPACE_DIR" ci
}

setup_client_package() {
  log_step "Creating token.place client package..."
  if [[ ! -d "$CLIENT_DIR" ]]; then
    if [[ ! -f "$CLIENT_TEMPLATE_DIR/index.js" ]]; then
      echo "Client template missing at $CLIENT_TEMPLATE_DIR" >&2
      exit 1
    fi
    if [[ $DRY_RUN -eq 0 ]]; then
      run_cmd mkdir -p "$CLIENT_DIR"
      run_cmd cp "$CLIENT_TEMPLATE_DIR/package.json" "$CLIENT_DIR/package.json"
      run_cmd cp "$CLIENT_TEMPLATE_DIR/index.js" "$CLIENT_DIR/index.js"
      run_cmd npm --prefix "$CLIENT_DIR" ci
    else
      printf 'DRY-RUN: mkdir -p %s\n' "$CLIENT_DIR"
      printf 'DRY-RUN: cp %s/package.json %s/package.json\n' \
        "$CLIENT_TEMPLATE_DIR" "$CLIENT_DIR"
      printf 'DRY-RUN: npm --prefix %s ci\n' "$CLIENT_DIR"
      printf 'DRY-RUN: cp %s/index.js %s/index.js\n' \
        "$CLIENT_TEMPLATE_DIR" "$CLIENT_DIR"
    fi
  else
    log_step "token.place client package already exists. Skipping creation."
  fi
}

run_integration_tests() {
  log_step "Running integration tests..."
  local test_script="$WORK_DIR/test_dspace_integration.js"
  if [[ -f "$test_script" ]]; then
    run_cmd npx --prefix "$WORK_DIR" mocha "$test_script"
  else
    log_step "No test_dspace_integration.js file found in $WORK_DIR. Skipping mocha run."
  fi
}

cleanup() {
  if [[ $DRY_RUN -eq 0 && -n "${VIRTUAL_ENV:-}" ]]; then
    log_step "Deactivating Python virtual environment..."
    deactivate || true
  fi
}

main() {
  ensure_repo "token.place" "https://github.com/futuroptimist/token.place.git" "$TOKEN_PLACE_DIR"
  ensure_repo "DSPACE" "https://github.com/democratizedspace/dspace.git" "$DSPACE_DIR" -b v3

  setup_python_env
  setup_dspace_env
  setup_client_package
  run_integration_tests
  cleanup
}

main "$@"
