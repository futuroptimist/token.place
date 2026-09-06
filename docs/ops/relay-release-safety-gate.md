# Relay release safety gate

Every production-eligible relay image publication is blocked on the behavioral contract in
`config/relay_release_safety_contract.json`. The gate starts the built candidate image with an
isolated, deliberately small quota and tests its HTTP and Prometheus surfaces. It does not query
staging or production, inspect commit ancestry, or record requests, client identity, credentials,
or sampled paths.

## Run locally

Build the image with immutable revision metadata, then run the same entry point as CI:

```bash
commit=$(git rev-parse HEAD)
docker build --label "org.opencontainers.image.revision=$commit" -t tokenplace-relay:safety .
python scripts/relay_release_safety_gate.py \
  --image tokenplace-relay:safety \
  --source-commit "$commit" \
  --release-ref "$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)" \
  --release-base main \
  --evidence relay-release-safety-evidence.json
```

A passing report contains `passed: true`, the source/ref and immutable image identity, plus a result
for every contract requirement. For publication, CI first pushes a uniquely named non-release
candidate index, pulls it by digest, qualifies that exact artifact, and only then attaches the
production-eligible tags. CI uploads the non-secret report and records the OCI index digest in the
workflow summary. Missing, duplicate, skipped, malformed, mismatched, or false results fail
closed. The report intentionally excludes request bodies, credentials, client identities, logs, and
the randomized unmatched paths used by the probe.

## Maintain the contract

To add a mandatory requirement:

1. Add a stable ID and description to `config/relay_release_safety_contract.json`.
2. Add the same ID to `EXPECTED_IDS` and an executable artifact-level check to `execute_checks()`.
3. Add passing and failing fixtures in `tests/unit/test_relay_release_safety_gate.py`.
4. Run the unit tests and the local image command above.

The one-to-one ID validation prevents a review-only declaration or an undeclared executable check.
Keep probes deterministic in their assertions, bounded in traffic, and free of sensitive evidence.

## Behavior-equivalent backports

The contract approves observed behavior rather than requiring particular commits to be ancestors.
A maintenance backport is therefore eligible when reviewers confirm its scope and the built image
passes every current contract result with matching revision metadata. Reviewers should link the CI
evidence artifact and immutable published digest in the pull request or release record. An ancestry
claim is neither needed nor accepted as a substitute for the behavioral gate.
