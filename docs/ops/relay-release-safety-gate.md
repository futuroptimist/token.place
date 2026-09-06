# Relay release-safety gate

Every production-eligible relay image publication from `.github/workflows/ci-image.yml`
is qualified against the artifact behavior declared in
`config/relay_release_safety_contract.json`. The publish job first pushes a
run-scoped `candidate-*` tag, tests its Linux AMD64 image, and only then copies the
same multi-architecture OCI index to `main-*`, `sha-*`, `main-latest`, or semantic
release tags. A failed, missing, or malformed check prevents those release tags
from being created. The candidate tag is quarantined and is not a deployment tag.

The resulting JSON artifact and job summary record the source commit, release ref
and base, candidate coordinate, immutable digest, and every contract result. They
contain aggregate counts only: credentials, client identities, request bodies, and
generated unmatched-path samples are never included.

## Run locally

Build the exact candidate and attach its full source revision label, then invoke
the same gate used by CI:

```bash
revision="$(git rev-parse HEAD)"
docker build \
  --label "org.opencontainers.image.revision=${revision}" \
  -t tokenplace-relay:safety-candidate -f Dockerfile .
python scripts/relay_release_safety_gate.py \
  --image tokenplace-relay:safety-candidate \
  --source-commit "${revision}" \
  --release-ref "$(git symbolic-ref --short HEAD)" \
  --release-base main \
  --evidence /tmp/relay-release-safety-evidence.json
```

Success means every `results.*.passed` value is exactly `true`. Missing contract
entries, malformed Prometheus output, an OCI revision mismatch, startup failure,
or any unsuccessful check fails closed. The script deliberately evaluates HTTP
behavior and never requires the original fix commits to be ancestors, so a
reviewed behavior-equivalent maintenance backport follows the same approval path:
review the backport, build it with the correct revision label, run the gate, and
retain the evidence artifact. Do not add an ancestry override or manually edit
evidence to approve a backport.

## Add a mandatory requirement

1. Add one stable ID and description to
   `config/relay_release_safety_contract.json`.
2. Implement the artifact-level probe and aggregate result in
   `scripts/relay_release_safety_gate.py`, and add its ID to `REQUIRED_CHECKS`.
3. Add passing, failing, missing-evidence, and privacy regression tests in
   `tests/unit/test_relay_release_safety_gate.py`.
4. Run the focused tests and the local image command above. Workflow call sites
   remain unchanged because they all invoke the single extensible gate.

Never encode a commit-only exception. Commit ancestry may inform review, but the
candidate artifact must independently satisfy every mandatory behavior.
