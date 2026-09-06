# Relay release-safety gate

Every production-eligible relay image built from `main` or a semantic-version tag must pass the
behavioral contract in `config/relay_release_safety_contract.json`. The gate intentionally tests a
built container rather than Git ancestry. This allows a reviewed maintenance branch to use a
behavior-equivalent backport while preventing an omitted fix from being published.

## Run locally

Build the same candidate shape used by CI, then run the gate:

```bash
docker build -t tokenplace-relay:safety-candidate .
image_id="$(docker image inspect --format '{{.Id}}' tokenplace-relay:safety-candidate)"
python scripts/relay_release_safety_gate.py \
  --image tokenplace-relay:safety-candidate \
  --expected-image-id "$image_id" \
  --source-commit "$(git rev-parse HEAD)" \
  --release-ref "$(git symbolic-ref --short HEAD 2>/dev/null || git describe --always)" \
  --evidence /tmp/relay-release-safety-evidence.json
```

The command starts the candidate with deliberately small ordinary API limits and uses only
synthetic requests. It writes non-secret evidence containing the source and artifact identities
and one result per required check. It never records request bodies, client identity, credentials,
or the randomly generated unmatched paths.

Any missing, duplicate, malformed, unimplemented, skipped, or unsuccessful requirement fails the
gate. A default Flask metric family, a synthetic raw-path label, excessive metric-series growth,
quota consumption by a public-information read, or failure to limit an ordinary protected route
identifies the failed contract member in stderr and the evidence file. Publication depends on the
gate job, so failure prevents package credentials from being used.

## Add a mandatory requirement

1. Add a stable check ID to `required_checks` in the JSON contract.
2. Add the same ID to `IMPLEMENTED_CHECKS` and implement its artifact probe in `evaluate()`.
3. Add passing and focused negative tests in `tests/unit/test_relay_release_safety_gate.py`.
4. Run the unit tests and the local built-image command above. Do not add an optional or
   continue-on-error workflow path.

The loader requires the contract and implementation sets to match exactly. This makes a partial
edit fail closed rather than silently reducing coverage.

## Behavior-equivalent backports

The approval is a normal code review of the backport and its behavioral evidence; there is no
ancestry bypass flag. Reviewers should compare the changed implementation with the canonical fix,
confirm that all focused negative tests pass, and inspect the uploaded
`relay-release-safety-evidence-<sha>` artifact. A successful gate proves behavior for the candidate
even when the canonical fix commit is not an ancestor. The workflow summary records the source
commit, release ref/base, local candidate image ID, each contract result, and—after publication—the
immutable OCI index digest. Approval does not deploy or alter staging or production.
