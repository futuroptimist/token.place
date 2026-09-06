#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: $0 IMAGE [EVIDENCE_PATH]}"
evidence="${2:-relay-release-safety-evidence.json}"
name="tokenplace-release-safety-${RANDOM}-$$"
port="${TOKENPLACE_SAFETY_GATE_PORT:-15012}"
source_commit="${SOURCE_COMMIT:-$(git rev-parse HEAD)}"
release_ref="${RELEASE_REF:-$(git symbolic-ref -q --short HEAD || git describe --always --exact-match 2>/dev/null || git rev-parse HEAD)}"
digest="$(docker image inspect --format '{{.Id}}' "${image}")"

cleanup() { docker rm -f "${name}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "${name}" \
  -p "127.0.0.1:${port}:5010" \
  -e TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0 \
  -e API_RATE_LIMIT=2/minute \
  -e API_DAILY_QUOTA=100/day \
  "${image}" >/dev/null

for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${port}/livez" >/dev/null; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${port}/livez" >/dev/null

python scripts/relay_release_safety_gate.py \
  --base-url "http://127.0.0.1:${port}" \
  --evidence "${evidence}" \
  --source-commit "${source_commit}" \
  --release-ref "${release_ref}" \
  --candidate-image "${image}" \
  --candidate-digest "${digest}"
